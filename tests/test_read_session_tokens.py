"""F3.3 ``read_session(with_tokens=...)`` MCP-surface tests — Step 4.

Hermetic: every fixture writes synthetic session data under the per-test
``AI_R_HOME`` (auto-set by the conftest hermetic-env fixture); nothing
touches the real host vault.  The estimator is pinned to the degraded
``chars/4`` branch via ``_no_tiktoken`` so category counts are deterministic
regardless of whether tiktoken is installed in the environment.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_r import tokens as tokens_mod
from ai_r.mcp_server import read_session, search_sessions


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


@pytest.fixture
def _no_tiktoken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tokens_mod, "_ENCODER_STATE", {"loaded": True, "encoder": None}
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_tokens_session(tmp_sessions_dir: Path) -> str:
    """Claude JSONL: a streamed call (2 dup records) + a distinct call."""
    sid = "rs-claude-tok"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-a" / f"{sid}.jsonl"
    usage_1 = {
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
    }
    _write_jsonl(
        jsonl,
        [
            {"type": "user",
             "message": {"role": "user", "content": "count my tokens"},
             "timestamp": "2026-06-14T10:00:00Z", "sessionId": sid},
            # First (thinking-only) record of a streamed call: dropped by
            # projection's ``if not content: continue`` when the projected
            # content is empty — so the block must ride the NEXT survivor.
            {"type": "assistant", "requestId": "req-1",
             "message": {"id": "msg-1", "role": "assistant",
                         "content": [{"type": "thinking", "thinking": "hmm"}],
                         "usage": usage_1},
             "timestamp": "2026-06-14T10:00:05Z", "sessionId": sid},
            # Second (text) record of the SAME call: identical usage/_call.
            {"type": "assistant", "requestId": "req-1",
             "message": {"id": "msg-1", "role": "assistant",
                         "content": [{"type": "text", "text": "part two"}],
                         "usage": usage_1},
             "timestamp": "2026-06-14T10:00:06Z", "sessionId": sid},
            # A distinct call.
            {"type": "assistant", "requestId": "req-2",
             "message": {"id": "msg-2", "role": "assistant",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 10, "output_tokens": 20}},
             "timestamp": "2026-06-14T10:00:10Z", "sessionId": sid},
        ],
    )
    return sid


@pytest.fixture
def codex_tokens_session(tmp_sessions_dir: Path) -> str:
    """Codex rollout with a cumulative ``token_count`` (session-only usage)."""
    uuid = "rs-codex-tok"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T10-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {"timestamp": "2026-06-14T10:00:00Z", "type": "session_meta",
             "payload": {"id": uuid, "cwd": "/tmp/work",
                         "timestamp": "2026-06-14T10:00:00Z"}},
            {"timestamp": "2026-06-14T10:00:02Z", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "text", "text": "hello codex"}]}},
            {"timestamp": "2026-06-14T10:00:03Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "text", "text": "hi back"}]}},
            {"timestamp": "2026-06-14T10:00:04Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage": {
                 "input_tokens": 200, "cached_input_tokens": 150,
                 "output_tokens": 40, "reasoning_output_tokens": 7,
                 "total_tokens": 240}}}},
        ],
    )
    return uuid


# ---------------------------------------------------------------------------
# with_tokens=True — Claude (dedup, exact flat + component estimate)
# ---------------------------------------------------------------------------


def test_read_session_with_tokens_claude(
    claude_tokens_session: str, _no_tiktoken: None
) -> None:
    result = read_session(claude_tokens_session, agent="claude", with_tokens=True)
    assert "error" not in result

    # Session block: flat exact numbers, no categories/breakdown key.
    st = result["tokens"]
    assert st["source"] == "exact"
    assert st["total"] == 195  # (100+50+10+5) + (10+20)
    assert "categories" not in st

    # Separate per-component estimate breakdown.
    comp = result["component_tokens"]
    assert comp is not None and comp["source"] == "estimate"
    assert comp["estimator"] == "chars/4"
    assert isinstance(comp["tool_call"], dict)

    # Per-message: exactly ONE block per API call; ``_call`` never leaks.
    with_tokens = [m for m in result["messages"] if "tokens" in m]
    assert len(with_tokens) == 2  # call req-1 (once) + call req-2
    for m in with_tokens:
        assert "_call" not in m["tokens"]
        assert m["tokens"]["source"] != "estimate"  # per-message = exact
    # The streamed req-1 block rode the surviving text record ("part two"),
    # not the dropped thinking-only first record.
    totals = sorted(m["tokens"]["total"] for m in with_tokens)
    assert totals == [30, 165]  # req-2 = 10+20; req-1 = 100+50+10+5


# ---------------------------------------------------------------------------
# with_tokens=True — Codex (session-only, no per-message)
# ---------------------------------------------------------------------------


def test_read_session_with_tokens_codex(
    codex_tokens_session: str, _no_tiktoken: None
) -> None:
    result = read_session(codex_tokens_session, agent="codex", with_tokens=True)
    assert "error" not in result

    st = result["tokens"]
    assert st["source"] == "exact"
    assert st["total"] == 240
    assert "categories" not in st
    # Component breakdown attached separately, always an estimate.
    assert result["component_tokens"]["source"] == "estimate"

    # Codex records no per-message usage → NO ``tokens`` key on any entry
    # (absent, not null).
    assert all("tokens" not in m for m in result["messages"])


# ---------------------------------------------------------------------------
# with_tokens=False — byte-identical historical output
# ---------------------------------------------------------------------------


def test_read_session_without_tokens_unchanged(
    claude_tokens_session: str,
) -> None:
    base = read_session(claude_tokens_session, agent="claude")
    explicit_false = read_session(
        claude_tokens_session, agent="claude", with_tokens=False
    )
    assert base == explicit_false
    assert "tokens" not in base
    assert "component_tokens" not in base
    assert "subagent_rollup" not in base
    assert all("tokens" not in m for m in base["messages"])


# ---------------------------------------------------------------------------
# invalid with_tokens → invalid_argument-style error
# ---------------------------------------------------------------------------


def test_read_session_with_tokens_invalid_type(
    claude_tokens_session: str,
) -> None:
    result = read_session(
        claude_tokens_session, agent="claude", with_tokens="yes"  # type: ignore[arg-type]
    )
    assert result["error"] == "invalid_argument"
    assert "with_tokens" in result["message"]


# ---------------------------------------------------------------------------
# include_subagents — parent + spawned child rollup
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_parent_with_child(tmp_sessions_dir: Path) -> str:
    """A Claude parent session plus one spawned subagent child under it."""
    parent = "rs-parent-1"
    slug = "proj-sub"
    proj = tmp_sessions_dir / ".claude" / "projects" / slug
    _write_jsonl(
        proj / f"{parent}.jsonl",
        [
            {"type": "user",
             "message": {"role": "user", "content": "spawn a subagent please"},
             "timestamp": "2026-06-14T10:00:00Z", "sessionId": parent},
            {"type": "assistant",
             "message": {"id": "pm1", "role": "assistant",
                         "content": [{"type": "text", "text": "on it"}]},
             "timestamp": "2026-06-14T10:00:05Z", "sessionId": parent},
        ],
    )
    # Subagent child: directory form ``<parent-uuid>/subagents/agent-*.jsonl``.
    child = "agent-child-1"
    _write_jsonl(
        proj / parent / "subagents" / f"{child}.jsonl",
        [
            {"type": "user",
             "message": {"role": "user", "content": "do the subtask"},
             "timestamp": "2026-06-14T10:00:06Z", "sessionId": child,
             "isSidechain": True, "parentUuid": parent},
            {"type": "assistant",
             "message": {"id": "cm1", "role": "assistant",
                         "content": [{"type": "text", "text": "subtask done"}]},
             "timestamp": "2026-06-14T10:00:07Z", "sessionId": child,
             "isSidechain": True, "parentUuid": parent},
        ],
    )
    return parent


def test_read_session_include_subagents_rollup(
    claude_parent_with_child: str, _no_tiktoken: None
) -> None:
    result = read_session(
        claude_parent_with_child, agent="claude", include_subagents=True
    )
    assert "error" not in result
    rollup = result["subagent_rollup"]
    assert rollup["parent"] is not None
    assert rollup["parent"]["source"] == "estimate"
    # Exactly one spawned child was found and rolled up.
    assert len(rollup["children"]) == 1
    child = rollup["children"][0]
    assert child["agent"] == "claude"
    assert child["component_tokens"]["source"] == "estimate"
    # The folded total sums parent + child.
    total = rollup["total"]
    assert total["source"] == "estimate"
    assert total["total"] == (
        rollup["parent"]["total"] + child["component_tokens"]["total"]
    )
    assert total["estimated"] == 2 and total["unknown"] == 0


def test_read_session_include_subagents_childless(
    claude_tokens_session: str, _no_tiktoken: None
) -> None:
    """A parent with no spawned children → empty children, total == parent."""
    result = read_session(
        claude_tokens_session, agent="claude", include_subagents=True
    )
    rollup = result["subagent_rollup"]
    assert rollup["children"] == []
    assert rollup["total"]["total"] == rollup["parent"]["total"]


def test_read_session_include_subagents_invalid_type(
    claude_tokens_session: str,
) -> None:
    result = read_session(
        claude_tokens_session, agent="claude",
        include_subagents="yes",  # type: ignore[arg-type]
    )
    assert result["error"] == "invalid_argument"
    assert "include_subagents" in result["message"]


# ---------------------------------------------------------------------------
# pagination: per-message dedup decided PRE-slice
# ---------------------------------------------------------------------------


def test_read_session_dedup_is_pre_slice(
    claude_tokens_session: str, _no_tiktoken: None
) -> None:
    """The first record of req-1 (thinking-only) is dropped by projection;
    the surviving 'part two' entry carries req-1's block.  With an offset
    that pages PAST that entry, the block is NOT re-emitted on the later
    'done' entry — dedup is decided on absolute positions before the slice.
    """
    full = read_session(claude_tokens_session, agent="claude", with_tokens=True)
    # Projected order: [user, "part two" (req-1 block), "done" (req-2 block)].
    assert full["messages"][1]["content"] == "part two"
    assert full["messages"][1]["tokens"]["total"] == 165
    assert full["messages"][2]["tokens"]["total"] == 30

    # Page to the last message only.  Its block is req-2's, unchanged; the
    # req-1 block is not "re-homed" onto it because dedup ran pre-slice.
    paged = read_session(
        claude_tokens_session, agent="claude", offset=2, limit=1, with_tokens=True
    )
    assert len(paged["messages"]) == 1
    assert paged["messages"][0]["content"] == "done"
    assert paged["messages"][0]["tokens"]["total"] == 30


# ---------------------------------------------------------------------------
# haystack regression: body/search now matches reasoning (thinking) for ALL
# agents — a term that lives ONLY in reasoning must be found.
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_thinking_session(tmp_sessions_dir: Path) -> str:
    """Claude session whose only occurrence of a term is in a thinking block."""
    sid = "rs-claude-think"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-a" / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "user",
             "message": {"role": "user", "content": "plain question"},
             "timestamp": "2026-06-14T10:00:00Z", "sessionId": sid},
            {"type": "assistant",
             "message": {"id": "m1", "role": "assistant", "content": [
                 {"type": "thinking", "thinking": "the secret is zephyrquux"},
                 {"type": "text", "text": "visible answer"}]},
             "timestamp": "2026-06-14T10:00:05Z", "sessionId": sid},
        ],
    )
    return sid


@pytest.fixture
def opencode_reasoning_db(tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """OpenCode DB whose only occurrence of a term is in a reasoning part."""
    db = tmp_sessions_dir / "opencode-reasoning.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        """
    )
    conn.execute("INSERT INTO session VALUES ('oc-reason', NULL, 'plain title', 1, 2)")
    conn.execute(
        "INSERT INTO message VALUES ('rm1', 'oc-reason', 2, 2, ?)",
        (json.dumps({"role": "assistant"}),),
    )
    conn.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("rm1-p0", "rm1", "oc-reason", 2, 2,
             json.dumps({"type": "reasoning", "text": "musing about wobblefrotz"})),
            ("rm1-p1", "rm1", "oc-reason", 3, 3,
             json.dumps({"type": "text", "text": "visible reply"})),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    return "oc-reason"


