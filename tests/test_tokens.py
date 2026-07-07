"""F3.3 token-usage tests — exact per-parser signals, estimates, rollups.

Hermetic by construction: every fixture writes synthetic session data under
the per-test ``AI_R_HOME`` (or an explicit ``OPENCODE_DB``); nothing touches
the real host vault.  Both estimator branches are covered WITHOUT requiring
tiktoken in the environment: the "installed" branch injects a fake
``tiktoken`` module into ``sys.modules``, the "absent" branch forces the
loader cache to the degraded state.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from ai_r import tokens as tokens_mod
from ai_r.events.aggregate import aggregate
from ai_r.parsers import AgentName, Session, antigravity, claude, codex, opencode, pi
from ai_r.session_stats import session_stats
from ai_r.tokens import (
    COMPONENT_FIELDS,
    TOKEN_FIELDS,
    component_tokens,
    estimate_tokens,
    rollup_component_tokens,
    session_tokens,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# Fixtures: synthetic sessions WITH recorded usage
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_token_session(tmp_sessions_dir: Path) -> str:
    """Claude JSONL with per-call ``message.usage`` incl. a streamed duplicate."""
    sid = "tok-claude-1"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-a" / f"{sid}.jsonl"
    usage_1 = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 5,
    }
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "count my tokens"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": sid,
            },
            # First content block of a streamed response...
            {
                "type": "assistant",
                "requestId": "req-1",
                "message": {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "part one"}],
                    "usage": usage_1,
                },
                "timestamp": "2026-06-14T10:00:05Z",
                "sessionId": sid,
            },
            # ...second block of the SAME API call: identical (id, requestId,
            # usage) — must be deduplicated, not double-counted.
            {
                "type": "assistant",
                "requestId": "req-1",
                "message": {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "part two"}],
                    "usage": usage_1,
                },
                "timestamp": "2026-06-14T10:00:06Z",
                "sessionId": sid,
            },
            # A second, distinct API call (no cache fields at all).
            {
                "type": "assistant",
                "requestId": "req-2",
                "message": {
                    "id": "msg-2",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
                "timestamp": "2026-06-14T10:00:10Z",
                "sessionId": sid,
            },
        ],
    )
    return sid


@pytest.fixture
def codex_token_session(tmp_sessions_dir: Path) -> str:
    """Codex rollout with two cumulative ``token_count`` events (last wins)."""
    uuid = "tok-codex-1"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T10-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": uuid, "cwd": "/tmp/work",
                            "timestamp": "2026-06-14T10:00:00Z"},
            },
            {
                "timestamp": "2026-06-14T10:00:02Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "text", "text": "hi"}]},
            },
            {
                "timestamp": "2026-06-14T10:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 80,
                        "output_tokens": 10, "reasoning_output_tokens": 0,
                        "total_tokens": 110,
                    }},
                },
            },
            {
                "timestamp": "2026-06-14T10:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 200, "cached_input_tokens": 150,
                        "output_tokens": 40, "reasoning_output_tokens": 7,
                        "total_tokens": 240,
                    }},
                },
            },
        ],
    )
    return uuid


@pytest.fixture
def opencode_token_db(tmp_sessions_dir: Path) -> Path:
    """OpenCode DB whose assistant ``message.data`` carries ``tokens`` blocks."""
    db_path = tmp_sessions_dir / "opencode-tokens.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES session(id),
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
        ("tok-oc-1", None, "Tokens session",
         1_716_000_000_000, 1_716_000_500_000),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("tm-0", "tok-oc-1", 1_716_000_100_000, 1_716_000_100_000,
         json.dumps({"role": "user"})),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("tm-1", "tok-oc-1", 1_716_000_200_000, 1_716_000_200_000,
         json.dumps({
             "role": "assistant",
             "tokens": {"input": 30, "output": 12, "reasoning": 3,
                        "cache": {"read": 100, "write": 4}},
         })),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("tm-2", "tok-oc-1", 1_716_000_300_000, 1_716_000_300_000,
         json.dumps({
             "role": "assistant",
             "tokens": {"input": 5, "output": 6, "reasoning": 0,
                        "cache": {"read": 0, "write": 0}},
         })),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def pi_token_session(tmp_sessions_dir: Path) -> str:
    """Pi JSONL whose assistant ``message.usage`` carries counters."""
    uuid = "tok-pi-1"
    jsonl = (
        tmp_sessions_dir / ".pi" / "agent" / "sessions" / "--tmp-work--"
        / f"2026-06-14T10-00-00-000Z_{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {"type": "session", "version": 3, "id": uuid,
             "timestamp": "2026-06-14T10:00:00.000Z", "cwd": "/tmp/work"},
            {"type": "message", "id": "u-1", "parentId": None,
             "timestamp": "2026-06-14T10:00:02.000Z",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "hi"}]}},
            {"type": "message", "id": "a-1", "parentId": "u-1",
             "timestamp": "2026-06-14T10:00:04.000Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "yo"}],
                         "usage": {"input": 100, "output": 7,
                                   "cacheRead": 20, "cacheWrite": 3,
                                   "totalTokens": 130}}},
            {"type": "message", "id": "a-2", "parentId": "a-1",
             "timestamp": "2026-06-14T10:00:06.000Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input": 10, "output": 5,
                                   "cacheRead": 0, "cacheWrite": 0,
                                   "totalTokens": 15}}},
        ],
    )
    return uuid


@pytest.fixture
def _no_tiktoken(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the degraded (tiktoken-absent) estimator branch."""
    monkeypatch.setattr(
        tokens_mod, "_ENCODER_STATE", {"loaded": True, "encoder": None}
    )


