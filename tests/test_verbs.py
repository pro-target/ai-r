"""Phase-3a verbs: ``aggregate`` / ``diff`` / ``detect_current`` (parity).

The point of this module is *equivalence*: each new verb must reproduce the
output of the legacy tool it will (in Phase 3b) replace, so the migration is
provably safe.

* ``aggregate(session_rows, group_by=X)`` == ``session_stats(group_by=X)``
  (groups + totals), and ``aggregate(edit_records, group_by="file")`` ==
  ``file_frequency`` (per-file edits/sessions/intents/agents).
* ``diff(query(edit+write rows))`` produces the SAME per-file unified diff as
  ``session_diff``.
* ``detect_current()`` reflects the same cascade as
  ``detect_session_candidates`` / ``detect_agent`` under a controlled env.

Hermetic by default (autouse ``_isolate_ai_r_home``); two host-marked tests
prove parity on real data via ``real_claude_home`` and *skip* (never fail)
when the host carries no Claude sessions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from ai_r.events import aggregate, detect_current, diff, query
from ai_r.file_frequency import file_frequency
from ai_r.find_file_edits import find_file_edits
from ai_r.session_diff import session_diff
from ai_r.session_stats import session_stats

from ._parity_helpers import session_rows


# ---------------------------------------------------------------------------
# Parity helpers: build the SAME rows the legacy tools fold internally, so the
# comparison exercises ``aggregate`` on the real inputs (not a re-implementation).
# ---------------------------------------------------------------------------


def _edit_diff_rows(uuid: str, agent: str | None = None) -> List[dict[str, Any]]:
    """The edit/write/other tool_call rows for a session (what ``diff`` folds).

    ``session_diff`` treats EDIT_TOOLS + shell-exec redirects as edits; the
    corresponding events are the ``tool_call(edit)`` / ``tool_call(write)`` /
    ``tool_call(other)`` (codex shell) subtypes carrying a ``file`` ref.
    """
    rows: List[dict[str, Any]] = []
    for sub in ("edit", "write", "other", "bash"):
        for ev in query(type=f"tool_call({sub})", session=uuid, agent=agent):
            if any("file" in r for r in ev.get("refs", ())):
                rows.append(ev)
    return rows


def _write_claude_edit_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    turns: list[tuple[str, str, str]],
    proj: str = "proj-vb",
    base_ts: str = "2026-06-14T10:0",
) -> None:
    records: list[dict] = []
    for i, (user_text, tool_name, file_path) in enumerate(turns):
        records.append({
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": f"{base_ts}{i}:00Z",
            "sessionId": uuid,
        })
        records.append({
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
        })
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


def _assert_stats_parity(legacy: dict[str, Any], agg: dict[str, Any]) -> None:
    """Groups + shared totals match between session_stats and aggregate."""
    assert legacy["group_by"] == agg["group_by"]
    # Group rows: same order (both use the edits→sessions→count→label rank),
    # same numbers.  session_stats truncates to ``top``; call aggregate with
    # the full set and slice to compare fairly.
    assert legacy["groups"] == agg["groups"][: len(legacy["groups"])]
    # Shared totals keys.
    for key in ("sessions", "edits", "agents", "agents_list"):
        assert legacy["totals"][key] == agg["totals"][key], key


# ---------------------------------------------------------------------------
# aggregate — validation + generic behaviour
# ---------------------------------------------------------------------------


def test_aggregate_unknown_metric_raises() -> None:
    with pytest.raises(ValueError, match="metric"):
        aggregate([], group_by="agent", metrics=["bogus"])


def test_aggregate_empty_rows() -> None:
    r = aggregate([], group_by="agent", metrics=["count", "sessions"])
    assert r["groups"] == []
    assert r["totals"]["sessions"] == 0
    assert r["totals"]["agents"] == 0
    assert r["totals"]["agents_list"] == []


def test_aggregate_callable_group_by() -> None:
    rows = [{"x": 1}, {"x": 2}, {"x": 3}]
    r = aggregate(rows, group_by=lambda row: "odd" if row["x"] % 2 else "even",
                  metrics=["count"])
    by = {g["group"]: g["count"] for g in r["groups"]}
    assert by == {"odd": 2, "even": 1}


def test_aggregate_missing_key_buckets_unknown() -> None:
    r = aggregate([{"agent": ""}, {}], group_by="agent", metrics=["count"])
    assert r["groups"][0]["group"] == "(unknown)"
    assert r["groups"][0]["count"] == 2


def test_aggregate_edits_sum_vs_count() -> None:
    # Rows with an ``edits`` int SUM; rows without count as one edit each.
    summed = aggregate([{"edits": 3}, {"edits": 2}], group_by=lambda r: "g",
                       metrics=["edits"])
    assert summed["groups"][0]["edits"] == 5
    counted = aggregate([{"file": "a"}, {"file": "a"}], group_by="file",
                        metrics=["edits"])
    assert counted["groups"][0]["edits"] == 2


# ---------------------------------------------------------------------------
# aggregate — PARITY with session_stats (all group_by dimensions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group_by", ["agent", "dir", "date", "kind"])
def test_aggregate_parity_session_stats(
    group_by: str,
    tmp_sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir, "vb-s1",
        turns=[
            ("change hot", "Edit", "/repo/hot.py"),
            ("change hot again", "Edit", "/repo/hot.py"),
            ("touch cold", "Write", "/repo/cold.py"),
        ],
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "vb-s2",
        turns=[("edit other", "Edit", "/repo/other.py")],
        proj="proj-vb2", base_ts="2026-06-14T11:0",
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)

    legacy = session_stats(group_by=group_by, agent="claude", top=0)
    agg = aggregate(
        session_rows(agent="claude"),
        group_by=group_by,
        metrics=["sessions", "edits", "intents", "agents", "messages"],
    )
    _assert_stats_parity(legacy, agg)


def test_aggregate_parity_session_stats_kind_split(
    tmp_sessions_dir: Path,
    fake_claude_session: Path,
    fake_claude_subagent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_claude(monkeypatch, tmp_sessions_dir)
    legacy = session_stats(group_by="kind", agent="claude", top=0)
    agg = aggregate(
        session_rows(agent="claude"),
        group_by="kind",
        metrics=["sessions", "edits", "intents", "agents", "messages"],
    )
    _assert_stats_parity(legacy, agg)
    # Both see the agent + subagent buckets.
    assert {g["group"] for g in agg["groups"]} == {"agent", "subagent"}


# ---------------------------------------------------------------------------
# aggregate — PARITY with file_frequency (group_by=file)
# ---------------------------------------------------------------------------


def test_aggregate_parity_file_frequency(
    tmp_sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir, "vb-f1",
        turns=[
            ("change hot", "Edit", "/repo/hot.py"),
            ("change hot again", "Edit", "/repo/hot.py"),
            ("touch cold", "Write", "/repo/cold.py"),
        ],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)

    legacy = file_frequency(path="/", agent="claude", top=0)
    records = find_file_edits(path="/", agent="claude", limit=0)["records"]
    agg = aggregate(
        records,
        group_by="file",
        metrics=["edits", "sessions", "intents", "agents"],
    )
    # Same per-file rows (edits/sessions/intents/agents), same rank order.
    legacy_files = [
        {"file": f["file"], "edits": f["edits"], "sessions": f["sessions"],
         "intents": f["intents"], "agents": f["agents"]}
        for f in legacy["files"]
    ]
    agg_files = [
        {"file": g["group"], "edits": g["edits"], "sessions": g["sessions"],
         "intents": g["intents"], "agents": g["agents"]}
        for g in agg["groups"]
    ]
    assert legacy_files == agg_files
    assert legacy["total_edits"] == agg["totals"]["edits"]
    assert legacy["total_sessions"] == agg["totals"]["sessions"]


# ---------------------------------------------------------------------------
# diff — PARITY with session_diff
# ---------------------------------------------------------------------------


def _write_multi_edit_session(tmp_sessions_dir: Path, uuid: str, edit_path: str) -> None:
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-vd" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user",
         "message": {"role": "user", "content": "Rename foo to bar"},
         "timestamp": "2026-06-14T10:00:00Z", "sessionId": uuid},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Editing."},
             {"type": "tool_use", "name": "Edit", "input": {
                 "file_path": edit_path, "old_string": "foo", "new_string": "bar"}},
         ]},
         "timestamp": "2026-06-14T10:00:05Z", "sessionId": uuid},
        {"type": "user",
         "message": {"role": "user", "content": "Now add a docstring"},
         "timestamp": "2026-06-14T10:01:00Z", "sessionId": uuid},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Edit", "input": {
                 "file_path": edit_path, "old_string": "def bar():",
                 "new_string": 'def bar():\n    """doc"""'}},
         ]},
         "timestamp": "2026-06-14T10:01:05Z", "sessionId": uuid},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Write", "input": {
                 "file_path": edit_path, "content": "def bar():\n    return 42\n"}},
         ]},
         "timestamp": "2026-06-14T10:02:00Z", "sessionId": uuid},
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_diff_parity_session_diff(tmp_sessions_dir: Path) -> None:
    uuid = "vb-diff-1"
    edit_path = "/repo/src/mod.py"
    _write_multi_edit_session(tmp_sessions_dir, uuid, edit_path)

    legacy = session_diff(uuid, "claude")
    new = diff(_edit_diff_rows(uuid, agent="claude"))

    assert new["count"] == legacy["count"] == 1
    assert new["caveats"] == legacy["caveats"]

    lf = legacy["files"][0]
    nf = new["files"][0]
    assert nf["file"] == lf["file"] == edit_path
    # The stitched, chronological unified diff is byte-identical.
    assert nf["diff"] == lf["diff"]
    # Same ordered edits (timestamp/tool/hunks).
    assert [e["tool"] for e in nf["edits"]] == [e["tool"] for e in lf["edits"]]
    assert [e["timestamp"] for e in nf["edits"]] == [
        e["timestamp"] for e in lf["edits"]
    ]
    assert [e["hunks"] for e in nf["edits"]] == [e["hunks"] for e in lf["edits"]]


def test_diff_format_validation() -> None:
    with pytest.raises(ValueError, match="format"):
        diff([], format="context")


def test_diff_empty_rows_has_caveats() -> None:
    r = diff([])
    assert r["count"] == 0
    assert r["files"] == []
    assert len(r["caveats"]) == 2


def test_diff_skips_rows_without_file_ref() -> None:
    # A bash tool_call with no file ref must not create a phantom file.
    r = diff([{"id": "s:0", "refs": [{"tool": "Bash"}], "ts": None}])
    assert r["count"] == 0


# ---------------------------------------------------------------------------
# detect_current — parity with the detect cascade under a controlled env
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_detect_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Blank every env var + flag dir the detect cascade reads."""
    for var in (
        "AI_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
        "OPENCODE_SESSION_ID", "AGENT_NAME", "AI_AGENT", "CODING_AGENT",
        "CODEX_HOME", "CLAUDECODE", "OPENCODE", "AI_SESSION_OUTPUT",
    ):
        monkeypatch.delenv(var, raising=False)
    # Point the flag-file base at an empty temp dir so no host flag leaks in.
    monkeypatch.setenv("AI_R_SESSION_IDENTITY_DIR", str(tmp_path / "identity"))