def test_search_excludes_claude_thinking_term_by_default(claude_thinking_session: str) -> None:
    """Thinking is OUT of body search by default (budget); opt-in re-includes it."""
    # Default: a term living only in a Claude thinking block is NOT matched.
    result = search_sessions(query="zephyrquux", agent="claude", scope="body")
    assert result["count"] == 0
    # Opt-in: include_thinking=True surfaces the reasoning term again.
    result = search_sessions(
        query="zephyrquux", agent="claude", scope="body", include_thinking=True
    )
    assert result["count"] == 1
    assert result["results"][0]["uuid"] == claude_thinking_session


def test_search_excludes_opencode_reasoning_term_by_default(opencode_reasoning_db: str) -> None:
    """OpenCode reasoning (text→thinking) is out of body search unless opted in."""
    result = search_sessions(query="wobblefrotz", agent="opencode", scope="body")
    assert result["count"] == 0
    result = search_sessions(
        query="wobblefrotz", agent="opencode", scope="body", include_thinking=True
    )
    assert result["count"] == 1
    assert result["results"][0]["uuid"] == opencode_reasoning_db


# ---------------------------------------------------------------------------
# Q2: the event-level ``has_thinking`` flag (discovery hint, never the text)
# ---------------------------------------------------------------------------


@pytest.fixture
def pi_thinking_session(tmp_sessions_dir: Path) -> str:
    """Pi session whose assistant carries a thinking block beside its text."""
    uuid = "rs-pi-think"
    jsonl = (
        tmp_sessions_dir / ".pi" / "agent" / "sessions" / "--tmp-work--"
        / f"2026-06-14T10-00-00-000Z_{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {"type": "session", "id": uuid,
             "timestamp": "2026-06-14T10:00:00.000Z", "cwd": "/tmp/work"},
            {"type": "message", "message": {
                "role": "user",
                "content": [{"type": "text", "text": "plain question"}],
                "timestamp": 1_718_360_002_000}},
            {"type": "message", "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "pondering quibblewax"},
                    {"type": "text", "text": "visible reply"},
                ],
                "timestamp": 1_718_360_004_000}},
        ],
    )
    return uuid