# ---------------------------------------------------------------------------
# Per-parser exact extraction
# ---------------------------------------------------------------------------


def test_claude_exact_usage_summed_and_deduped(claude_token_session: str) -> None:
    usage = claude.read_token_usage(claude_token_session)
    assert usage == {
        "input": 110,          # 100 + 10; the streamed duplicate NOT re-counted
        "output": 70,          # 50 + 20
        "reasoning": None,     # no Claude breakdown — honest None
        "cache_read": 10,
        "cache_write": 5,
        "total": 195,
    }


def test_claude_without_usage_returns_none(fake_claude_session: Path) -> None:
    assert claude.read_token_usage("test-claude-1") is None


def test_claude_unknown_session_raises(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        claude.read_token_usage("no-such-session")


def test_codex_last_cumulative_token_count_wins(codex_token_session: str) -> None:
    usage = codex.read_token_usage(codex_token_session)
    assert usage == {
        "input": 200,
        "output": 40,
        "reasoning": 7,
        "cache_read": 150,
        "cache_write": None,   # no cache-creation counter in the format
        "total": 240,
    }


def test_codex_without_token_count_returns_none(fake_codex_session: Path) -> None:
    assert codex.read_token_usage("test-codex-1") is None


def test_opencode_exact_from_message_tokens(
    opencode_token_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_DB", str(opencode_token_db))
    usage = opencode.read_token_usage("tok-oc-1")
    assert usage == {
        "input": 35,
        "output": 18,
        "reasoning": 3,
        "cache_read": 100,
        "cache_write": 4,
        "total": 160,
    }


def test_opencode_without_tokens_blocks_returns_none(fake_opencode_db: Path) -> None:
    # fake_opencode_db messages carry role-only metadata (no tokens block).
    assert opencode.read_token_usage(
        "test-oc-1", override=str(fake_opencode_db)
    ) is None


def test_pi_exact_usage_summed(pi_token_session: str) -> None:
    usage = pi.read_token_usage(pi_token_session)
    assert usage == {
        "input": 110,
        "output": 12,
        "reasoning": None,
        "cache_read": 20,
        "cache_write": 3,
        "total": 145,
    }


def test_pi_total_tokens_fallback(tmp_sessions_dir: Path) -> None:
    """Per-field counters absent → the summed ``totalTokens`` still counts."""
    uuid = "tok-pi-2"
    jsonl = (
        tmp_sessions_dir / ".pi" / "agent" / "sessions" / "--tmp-work--"
        / f"2026-06-14T11-00-00-000Z_{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {"type": "session", "version": 3, "id": uuid,
             "timestamp": "2026-06-14T11:00:00.000Z", "cwd": "/tmp/work"},
            {"type": "message", "id": "a-1", "parentId": None,
             "timestamp": "2026-06-14T11:00:04.000Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "yo"}],
                         "usage": {"totalTokens": 42}}},
        ],
    )
    usage = pi.read_token_usage(uuid)
    assert usage is not None
    assert usage["total"] == 42
    assert usage["input"] == 0 and usage["output"] == 0


