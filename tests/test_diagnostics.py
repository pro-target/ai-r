"""Empty-result diagnostics tests (hermetic).

Zero-result responses of the scanning methods (``query`` /
``search_sessions`` / ``find_tool_calls`` / ``find_file_edits`` /
``list_sessions``) must carry a ``diagnostics`` dict explaining WHAT was
scanned (per-agent session counts, corpus date bounds) and WHY nothing
matched (missing source dir, all-excluding date filter, remaining
filters).  Non-empty responses must NOT carry it.

All tests run against the fake ``AI_R_HOME`` tree (autouse
``_isolate_ai_r_home``) and scope to ``agent="claude"`` where a count is
asserted — the OpenCode parser can leak the real host DB (documented
host-leak), so cross-agent totals are never asserted exactly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_r import mcp_server
from ai_r.diagnostics import empty_result_diagnostics
from ai_r.find_file_edits import find_file_edits
from ai_r.find_tool_calls import find_tool_calls
from ai_r.parsers import PARSERS, AgentName


def _by_agent(diag: dict) -> dict[str, dict]:
    return {e["agent"]: e for e in diag["scanned"]}


# ---------------------------------------------------------------------------
# The diagnostics builder itself
# ---------------------------------------------------------------------------


def test_builder_missing_source_dir(tmp_path: Path) -> None:
    """No Claude dir at all → source_found False + a 'source not found' hint."""
    diag = empty_result_diagnostics(agent="claude")
    entry = _by_agent(diag)["claude"]
    assert entry["sessions"] == 0
    assert entry["source_found"] is False
    assert "source not found" in entry["hint"]
    assert diag["corpus"]["sessions"] == 0
    assert any("no sessions found" in h for h in diag["hints"])


def test_builder_counts_and_date_bounds(fake_claude_session: Path) -> None:
    diag = empty_result_diagnostics(agent="claude")
    entry = _by_agent(diag)["claude"]
    assert entry["sessions"] == 1
    assert entry["source_found"] is True
    assert "hint" not in entry
    assert entry["date_min"].startswith("2026-06-14")
    assert entry["date_max"].startswith("2026-06-14")
    assert diag["corpus"]["sessions"] == 1
    assert diag["corpus"]["date_min"].startswith("2026-06-14")


def test_builder_scans_all_agents_when_unfiltered(
    fake_claude_session: Path,
) -> None:
    diag = empty_result_diagnostics()
    agents = set(_by_agent(diag))
    assert agents == {"claude", "codex", "opencode", "antigravity", "pi"}


def test_builder_since_excludes_corpus_hint(fake_claude_session: Path) -> None:
    diag = empty_result_diagnostics(agent="claude", since="2030-01-01")
    assert any(
        "since" in h and "excludes the entire corpus" in h
        for h in diag["hints"]
    )


def test_builder_until_excludes_corpus_hint(fake_claude_session: Path) -> None:
    diag = empty_result_diagnostics(agent="claude", until="2020-01-01")
    assert any(
        "until" in h and "excludes the entire corpus" in h
        for h in diag["hints"]
    )


def test_builder_generic_filter_hint_names_filters(
    fake_claude_session: Path,
) -> None:
    diag = empty_result_diagnostics(
        agent="claude", filters={"path": "nope.py", "skipped": None}
    )
    # None-valued filters are dropped from the echo; active ones are kept.
    assert diag["filters"]["path"] == "nope.py"
    assert "skipped" not in diag["filters"]
    assert any("path=" in h for h in diag["hints"])


# ---------------------------------------------------------------------------
# Wiring: find_file_edits / find_tool_calls cores
# ---------------------------------------------------------------------------


def test_find_file_edits_empty_carries_diagnostics(
    fake_claude_session: Path,
) -> None:
    result = find_file_edits(path="no-such-file-xyz", agent="claude")
    assert result["count"] == 0 and result["records"] == []
    diag = result["diagnostics"]
    assert _by_agent(diag)["claude"]["sessions"] == 1
    assert diag["filters"]["path"] == "no-such-file-xyz"
    assert diag["hints"]


def test_find_tool_calls_empty_carries_diagnostics(
    fake_claude_session_with_tools: Path,
) -> None:
    result = find_tool_calls(tool_name="NoSuchTool", agent="claude")
    assert result["count"] == 0
    diag = result["diagnostics"]
    assert _by_agent(diag)["claude"]["sessions"] == 1
    assert diag["filters"]["tool_name"] == "NoSuchTool"
    assert diag["hints"]


def test_find_tool_calls_date_excluded_hint(
    fake_claude_session_with_tools: Path,
) -> None:
    # Without the date bound this matches one Bash call; with an
    # after-the-corpus ``since`` it matches nothing and must say why.
    assert find_tool_calls(tool_name="Bash", agent="claude")["count"] == 1
    result = find_tool_calls(
        tool_name="Bash", agent="claude", since="2030-01-01"
    )
    assert result["count"] == 0
    assert any(
        "excludes the entire corpus" in h
        for h in result["diagnostics"]["hints"]
    )


def test_find_tool_calls_nonempty_has_no_diagnostics(
    fake_claude_session_with_tools: Path,
) -> None:
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert result["count"] == 1
    assert "diagnostics" not in result


def test_find_file_edits_nonempty_has_no_diagnostics(
    tmp_sessions_dir: Path,
) -> None:
    import json as _json

    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-a" / "edit-1.jsonl"
    )
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "fix app.py"},
            "timestamp": "2026-06-14T10:00:00Z",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": "/repo/app.py",
                            "old_string": "a",
                            "new_string": "b",
                        },
                    }
                ],
            },
            "timestamp": "2026-06-14T10:00:05Z",
        },
    ]
    with jsonl.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(_json.dumps(rec) + "\n")
    result = find_file_edits(path="app.py", agent="claude")
    assert result["count"] == 1
    assert "diagnostics" not in result


# ---------------------------------------------------------------------------
# Wiring: MCP surface (query / search_sessions / list_sessions)
# ---------------------------------------------------------------------------


def test_mcp_query_empty_carries_diagnostics(
    fake_claude_session: Path,
) -> None:
    out = mcp_server.query(agent="claude", type="plan_event")
    assert out["count"] == 0 and out["events"] == []
    diag = out["diagnostics"]
    assert _by_agent(diag)["claude"]["sessions"] == 1
    assert diag["filters"]["type"] == "plan_event"
    assert diag["hints"]


def test_mcp_query_nonempty_has_no_diagnostics(
    fake_claude_session: Path,
) -> None:
    out = mcp_server.query(agent="claude", type="user_turn")
    assert out["count"] >= 1
    assert "diagnostics" not in out


def test_mcp_search_sessions_empty_carries_diagnostics(
    fake_claude_session: Path,
) -> None:
    out = mcp_server.search_sessions(query="zzz-no-such-title", agent="claude")
    assert out["count"] == 0 and out["results"] == []
    diag = out["diagnostics"]
    assert _by_agent(diag)["claude"]["sessions"] == 1
    assert diag["filters"]["query"] == "zzz-no-such-title"


def test_mcp_search_sessions_nonempty_has_no_diagnostics(
    fake_claude_session: Path,
) -> None:
    out = mcp_server.search_sessions(query="hello", agent="claude")
    assert out["count"] == 1
    assert "diagnostics" not in out


def test_mcp_list_sessions_empty_carries_diagnostics() -> None:
    out = mcp_server.list_sessions(agent="claude")
    assert out["total"] == 0
    diag = out["diagnostics"]
    entry = _by_agent(diag)["claude"]
    assert entry["source_found"] is False
    assert "source not found" in entry["hint"]


def test_mcp_list_sessions_nonempty_has_no_diagnostics(
    fake_claude_session: Path,
) -> None:
    out = mcp_server.list_sessions(agent="claude")
    assert out["total"] == 1
    assert "diagnostics" not in out


# ---------------------------------------------------------------------------
# No-rescan: diagnostics reuse the caller's scan instead of re-listing
# ---------------------------------------------------------------------------


@pytest.fixture()
def claude_list_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every ``list_sessions()`` call on the Claude parser."""
    parser = PARSERS[AgentName.CLAUDE]
    calls = {"n": 0}
    orig = parser.list_sessions

    def counted(*args: object, **kwargs: object):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(parser, "list_sessions", counted)
    return calls


