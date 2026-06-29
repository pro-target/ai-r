"""Tests for ``ai_r.session_stats`` core.

The grouping/ranking is the only new logic — the underlying inventory
(``list_sessions``) and the edit enrichment
(:func:`ai_r.find_file_edits.find_file_edits`) are exercised by their own
tests; this module covers the group-by dimensions, the ranking tie-break,
the ``totals`` block, argument validation, and the RISK-4 degenerate
``kind`` split flag.

All fixtures come from :mod:`tests.conftest` (the hermetic
``tmp_sessions_dir`` tree); layout mirrors ``tests/test_file_frequency``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.session_stats import GROUP_BY, group_key, session_stats


# ---------------------------------------------------------------------------
# Helpers (local — kept private to this module)
# ---------------------------------------------------------------------------


def _write_claude_edit_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    turns: list[tuple[str, str, str]],
    proj: str = "proj-ss",
    base_ts: str = "2026-06-14T10:0",
) -> None:
    """Write a Claude JSONL of (user_text, tool_name, file_path) edit turns."""
    records: list[dict] = []
    for i, (user_text, tool_name, file_path) in enumerate(turns):
        records.append(
            {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "timestamp": f"{base_ts}{i}:00Z",
                "sessionId": uuid,
            }
        )
        records.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"Editing {file_path}."},
                        {
                            "type": "tool_use",
                            "name": tool_name,
                            "input": {
                                "file_path": file_path,
                                "old_string": "a",
                                "new_string": "b",
                            },
                        },
                    ],
                },
                "timestamp": f"{base_ts}{i}:05Z",
                "sessionId": uuid,
            }
        )
    jsonl = tmp_sessions_dir / ".claude" / "projects" / proj / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _patch_claude(monkeypatch: pytest.MonkeyPatch, tmp_sessions_dir: Path) -> None:
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )


# ---------------------------------------------------------------------------
# group_key() — pure unit
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, agent_value, kind="agent", date=None, extra=None, mc=0):
        self.agent = type("A", (), {"value": agent_value})()
        self.kind = kind
        self.date = date
        self.extra = extra or {}
        self.message_count = mc


def test_group_key_dimensions() -> None:
    from datetime import datetime, timezone

    s = _FakeSession(
        "CLAUDE",
        kind="subagent",
        date=datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc),
        extra={"project_slug": "proj-x"},
    )
    assert group_key(s, "agent") == "claude"
    assert group_key(s, "kind") == "subagent"
    assert group_key(s, "dir") == "proj-x"
    assert group_key(s, "date") == "2026-06-14"


def test_group_key_dir_prefers_cwd_over_slug() -> None:
    s = _FakeSession("CODEX", extra={"cwd": "/tmp/work", "project_slug": "p"})
    assert group_key(s, "dir") == "/tmp/work"


def test_group_key_dir_unknown_fallback() -> None:
    s = _FakeSession("OPENCODE", extra={})
    assert group_key(s, "dir") == "(unknown)"


def test_group_key_date_undated() -> None:
    s = _FakeSession("CLAUDE", date=None)
    assert group_key(s, "date") == "(undated)"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_session_stats_unknown_group_by_raises() -> None:
    with pytest.raises(ValueError, match="group_by"):
        session_stats(group_by="mystery")


def test_session_stats_negative_top_raises() -> None:
    with pytest.raises(ValueError, match="top"):
        session_stats(top=-1)


def test_session_stats_bool_top_raises() -> None:
    with pytest.raises(ValueError, match="top"):
        session_stats(top=True)  # type: ignore[arg-type]


def test_session_stats_unknown_agent_raises() -> None:
    with pytest.raises(ValueError, match="agent"):
        session_stats(agent="mystery")


def test_session_stats_bad_since_raises() -> None:
    with pytest.raises(ValueError, match="since"):
        session_stats(since="not-a-date")


def test_group_by_set_is_complete() -> None:
    assert GROUP_BY == {"agent", "dir", "date", "kind"}


# ---------------------------------------------------------------------------
# Core integration (hermetic tree)
# ---------------------------------------------------------------------------


def test_session_stats_no_sessions_empty(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_claude(monkeypatch, tmp_sessions_dir)
    # Scope to claude: only that parser is patched onto the hermetic tree;
    # the other parsers resolve host-default paths (e.g. opencode's
    # ~/.local/share/opencode.db) which AI_R_HOME does not isolate, so a
    # cross-agent session *count* would be host-dependent.
    result = session_stats(group_by="agent", agent="claude")
    assert result["groups"] == []
    assert result["totals"]["sessions"] == 0
    assert result["totals"]["edits"] == 0
    assert result["totals"]["agents"] == 0
    assert result["totals"]["agents_list"] == []
    # No subagents => degenerate split flag + note.
    assert result["kind_split_available"] is False
    assert "note" in result


def test_session_stats_group_by_agent_counts(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two claude sessions, one editing two files, one editing one.
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ss-s1",
        turns=[
            ("change hot", "Edit", "/repo/hot.py"),
            ("change hot again", "Edit", "/repo/hot.py"),
            ("touch cold", "Write", "/repo/cold.py"),
        ],
    )
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ss-s2",
        turns=[("edit other", "Edit", "/repo/other.py")],
        proj="proj-ss2",
        base_ts="2026-06-14T11:0",
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)

    result = session_stats(group_by="agent", agent="claude")
    assert result["group_by"] == "agent"
    assert result["totals"]["sessions"] == 2
    assert result["totals"]["agents"] == 1
    assert result["totals"]["agents_list"] == ["claude"]
    # All edits roll up under the single claude bucket: 3 + 1 = 4.
    assert result["totals"]["edits"] == 4

    assert len(result["groups"]) == 1
    g = result["groups"][0]
    assert g["group"] == "claude"
    assert g["sessions"] == 2
    assert g["edits"] == 4
    assert g["agents"] == ["claude"]
    # Distinct intents across both sessions: 3 distinct user texts in s1
    # (each unique) + 1 in s2 = 4.
    assert g["intents"] == 4


def test_session_stats_top_truncates_groups_not_totals(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three distinct projects/dates => three date buckets.
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-d1",
        turns=[("a", "Edit", "/r/a.py")], base_ts="2026-06-10T10:0",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-d2",
        turns=[("b", "Edit", "/r/b.py")], proj="proj-d2",
        base_ts="2026-06-11T10:0",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-d3",
        turns=[("c", "Edit", "/r/c.py")], proj="proj-d3",
        base_ts="2026-06-12T10:0",
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="date", top=1, agent="claude")
    assert len(result["groups"]) == 1
    # totals reflect the full match set, not the truncated list
    assert result["totals"]["sessions"] == 3


def test_session_stats_kind_split(
    tmp_sessions_dir: Path,
    fake_claude_session: Path,
    fake_claude_subagent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fake_claude_session (top-level agent) + fake_claude_subagent (subagent)
    # both live under .claude/projects; point the parser at that tree.
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="kind", agent="claude")

    assert result["group_by"] == "kind"
    assert result["kind_split_available"] is True
    assert "note" not in result

    by_group = {g["group"]: g for g in result["groups"]}
    assert set(by_group) == {"agent", "subagent"}
    assert by_group["agent"]["sessions"] == 1
    assert by_group["subagent"]["sessions"] == 1
    assert result["totals"]["sessions"] == 2


def test_session_stats_kind_split_degenerate_flag(
    tmp_sessions_dir: Path,
    fake_claude_session: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only a top-level session, no subagents: split must flag itself as
    # degenerate so an auditor does not read it as "verified no subagents".
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="kind", agent="claude")

    assert result["kind_split_available"] is False
    assert "note" in result
    assert "subagent" in result["note"].lower()
    by_group = {g["group"]: g for g in result["groups"]}
    assert set(by_group) == {"agent"}


def test_session_stats_agent_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-af", turns=[("edit x", "Edit", "/r/x.py")],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="agent", agent="claude")
    assert result["totals"]["agents_list"] == ["claude"]
    assert result["totals"]["sessions"] == 1