def test_antigravity_always_none(fake_antigravity_brain: Path) -> None:
    assert antigravity.read_token_usage("test-ag-1") is None
    with pytest.raises(FileNotFoundError):
        antigravity.read_token_usage("no-such-brain")


# ---------------------------------------------------------------------------
# Estimator: tiktoken-present vs degraded chars/4
# ---------------------------------------------------------------------------


def test_estimate_tokens_heuristic_without_tiktoken(_no_tiktoken: None) -> None:
    count, estimator = estimate_tokens("x" * 10)
    assert (count, estimator) == (3, "chars/4")  # ceil(10 / 4)


def test_estimate_tokens_with_fake_tiktoken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeEncoding:
        def encode(self, text: str, disallowed_special=()) -> list[int]:
            return [1] * len(text.split())

    fake = types.ModuleType("tiktoken")
    fake.get_encoding = lambda name: _FakeEncoding()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    monkeypatch.setattr(
        tokens_mod, "_ENCODER_STATE", {"loaded": False, "encoder": None}
    )
    count, estimator = estimate_tokens("one two three")
    assert (count, estimator) == (3, "tiktoken")


def test_estimator_degrades_when_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ``None`` sys.modules entry makes ``import tiktoken`` raise — the
    # loader must degrade to the heuristic, never crash.
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    monkeypatch.setattr(
        tokens_mod, "_ENCODER_STATE", {"loaded": False, "encoder": None}
    )
    count, estimator = estimate_tokens("abcdefgh")
    assert (count, estimator) == (2, "chars/4")


# ---------------------------------------------------------------------------
# session_tokens orchestration
# ---------------------------------------------------------------------------


def test_session_tokens_exact_source(claude_token_session: str) -> None:
    session = claude.read_session(claude_token_session)
    block = session_tokens(session)
    assert block["source"] == "exact"
    assert block["total"] == 195
    assert "estimator" not in block
    assert set(TOKEN_FIELDS) <= set(block)


def test_session_tokens_estimate_without_signal(
    fake_claude_session: Path, _no_tiktoken: None
) -> None:
    session = claude.read_session("test-claude-1")
    block = session_tokens(session)
    assert block["source"] == "estimate"
    assert block["estimator"] == "chars/4"
    assert isinstance(block["total"], int) and block["total"] > 0
    # Only the total is estimated; sub-fields stay honest None.
    for field in ("input", "output", "reasoning", "cache_read", "cache_write"):
        assert block[field] is None


def test_session_tokens_no_signal_at_all(_no_tiktoken: None) -> None:
    from datetime import datetime, timezone

    ghost = Session(
        uuid="no-such-session",
        agent=AgentName.CLAUDE,
        title="ghost",
        date=datetime(2026, 6, 14, tzinfo=timezone.utc),
        path="/nonexistent",
        message_count=0,
    )
    block = session_tokens(ghost)
    assert block["source"] is None
    assert all(block[field] is None for field in TOKEN_FIELDS)


# ---------------------------------------------------------------------------
# aggregate: the ``tokens`` metric (pure fold)
# ---------------------------------------------------------------------------