def _fake_sessions() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(date=datetime(2026, 6, 10, tzinfo=timezone.utc)),
        SimpleNamespace(date=datetime(2026, 6, 20, tzinfo=timezone.utc)),
    ]


def test_builder_provided_sessions_skip_list_sessions(
    fake_claude_session: Path, claude_list_calls: dict[str, int]
) -> None:
    """Passed scan stats → aggregates come from them, zero re-listing."""
    diag = empty_result_diagnostics(
        agent="claude", scanned_sessions={"claude": _fake_sessions()}
    )
    assert claude_list_calls["n"] == 0
    entry = _by_agent(diag)["claude"]
    assert entry["sessions"] == 2
    assert entry["date_min"].startswith("2026-06-10")
    assert entry["date_max"].startswith("2026-06-20")
    assert entry["source_found"] is True  # cheap dir probe still runs
    assert diag["corpus"]["sessions"] == 2


def test_builder_accepts_agentname_keys(
    fake_claude_session: Path, claude_list_calls: dict[str, int]
) -> None:
    diag = empty_result_diagnostics(
        agent="claude",
        scanned_sessions={AgentName.CLAUDE: _fake_sessions()},
    )
    assert claude_list_calls["n"] == 0
    assert _by_agent(diag)["claude"]["sessions"] == 2