def test_has_thinking_true_when_assistant_reasoned_claude(
    claude_thinking_session: str,
) -> None:
    """Claude assistant turn with a thinking block → ``has_thinking`` True;
    the user turn (no reasoning) carries no flag."""
    from ai_r.events import query

    rows = query(session=claude_thinking_session, agent="claude")
    asst = next(r for r in rows if r["type"] == "assistant_turn")
    assert asst["has_thinking"] is True
    user = next(r for r in rows if r["type"] == "user_turn")
    # A bare True marker, never False → no-signal turns keep the base shape.
    assert "has_thinking" not in user


def test_has_thinking_true_opencode(opencode_reasoning_db: str) -> None:
    from ai_r.events import query

    rows = query(session=opencode_reasoning_db, agent="opencode")
    asst = next(r for r in rows if r["type"] == "assistant_turn")
    assert asst["has_thinking"] is True


def test_has_thinking_true_pi(pi_thinking_session: str) -> None:
    from ai_r.events import query

    rows = query(session=pi_thinking_session, agent="pi")
    asst = next(r for r in rows if r["type"] == "assistant_turn")
    assert asst["has_thinking"] is True


def test_has_thinking_only_on_reasoning_assistant_turn(
    claude_thinking_session: str,
) -> None:
    """Within the reasoning session, the flag lands on exactly one row — the
    reasoning assistant turn; the user turn stays flag-free."""
    from ai_r.events import query

    rows = query(session=claude_thinking_session, agent="claude")
    flagged = [r for r in rows if r.get("has_thinking")]
    assert len(flagged) == 1 and flagged[0]["type"] == "assistant_turn"