def test_aggregate_tokens_metric_sums_and_provenance() -> None:
    rows = [
        {"agent": "claude", "tokens": {
            "input": 100, "output": 50, "reasoning": None,
            "cache_read": 10, "cache_write": 5, "total": 165,
            "source": "exact",
        }},
        {"agent": "claude", "tokens": {
            "input": None, "output": None, "reasoning": None,
            "cache_read": None, "cache_write": None, "total": 40,
            "source": "estimate", "estimator": "chars/4",
        }},
        {"agent": "codex", "tokens": 25},  # bare-int convenience form
        {"agent": "codex"},                # no token info at all
    ]
    result = aggregate(rows, group_by="agent", metrics=["count", "tokens"])
    groups = {g["group"]: g for g in result["groups"]}

    claude_tokens = groups["claude"]["tokens"]
    assert claude_tokens["total"] == 205
    assert claude_tokens["input"] == 100
    assert claude_tokens["reasoning"] is None  # no row carried an int
    assert (claude_tokens["exact"], claude_tokens["estimated"],
            claude_tokens["unknown"]) == (1, 1, 0)

    codex_tokens = groups["codex"]["tokens"]
    assert codex_tokens["total"] == 25
    assert codex_tokens["input"] is None
    # Bare int = total of unknown provenance; missing block = unknown too.
    assert (codex_tokens["exact"], codex_tokens["estimated"],
            codex_tokens["unknown"]) == (0, 0, 2)

    totals = result["totals"]["tokens"]
    assert totals["total"] == 230
    assert totals["exact"] + totals["estimated"] + totals["unknown"] == len(rows)


def test_aggregate_tokens_metric_empty_rows() -> None:
    result = aggregate([], group_by="agent", metrics=["tokens"])
    assert result["groups"] == []
    totals = result["totals"]["tokens"]
    assert totals["total"] is None
    assert (totals["exact"], totals["estimated"], totals["unknown"]) == (0, 0, 0)


def test_aggregate_unknown_metric_still_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown metric"):
        aggregate([], group_by="agent", metrics=["tokenz"])


# ---------------------------------------------------------------------------
# session_stats(with_tokens=...)
# ---------------------------------------------------------------------------


def test_session_stats_with_tokens_claude_exact(
    claude_token_session: str, _no_tiktoken: None
) -> None:
    stats = session_stats(agent="claude", group_by="agent", with_tokens=True)
    assert stats["groups"], "expected the fixture session in scope"
    group = stats["groups"][0]
    assert group["group"] == "claude"
    block = group["tokens"]
    assert block["total"] == 195
    assert (block["exact"], block["estimated"], block["unknown"]) == (1, 0, 0)
    assert stats["totals"]["tokens"]["total"] == 195


def test_session_stats_with_tokens_estimated_session(
    fake_codex_session: Path, _no_tiktoken: None
) -> None:
    # The codex fixture has no token_count events → labeled estimate.
    stats = session_stats(agent="codex", group_by="agent", with_tokens=True)
    assert stats["groups"]
    block = stats["groups"][0]["tokens"]
    assert block["estimated"] == 1
    assert block["exact"] == 0
    assert isinstance(block["total"], int) and block["total"] > 0


def test_session_stats_without_tokens_is_unchanged(
    claude_token_session: str,
) -> None:
    stats = session_stats(agent="claude", group_by="agent")
    assert "tokens" not in stats["totals"]
    assert all("tokens" not in g for g in stats["groups"])