def test_builder_provided_empty_list_is_not_a_rescan(
    fake_claude_session: Path, claude_list_calls: dict[str, int]
) -> None:
    """An explicitly provided EMPTY list means 'scanned, found none'."""
    diag = empty_result_diagnostics(
        agent="claude", scanned_sessions={"claude": []}
    )
    assert claude_list_calls["n"] == 0
    entry = _by_agent(diag)["claude"]
    assert entry["sessions"] == 0
    assert "hint" in entry


def test_builder_rescans_only_when_nothing_provided(
    fake_claude_session: Path, claude_list_calls: dict[str, int]
) -> None:
    """No stats passed → the documented fallback re-scan happens."""
    diag = empty_result_diagnostics(agent="claude")
    assert claude_list_calls["n"] == 1
    assert _by_agent(diag)["claude"]["sessions"] == 1


def test_mcp_query_empty_scans_corpus_once(
    fake_claude_session: Path, claude_list_calls: dict[str, int]
) -> None:
    out = mcp_server.query(agent="claude", type="plan_event")
    assert out["count"] == 0 and "diagnostics" in out
    assert claude_list_calls["n"] == 1


def test_mcp_search_sessions_empty_scans_corpus_once(
    fake_claude_session: Path, claude_list_calls: dict[str, int]
) -> None:
    out = mcp_server.search_sessions(query="zzz-no-such-title", agent="claude")
    assert out["count"] == 0 and "diagnostics" in out
    assert claude_list_calls["n"] == 1


def test_mcp_list_sessions_empty_scans_corpus_once(
    claude_list_calls: dict[str, int],
) -> None:
    out = mcp_server.list_sessions(agent="claude")
    assert out["total"] == 0 and "diagnostics" in out
    assert claude_list_calls["n"] == 1


def test_find_file_edits_empty_scans_corpus_once(
    fake_claude_session: Path, claude_list_calls: dict[str, int]
) -> None:
    result = find_file_edits(path="no-such-file-xyz", agent="claude")
    assert result["count"] == 0 and "diagnostics" in result
    assert claude_list_calls["n"] == 1


def test_find_tool_calls_empty_scans_corpus_once(
    fake_claude_session_with_tools: Path, claude_list_calls: dict[str, int]
) -> None:
    result = find_tool_calls(tool_name="NoSuchTool", agent="claude")
    assert result["count"] == 0 and "diagnostics" in result
    assert claude_list_calls["n"] == 1