def test_detect_current_empty(_clean_detect_env: None) -> None:
    r = detect_current()
    assert r["session_id"] is None
    assert r["agent"] is None
    assert r["candidates"] == []
    assert r["verified"] is False
    assert r["self"] is False


def test_detect_current_ai_session_id(
    _clean_detect_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_SESSION_ID", "abcdef123456")
    r = detect_current()
    assert r["session_id"] == "abcdef123456"
    assert r["candidates"][0]["source"] == "AI_SESSION_ID"
    assert r["verified"] is True


def test_detect_current_per_agent_env(
    _clean_detect_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    r = detect_current()
    assert r["session_id"] == sid
    assert r["agent"] == "claude"
    assert r["candidates"][0]["source"] == "CLAUDE_CODE_SESSION_ID"


def test_detect_current_matches_cascade(
    _clean_detect_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verb's candidates mirror detect_session_candidates exactly."""
    from ai_r.session import detect_session_candidates

    monkeypatch.setenv("AI_SESSION_ID", "sess12345")
    monkeypatch.setenv("CODEX_THREAD_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    r = detect_current()
    cascade = detect_session_candidates()
    assert [c["id"] for c in r["candidates"]] == [c.session_id for c in cascade]
    assert [c["source"] for c in r["candidates"]] == [c.source for c in cascade]


def test_detect_current_agent_hint_when_no_session_agent(
    _clean_detect_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AI_SESSION_ID carries no agent context; the hint fills the reported agent.
    monkeypatch.setenv("AI_SESSION_ID", "sess12345")
    r = detect_current(agent="codex")
    assert r["session_id"] == "sess12345"
    assert r["agent"] == "codex"


def test_detect_current_unknown_agent_hint_raises(_clean_detect_env: None) -> None:
    with pytest.raises(ValueError):
        detect_current(agent="not-an-agent")


# ---------------------------------------------------------------------------
# Host-marked parity on REAL data (skips when no host sessions)
# ---------------------------------------------------------------------------


def test_aggregate_parity_session_stats_host(frozen_claude_home: Path) -> None:
    """aggregate == session_stats on a FROZEN snapshot of ~/.claude.

    Frozen (not live): both sides re-scan the vault, and the LIVE
    ``~/.claude`` mutates between the two scans (most acutely the session
    the test itself runs inside, which the harness is actively writing) —
    that produced false parity mismatches.  Same rationale as
    ``tests/test_phase3b_parity.py``; still host-marked, still skips when
    the host has no Claude data.
    """
    legacy = session_stats(group_by="agent", agent="claude", top=0)
    agg = aggregate(
        session_rows(agent="claude"),
        group_by="agent",
        metrics=["sessions", "edits", "intents", "agents", "messages"],
    )
    _assert_stats_parity(legacy, agg)


def test_diff_parity_session_diff_host(real_claude_home: Path) -> None:
    """diff == session_diff on the first real claude session that has edits."""
    # Find a real session with at least one edit so the parity is meaningful.
    edits = find_file_edits(path="/", agent="claude", limit=200)["records"]
    uuid = next((r["session_uuid"] for r in edits if r.get("session_uuid")), None)
    if uuid is None:
        pytest.skip("no real claude session with edits on this host")
    legacy = session_diff(uuid, "claude")
    new = diff(_edit_diff_rows(uuid, agent="claude"))
    legacy_by_file = {f["file"]: f["diff"] for f in legacy["files"]}
    new_by_file = {f["file"]: f["diff"] for f in new["files"]}
    # Every file session_diff reconstructs, diff reconstructs identically.
    for path, legacy_diff in legacy_by_file.items():
        assert path in new_by_file, path
        assert new_by_file[path] == legacy_diff, path


# ---------------------------------------------------------------------------
# MCP wrappers (registration + error-dict contract)
# ---------------------------------------------------------------------------


def test_mcp_tools_registered() -> None:
    from ai_r.mcp_server import mcp

    internals = set(mcp._tool_manager._tools.keys())
    assert {"aggregate", "diff", "detect_current"} <= internals


def test_mcp_aggregate_happy_and_error() -> None:
    from ai_r.mcp_server import aggregate as mcp_aggregate

    ok = mcp_aggregate(
        rows=[{"agent": "claude", "session_uuid": "s1"}],
        group_by="agent",
        metrics=["sessions"],
    )
    assert "error" not in ok
    assert ok["groups"][0]["group"] == "claude"

    err = mcp_aggregate(rows=[], group_by="agent", metrics=["nope"])
    assert err["error"] == "invalid_argument"


def test_mcp_aggregate_default_metrics() -> None:
    from ai_r.mcp_server import aggregate as mcp_aggregate

    r = mcp_aggregate(rows=[{"agent": "claude"}, {"agent": "claude"}], group_by="agent")
    assert r["groups"][0]["count"] == 2


def test_mcp_diff_happy_and_error(tmp_sessions_dir: Path) -> None:
    from ai_r.mcp_server import diff as mcp_diff

    uuid = "vb-mcp-diff-1"
    _write_multi_edit_session(tmp_sessions_dir, uuid, "/repo/src/mod.py")
    ok = mcp_diff(rows=_edit_diff_rows(uuid, agent="claude"))
    assert "error" not in ok
    assert ok["count"] == 1

    err = mcp_diff(rows=[], format="context")
    assert err["error"] == "invalid_argument"


def test_mcp_detect_current_error(_clean_detect_env: None) -> None:
    from ai_r.mcp_server import detect_current as mcp_detect

    err = mcp_detect(agent="not-an-agent")
    assert err["error"] == "invalid_argument"
    ok = mcp_detect()
    assert "error" not in ok
    assert ok["session_id"] is None