def test_has_thinking_false_for_tool_call_and_plain_turns(
    fake_claude_session_with_tools: object,
) -> None:
    """A session with a plain assistant turn + a tool_call (no reasoning
    anywhere) → no row carries ``has_thinking`` (user, assistant, tool_call
    all flag-free)."""
    from ai_r.events import query

    rows = query(session="claude-tools-1", agent="claude")
    kinds = {r["type"] for r in rows}
    assert "assistant_turn" in kinds
    assert any(t.startswith("tool_call") for t in kinds)
    assert not any(r.get("has_thinking") for r in rows)


@pytest.fixture
def antigravity_no_thinking_brain(tmp_sessions_dir: Path) -> str:
    """Antigravity brain with a plain user/model exchange — no reasoning
    channel exists in the format, so ``has_thinking`` is always False."""
    uuid = "rs-ag-think"
    brain = tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / uuid
    (brain / ".system_generated" / "logs").mkdir(parents=True)
    _write_jsonl(
        brain / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {"timestamp": "2026-06-14T10:00:00Z", "source": "USER_EXPLICIT",
             "type": "USER_INPUT", "content": "set up the lab"},
            {"timestamp": "2026-06-14T10:00:05Z", "source": "MODEL",
             "type": "MODEL_OUTPUT", "content": "lab ready"},
        ],
    )
    return uuid


def test_has_thinking_false_antigravity(
    antigravity_no_thinking_brain: str,
) -> None:
    from ai_r.events import query

    rows = query(session=antigravity_no_thinking_brain, agent="antigravity")
    assert rows  # the exchange produced events
    assert not any(r.get("has_thinking") for r in rows)


