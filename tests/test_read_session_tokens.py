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
# with_tokens=True — Claude (dedup, exact + categories)
# ---------------------------------------------------------------------------


def test_read_session_with_tokens_claude(
    claude_tokens_session: str, _no_tiktoken: None
) -> None:
    result = read_session(claude_tokens_session, agent="claude", with_tokens=True)
    assert "error" not in result

    # Session block: exact outer numbers + independent estimate categories.
    st = result["tokens"]
    assert st["source"] == "exact"
    assert st["total"] == 195  # (100+50+10+5) + (10+20)
    cats = st["categories"]
    assert cats is not None and cats["source"] == "estimate"
    assert cats["estimator"] == "chars/4"
    four = cats["text"] + cats["thinking"] + cats["tool_input"] + cats["tool_result"]
    assert four == cats["total"]

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
    assert st["categories"] is not None  # categories still estimated

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


def test_search_finds_claude_thinking_term(claude_thinking_session: str) -> None:
    """A term present only in a Claude thinking block is body-searchable now."""
    result = search_sessions(query="zephyrquux", agent="claude", scope="body")
    assert result["count"] == 1
    assert result["results"][0]["uuid"] == claude_thinking_session


def test_search_finds_opencode_reasoning_term(opencode_reasoning_db: str) -> None:
    """OpenCode reasoning moved text→thinking; body search still finds it."""
    result = search_sessions(query="wobblefrotz", agent="opencode", scope="body")
    assert result["count"] == 1
    assert result["results"][0]["uuid"] == opencode_reasoning_db
