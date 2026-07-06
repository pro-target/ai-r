"""MCP ``read_session`` without ``agent`` (hermetic).

The ``agent`` parameter is optional: when omitted, the uuid is resolved
across every parser.  Session ids are unique across agents in practice;
the rare cross-agent collision returns a ``candidates`` list (NOT an
error) so the caller can re-ask with an explicit ``agent``.
"""
from __future__ import annotations

import json
from pathlib import Path

from ai_r import mcp_server


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_pi_session(home: Path, uuid: str) -> None:
    """Minimal Pi session with the given id under the fake home."""
    jsonl = (
        home / ".pi" / "agent" / "sessions" / "--tmp-work--"
        / f"2026-06-14T10-00-00-000Z_{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "session",
                "version": 3,
                "id": uuid,
                "timestamp": "2026-06-14T10:00:00.000Z",
                "cwd": "/tmp/work",
            },
            {
                "type": "message",
                "id": "user-1",
                "timestamp": "2026-06-14T10:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "pi says hi"}],
                    "timestamp": 1_718_360_002_000,
                },
            },
        ],
    )


def _write_claude_session(home: Path, uuid: str) -> None:
    jsonl = home / ".claude" / "projects" / "proj-a" / f"{uuid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "claude says hi"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": uuid,
            },
        ],
    )


def test_read_session_without_agent_resolves(fake_claude_session: Path) -> None:
    """Omitting ``agent`` finds the session by id across parsers."""
    out = mcp_server.read_session(uuid="test-claude-1")
    assert "error" not in out
    assert out["agent"] == "CLAUDE"
    assert out["uuid"] == "test-claude-1"
    assert out["messages"]


def test_read_session_without_agent_matches_explicit(
    fake_claude_session: Path,
) -> None:
    """Agent-free and explicit-agent reads return the same session."""
    free = mcp_server.read_session(uuid="test-claude-1")
    explicit = mcp_server.read_session(uuid="test-claude-1", agent="claude")
    assert free == explicit


def test_read_session_without_agent_not_found() -> None:
    out = mcp_server.read_session(uuid="no-such-session-xyz")
    assert out["error"] == "not_found"
    assert out["agent"] is None
    assert set(out["agents_scanned"]) == {
        "claude", "codex", "opencode", "antigravity", "pi",
    }


def test_read_session_explicit_agent_not_found_keeps_shape() -> None:
    """The historical single-agent not_found shape is preserved."""
    out = mcp_server.read_session(uuid="no-such-session-xyz", agent="claude")
    assert out["error"] == "not_found"
    assert out["agent"] == "CLAUDE"
    assert out["agents_scanned"] == ["claude"]


def test_read_session_id_collision_returns_candidates(
    tmp_sessions_dir: Path,
) -> None:
    """Same id under two agents → candidates list, NOT an error."""
    _write_claude_session(tmp_sessions_dir, "dup-id-1")
    _write_pi_session(tmp_sessions_dir, "dup-id-1")

    out = mcp_server.read_session(uuid="dup-id-1")
    assert "error" not in out
    assert out["ambiguous"] is True
    assert out["count"] == 2
    agents = {c["agent"] for c in out["candidates"]}
    assert agents == {"CLAUDE", "PI"}
    # Every candidate carries enough identity to re-ask with agent=…
    for cand in out["candidates"]:
        assert cand["uuid"] == "dup-id-1"


def test_read_session_collision_disambiguated_by_agent(
    tmp_sessions_dir: Path,
) -> None:
    _write_claude_session(tmp_sessions_dir, "dup-id-2")
    _write_pi_session(tmp_sessions_dir, "dup-id-2")

    out = mcp_server.read_session(uuid="dup-id-2", agent="pi")
    assert "error" not in out and "ambiguous" not in out
    assert out["agent"] == "PI"


def test_read_session_without_agent_malformed_uuid() -> None:
    """A uuid every parser rejects stays a structured invalid_argument."""
    out = mcp_server.read_session(uuid="../etc/passwd")
    assert out["error"] == "invalid_argument"