def test_query_has_thinking_filter(claude_thinking_session: str) -> None:
    """``has_thinking=True/False`` gates the event stream tri-state."""
    from ai_r.events import query

    only_thinking = query(session=claude_thinking_session, has_thinking=True)
    assert only_thinking and all(
        r["type"] == "assistant_turn" for r in only_thinking
    )
    no_thinking = query(session=claude_thinking_session, has_thinking=False)
    assert no_thinking and all(not r.get("has_thinking") for r in no_thinking)
    # The two partitions are disjoint and cover the whole stream.
    everything = query(session=claude_thinking_session)
    assert len(only_thinking) + len(no_thinking) == len(everything)


# ---------------------------------------------------------------------------
# Q2: read_session(include_thinking=...) — reasoning as a SEPARATE field
# ---------------------------------------------------------------------------


def test_read_session_thinking_absent_by_default(
    claude_thinking_session: str,
) -> None:
    """Default: NO ``thinking`` key on any projected message (byte-identical
    historical shape); the reasoning text never contaminates ``content``."""
    result = read_session(claude_thinking_session, agent="claude")
    assert "error" not in result
    assert all("thinking" not in m for m in result["messages"])
    asst = next(m for m in result["messages"] if m["role"] == "assistant")
    assert "zephyrquux" not in asst["content"]


def test_read_session_include_thinking_adds_separate_field(
    claude_thinking_session: str,
) -> None:
    """``include_thinking=True``: the reasoning arrives as a string ``thinking``
    field ALONGSIDE ``content`` (never folded into content)."""
    result = read_session(
        claude_thinking_session, agent="claude", include_thinking=True
    )
    assert "error" not in result
    asst = next(m for m in result["messages"] if m["role"] == "assistant")
    assert isinstance(asst["thinking"], str)
    assert "zephyrquux" in asst["thinking"]
    # Content stays the historical text — reasoning is not inlined.
    assert "zephyrquux" not in asst["content"]
    assert asst["content"] == "visible answer"


def test_read_session_include_thinking_invalid_type(
    claude_thinking_session: str,
) -> None:
    result = read_session(
        claude_thinking_session, agent="claude",
        include_thinking="yes",  # type: ignore[arg-type]
    )
    assert result["error"] == "invalid_argument"
    assert "include_thinking" in result["message"]


# ---------------------------------------------------------------------------
# Q2: get_body(include_thinking=...) — reasoning re-read on demand
# ---------------------------------------------------------------------------


def test_get_body_turn_thinking_opt_in(claude_thinking_session: str) -> None:
    """A turn body has no ``thinking`` by default; ``include_thinking=True``
    re-reads the model reasoning from the hosting message."""
    from ai_r.events import query
    from ai_r.mcp_server import get_body

    rows = query(session=claude_thinking_session, agent="claude")
    asst = next(r for r in rows if r["type"] == "assistant_turn")

    default = get_body(asst["id"])
    assert "error" not in default
    assert "thinking" not in default

    opted = get_body(asst["id"], include_thinking=True)
    assert isinstance(opted["thinking"], str)
    assert "zephyrquux" in opted["thinking"]
    # The turn text is unchanged by the flag.
    assert opted["text"] == default["text"]


def test_get_body_include_thinking_invalid_type(
    claude_thinking_session: str,
) -> None:
    from ai_r.events import query
    from ai_r.mcp_server import get_body

    rows = query(session=claude_thinking_session, agent="claude")
    asst = next(r for r in rows if r["type"] == "assistant_turn")
    result = get_body(asst["id"], include_thinking="yes")  # type: ignore[arg-type]
    assert result["error"] == "invalid_argument"
    assert "include_thinking" in result["message"]


# ---------------------------------------------------------------------------
# Q2: haystack cache is not poisoned across include_thinking modes
# ---------------------------------------------------------------------------


def test_search_cache_not_poisoned_across_thinking_modes(
    claude_thinking_session: str,
) -> None:
    """Both build modes are valid at ONE mtime: a default search primes the
    cache WITHOUT reasoning, then an ``include_thinking=True`` search on the
    SAME session still finds the reasoning-only term (distinct cache key, not
    a stale default hit).  Order-independent: default first, then opt-in."""
    # Prime the cache in default mode: reasoning term is not matched.
    first = search_sessions(query="zephyrquux", agent="claude", scope="body")
    assert first["count"] == 0
    # Same session, same mtime, opt-in mode: the reasoning term IS found.
    second = search_sessions(
        query="zephyrquux", agent="claude", scope="body", include_thinking=True
    )
    assert second["count"] == 1
    assert second["results"][0]["uuid"] == claude_thinking_session
    # And the default mode still returns 0 afterwards (opt-in did not poison
    # the default-keyed entry).
    third = search_sessions(query="zephyrquux", agent="claude", scope="body")
    assert third["count"] == 0