def test_session_stats_with_tokens_validates_type() -> None:
    with pytest.raises(ValueError, match="with_tokens"):
        session_stats(with_tokens="yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# MCP wrapper surface
# ---------------------------------------------------------------------------


def test_mcp_session_stats_forwards_with_tokens(
    claude_token_session: str, _no_tiktoken: None
) -> None:
    from ai_r.mcp_server import session_stats as mcp_session_stats

    result = mcp_session_stats(agent="claude", group_by="agent", with_tokens=True)
    assert "error" not in result
    assert result["totals"]["tokens"]["total"] == 195


def test_mcp_session_stats_with_tokens_invalid_is_error_dict() -> None:
    from ai_r.mcp_server import session_stats as mcp_session_stats

    result = mcp_session_stats(with_tokens="yes")  # type: ignore[arg-type]
    assert result["error"] == "invalid_argument"
    assert "with_tokens" in result["message"]


# ---------------------------------------------------------------------------
# component_tokens — per-component estimate over the event taxonomy
# ---------------------------------------------------------------------------


from ai_r.parsers.models import Message  # noqa: E402


def _msg(
    role: str = "assistant",
    text: str = "",
    thinking: str = "",
    tool_use=(),
    tool_result=(),
    tokens=None,
) -> Message:
    return Message(
        role=role,
        text=text,
        thinking=thinking,
        tool_use=tuple(tool_use),
        tool_result=tuple(tool_result),
        tokens=tokens,
    )


def test_component_tokens_taxonomy_chars4(_no_tiktoken: None) -> None:
    """user→user_turn, assistant→assistant_turn, thinking→thinking, Bash→tool_call.bash."""
    messages = [
        _msg(role="user", text="hello there, please run ls"),
        _msg(
            role="assistant",
            text="here is an answer",
            thinking="let me think about it",
            tool_use=(
                {"name": "Bash", "input": "ls -la /tmp", "tool_use_id": "t1"},
            ),
            tool_result=(
                {"content": "a tool result body", "is_error": False,
                 "tool_use_id": "t1"},
            ),
        ),
    ]
    block = component_tokens(messages, agent="claude")
    assert block is not None
    assert block["source"] == "estimate"
    assert block["estimator"] == "chars/4"
    assert block["user_turn"] > 0
    assert block["assistant_turn"] > 0
    assert block["thinking"] > 0
    # No plan surface here → plan measured 0 (honest, present).
    assert block["plan"] == 0
    assert set(block["tool_call"]) == {"bash"}
    assert block["tool_call"]["bash"] > 0


def test_component_tokens_plan_not_tool_call(_no_tiktoken: None) -> None:
    """An ExitPlanMode call → plan, NOT tool_call; its result also → plan (g)."""
    messages = [
        _msg(
            role="assistant",
            tool_use=(
                {"name": "ExitPlanMode",
                 "input": {"plan": "# Title\n\nstep one\nstep two"},
                 "tool_use_id": "p1"},
            ),
            tool_result=(
                {"content": "plan approved by the user", "is_error": False,
                 "tool_use_id": "p1"},
            ),
        ),
    ]
    block = component_tokens(messages, agent="claude")
    assert block is not None
    assert block["plan"] > 0
    # No non-plan tool call → the tool_call sub-dict is empty.
    assert block["tool_call"] == {}


def test_component_tokens_total_is_sum(_no_tiktoken: None) -> None:
    messages = [
        _msg(role="user", text="a request from the user side here"),
        _msg(
            role="assistant",
            text="an assistant answer",
            thinking="reasoning trace",
            tool_use=(
                {"name": "Read", "input": "/etc/hosts", "tool_use_id": "r1"},
                {"name": "Bash", "input": "echo hi", "tool_use_id": "b1"},
            ),
        ),
    ]
    block = component_tokens(messages, agent="claude")
    assert block is not None
    scalar_sum = sum(block[f] for f in COMPONENT_FIELDS)
    tool_sum = sum(block["tool_call"].values())
    assert block["total"] == scalar_sum + tool_sum
    # Two distinct kinds present.
    assert set(block["tool_call"]) == {"read", "bash"}


def test_component_tokens_one_estimator_tiktoken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With tiktoken injected every component shares the tiktoken label."""
    fake = types.ModuleType("tiktoken")
    fake.get_encoding = lambda name: types.SimpleNamespace(  # type: ignore[attr-defined]
        encode=lambda s, disallowed_special=(): list(range(len(s)))
    )
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    monkeypatch.setattr(
        tokens_mod, "_ENCODER_STATE", {"loaded": False, "encoder": None}
    )
    messages = [
        _msg(role="user", text="hello"),
        _msg(role="assistant", text="world",
             tool_use=({"name": "Bash", "input": "ls"},)),
    ]
    block = component_tokens(messages, agent="claude")
    assert block is not None
    assert block["estimator"] == "tiktoken"


def test_component_tokens_all_empty_is_none(_no_tiktoken: None) -> None:
    assert component_tokens([], agent="claude") is None
    assert component_tokens([_msg(role="user", text="")], agent="claude") is None


def test_component_tokens_orphan_result_is_other(_no_tiktoken: None) -> None:
    """A tool_result with no matching tool_use_id → tool_call.other."""
    messages = [
        _msg(
            role="assistant",
            tool_result=(
                {"content": "orphan result text with no matching call",
                 "is_error": False, "tool_use_id": "no-such-id"},
            ),
        ),
    ]
    block = component_tokens(messages, agent="claude")
    assert block is not None
    assert "other" in block["tool_call"]
    assert block["tool_call"]["other"] > 0


def test_component_tokens_opencode_reasoning_is_thinking(_no_tiktoken: None) -> None:
    """OpenCode reasoning lands on Message.thinking → counted under thinking."""
    messages = [
        _msg(role="assistant", thinking="opencode reasoning part text"),
    ]
    block = component_tokens(messages, agent="opencode")
    assert block is not None
    assert block["thinking"] > 0
    assert block["assistant_turn"] == 0


def test_component_tokens_accepts_agent_enum(_no_tiktoken: None) -> None:
    """Passing the AgentName enum still detects plans (agent normalized)."""
    messages = [
        _msg(
            role="assistant",
            tool_use=(
                {"name": "ExitPlanMode", "input": {"plan": "# P\n\nstep"},
                 "tool_use_id": "p1"},
            ),
        ),
    ]
    block = component_tokens(messages, agent=AgentName.CLAUDE)
    assert block is not None
    assert block["plan"] > 0
    assert block["tool_call"] == {}


def test_estimate_session_total_equals_component_total(
    fake_codex_session: Path, _no_tiktoken: None
) -> None:
    """On an estimate-tier session: session_tokens total == component_tokens total."""
    session = codex.read_session("test-codex-1")
    parser_msgs = codex.read_messages("test-codex-1")

    block = session_tokens(session, messages=parser_msgs)
    assert block["source"] == "estimate"
    assert "categories" not in block
    # Flat block carries only TOKEN_FIELDS + source + estimator.
    assert set(block) == set(TOKEN_FIELDS) | {"source", "estimator"}

    comp = component_tokens(parser_msgs, agent=session.agent)
    assert comp is not None
    assert block["total"] == comp["total"]


def test_session_tokens_exact_shape_byte_identical(
    claude_token_session: str, _no_tiktoken: None
) -> None:
    """The exact-tier flat block has no categories/breakdown key."""
    session = claude.read_session(claude_token_session)
    block = session_tokens(session)
    assert block["source"] == "exact"
    assert block["total"] == 195
    assert "categories" not in block
    # Same keys as the historical exact block: TOKEN_FIELDS + source.
    assert set(block) == set(TOKEN_FIELDS) | {"source"}


def test_aggregate_component_tokens_fold(_no_tiktoken: None) -> None:
    """Fold component_tokens over 2 rows: summed scalars + tool union + provenance."""
    rows = [
        {"agent": "claude", "component_tokens": {
            "user_turn": 10, "assistant_turn": 20, "thinking": 5, "plan": 0,
            "tool_call": {"bash": 7, "read": 3},
            "total": 45, "source": "estimate", "estimator": "chars/4",
        }},
        {"agent": "claude", "component_tokens": {
            "user_turn": 4, "assistant_turn": 6,
            # no thinking / no plan keys on this row → None-not-0 honesty
            "tool_call": {"bash": 1, "edit": 9},
            "total": 21, "source": "estimate", "estimator": "chars/4",
        }},
    ]
    result = aggregate(rows, group_by="agent", metrics=["component_tokens"])
    block = result["groups"][0]["component_tokens"]
    assert block["user_turn"] == 14
    assert block["assistant_turn"] == 26
    assert block["thinking"] == 5      # only one row carried it
    assert block["plan"] == 0          # one row had 0, summed
    # Union of tool kinds, summed per kind.
    assert block["tool_call"] == {"bash": 8, "read": 3, "edit": 9}
    assert block["total"] == 14 + 26 + 5 + 0 + 8 + 3 + 9
    assert (block["estimated"], block["unknown"]) == (2, 0)
    assert block["source"] == "estimate"
    assert block["estimator"] == "chars/4"


def test_aggregate_component_tokens_none_not_fabricated() -> None:
    """A component no row carried stays absent (never a fabricated 0)."""
    rows = [
        {"agent": "claude", "component_tokens": {
            "user_turn": 3, "tool_call": {}, "total": 3,
            "source": "estimate", "estimator": "chars/4",
        }},
        {"agent": "claude"},  # no block → unknown
    ]
    result = aggregate(rows, group_by="agent", metrics=["component_tokens"])
    block = result["groups"][0]["component_tokens"]
    assert block["user_turn"] == 3
    # assistant_turn / thinking / plan absent — no row carried them.
    assert "assistant_turn" not in block
    assert "thinking" not in block
    assert "plan" not in block
    assert (block["estimated"], block["unknown"]) == (1, 1)


# ---------------------------------------------------------------------------
# rollup_component_tokens — parent + spawned children (NIT 2 / NIT 3)
# ---------------------------------------------------------------------------


def _cblock(**over: object) -> dict:
    """A minimal component_tokens-shaped block for rollup tests."""
    base = {
        "user_turn": 0, "assistant_turn": 0, "thinking": 0, "plan": 0,
        "tool_call": {}, "total": 0, "source": "estimate",
        "estimator": "chars/4",
    }
    base.update(over)  # type: ignore[arg-type]
    return base


def test_rollup_drops_parent_task_when_children_present() -> None:
    """NIT 2: a subagent's tokens must not be counted in parent AND child.

    The parent transcript records the spawn as a ``task`` tool call whose
    tokens (spawn input + returned report) are the SAME text the child block
    already accounts for in full.  With a child rolled up, the parent's
    ``task`` bucket is dropped so the report is counted exactly once.
    """
    parent = _cblock(
        user_turn=6, assistant_turn=2, tool_call={"task": 243}, total=251,
    )
    child = _cblock(user_turn=4, assistant_turn=229, total=233)

    rolled = rollup_component_tokens(parent, [child])

    # The child report (~229) is NOT double counted: parent task dropped.
    assert rolled["total"] == 251 - 243 + 233
    assert "task" not in rolled.get("tool_call", {})
    assert rolled["assistant_turn"] == 2 + 229
    assert (rolled["estimated"], rolled["unknown"]) == (2, 0)
    assert rolled["source"] == "estimate"


def test_rollup_keeps_parent_task_when_no_children() -> None:
    """A childless rollup keeps the parent ``task`` bucket (nothing else has it)."""
    parent = _cblock(user_turn=6, tool_call={"task": 243}, total=249)

    rolled = rollup_component_tokens(parent, [])

    assert rolled["tool_call"]["task"] == 243
    assert rolled["total"] == 249
    assert (rolled["estimated"], rolled["unknown"]) == (1, 0)


def test_rollup_preserves_non_task_parent_tool_calls() -> None:
    """Only the parent ``task`` bucket is dropped; real tool work survives."""
    parent = _cblock(
        user_turn=5, tool_call={"task": 100, "edit": 40}, total=145,
    )
    child = _cblock(assistant_turn=90, total=90)

    rolled = rollup_component_tokens(parent, [child])

    assert rolled["tool_call"] == {"edit": 40}
    assert rolled["total"] == 5 + 40 + 90


def test_rollup_all_unknown_total_is_none() -> None:
    """NIT 3: no usable block anywhere → total is None (unknown), never 0."""
    rolled = rollup_component_tokens(None, [None, None])

    assert rolled["total"] is None
    assert (rolled["estimated"], rolled["unknown"]) == (0, 3)
    assert "source" not in rolled


def test_rollup_partial_unknown_children() -> None:
    """A missing child block counts as ``unknown`` but never breaks the sum."""
    parent = _cblock(user_turn=10, total=10)
    good_child = _cblock(assistant_turn=7, total=7)

    rolled = rollup_component_tokens(parent, [good_child, None])

    assert rolled["total"] == 17
    assert (rolled["estimated"], rolled["unknown"]) == (2, 1)
