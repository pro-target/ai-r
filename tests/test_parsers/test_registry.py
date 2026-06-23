"""Tests for the cross-agent registry helpers.

Exercises the package-level :func:`find_sessions` and
:func:`read_session` dispatchers in :mod:`ai_reader.parsers`.  Each
test seeds a small number of synthetic Claude sessions into the
shared ``tmp_sessions_dir`` fixture (the autouse
``_isolate_ai_reader_home`` fixture redirects every parser there).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_reader.parsers import AgentName, Session, find_sessions, read_session


def _write_claude_session(
    home: Path, project: str, session_id: str, title: str
) -> Path:
    """Drop a minimal Claude JSONL into the fake projects tree."""
    path = home / ".claude" / "projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": title},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": session_id,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": session_id,
        },
    ]
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")
    return path


def test_ambiguous_query_returns_candidates(tmp_sessions_dir: Path) -> None:
    _write_claude_session(
        tmp_sessions_dir, "proj-amb", "ses-alpha", "deploy auth"
    )
    _write_claude_session(
        tmp_sessions_dir, "proj-amb", "ses-bravo", "deploy auth v2"
    )

    result = read_session("deploy auth")

    assert isinstance(result, list)
    assert len(result) == 2
    titles = {c["title"] for c in result}
    assert titles == {"deploy auth", "deploy auth v2"}
    for cand in result:
        assert set(cand.keys()) == {"uuid", "agent", "title", "mtime", "path"}
        assert cand["agent"] == AgentName.CLAUDE.value
        assert cand["mtime"]
        assert cand["mtime"][0].isdigit()


def test_unique_query_returns_session(tmp_sessions_dir: Path) -> None:
    _write_claude_session(
        tmp_sessions_dir, "proj-uni", "ses-solo", "deploy auth"
    )

    result = read_session("deploy auth")

    assert not isinstance(result, list)
    assert isinstance(result, Session)
    assert result.title == "deploy auth"
    assert result.uuid == "ses-solo"
    assert result.agent is AgentName.CLAUDE


def test_no_match_raises(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_session("nonexistent", agent="claude")


def test_find_sessions_empty_query_returns_empty(tmp_sessions_dir: Path) -> None:
    _write_claude_session(
        tmp_sessions_dir, "proj-e", "ses-e1", "anything"
    )
    assert find_sessions("") == []
    assert find_sessions("   ") == []


def test_read_session_empty_query_raises(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_session("")
    with pytest.raises(FileNotFoundError):
        read_session("   ")


def test_read_session_exact_uuid_unchanged(tmp_sessions_dir: Path) -> None:
    _write_claude_session(
        tmp_sessions_dir, "proj-uuid", "ses-known", "real title"
    )
    result = read_session("ses-known", agent="claude")
    assert isinstance(result, Session)
    assert result.uuid == "ses-known"


def test_find_sessions_unknown_agent_raises(tmp_sessions_dir: Path) -> None:
    with pytest.raises(ValueError):
        find_sessions("x", agent="bogus")


def test_find_sessions_agent_filter(tmp_sessions_dir: Path) -> None:
    _write_claude_session(
        tmp_sessions_dir, "proj-f", "ses-f1", "refactor module"
    )
    results = find_sessions("refactor", agent="claude")
    assert len(results) == 1
    assert results[0]["agent"] == "CLAUDE"
    assert results[0]["title"] == "refactor module"
    assert find_sessions("refactor", agent="codex") == []


def test_read_session_multi_candidate_across_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same uuid claimed by two agents returns the candidate list.

    Covers the ``len(exact) > 1`` branch of ``parsers.read_session``
    (``__init__.py:160-161``) that the same-agent ambiguity test does not
    reach.
    """
    from datetime import datetime

    from ai_reader.parsers import claude as claude_mod
    from ai_reader.parsers import codex as codex_mod

    shared = "ses-shared-uuid"
    fake_claude = Session(
        uuid=shared,
        agent=AgentName.CLAUDE,
        title="claude one",
        date=datetime(2026, 6, 14, 10, 0, 0),
        path="/tmp/claude.jsonl",
        message_count=0,
        extra={},
    )
    fake_codex = Session(
        uuid=shared,
        agent=AgentName.CODEX,
        title="codex one",
        date=datetime(2026, 6, 14, 10, 0, 0),
        path="/tmp/codex.jsonl",
        message_count=0,
        extra={},
    )
    monkeypatch.setattr(claude_mod, "read_session", lambda query, base_dir=None: fake_claude)
    monkeypatch.setattr(codex_mod, "read_session", lambda query, base_dir=None: fake_codex)

    result = read_session(shared)
    assert isinstance(result, list)
    assert len(result) == 2
    agents = {cand["agent"] for cand in result}
    assert agents == {"CLAUDE", "CODEX"}
