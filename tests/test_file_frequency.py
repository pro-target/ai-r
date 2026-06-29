"""Tests for ``ai_r.file_frequency`` core and the ``file-frequency`` CLI.

The aggregation/ranking is the only new logic — the underlying scan
(:func:`ai_r.find_file_edits.find_file_edits`) is exercised by its own
tests; this module covers the group-by, the ranking tie-break, the
``total_*`` counters, argument validation, and the CLI rendering.

All fixtures come from :mod:`tests.conftest` (the hermetic
``tmp_sessions_dir`` tree); layout mirrors ``tests/test_find_tool_calls``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r import cli as cli_module
from ai_r.file_frequency import aggregate, file_frequency, rank


# ---------------------------------------------------------------------------
# Helpers (local — kept private to this module)
# ---------------------------------------------------------------------------


def _run_inproc(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run ``cli.main`` in-process; return ``(rc, stdout, stderr)``."""
    import contextlib
    import io
    import os

    saved_env = {k: os.environ.get(k) for k in ("AI_R_HOME", "OPENCODE_DB")}
    try:
        for k in ("AI_R_HOME", "OPENCODE_DB"):
            os.environ.pop(k, None)
        if env:
            os.environ.update(env)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = cli_module.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, stdout.getvalue(), stderr.getvalue()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_claude_edit_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    turns: list[tuple[str, str, str]],
    proj: str = "proj-ff",
    base_ts: str = "2026-06-14T10:0",
) -> None:
    """Write a Claude JSONL of (user_text, tool_name, file_path) edit turns.

    Each turn = one user msg followed by one assistant ``tool_use`` editing
    ``file_path``. Timestamps are spaced a minute apart so the scan keeps a
    deterministic order.
    """
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
# aggregate() — pure unit
# ---------------------------------------------------------------------------


def test_aggregate_rolls_up_by_file() -> None:
    records = [
        {"file": "/a.py", "session_uuid": "s1", "agent": "claude", "intent": "x"},
        {"file": "/a.py", "session_uuid": "s2", "agent": "codex", "intent": "y"},
        {"file": "/b.py", "session_uuid": "s1", "agent": "claude", "intent": "x"},
    ]
    by_file = aggregate(records)
    assert by_file["/a.py"]["edits"] == 2
    assert by_file["/a.py"]["sessions"] == {"s1", "s2"}
    assert by_file["/a.py"]["agents"] == {"claude", "codex"}
    assert by_file["/a.py"]["intents"] == {"x", "y"}
    assert by_file["/b.py"]["edits"] == 1


def test_aggregate_dedups_intents_and_skips_none() -> None:
    records = [
        {"file": "/a.py", "session_uuid": "s1", "agent": "claude", "intent": "same"},
        {"file": "/a.py", "session_uuid": "s1", "agent": "claude", "intent": " same "},
        {"file": "/a.py", "session_uuid": "s1", "agent": "claude", "intent": None},
    ]
    by_file = aggregate(records)
    assert by_file["/a.py"]["edits"] == 3
    # whitespace-stripped duplicate collapses; None ignored
    assert by_file["/a.py"]["intents"] == {"same"}


def test_aggregate_ignores_records_without_file() -> None:
    by_file = aggregate([{"session_uuid": "s1", "agent": "claude"}])
    assert by_file == {}


# ---------------------------------------------------------------------------
# rank() — ordering + truncation
# ---------------------------------------------------------------------------


def test_rank_orders_by_edits_then_sessions_then_path() -> None:
    by_file = {
        "/z.py": {"edits": 1, "sessions": {"s"}, "agents": set(), "intents": set()},
        "/a.py": {"edits": 3, "sessions": {"s"}, "agents": set(), "intents": set()},
        "/b.py": {
            "edits": 3,
            "sessions": {"s1", "s2"},
            "agents": set(),
            "intents": set(),
        },
    }
    ordered = [f for f, _ in rank(by_file, top=0)]
    # /b.py (3 edits, 2 sessions) before /a.py (3 edits, 1 session); /z.py last
    assert ordered == ["/b.py", "/a.py", "/z.py"]


def test_rank_top_truncates() -> None:
    by_file = {
        f"/f{i}.py": {"edits": i, "sessions": set(), "agents": set(), "intents": set()}
        for i in range(1, 6)
    }
    top2 = rank(by_file, top=2)
    assert len(top2) == 2
    assert [f for f, _ in top2] == ["/f5.py", "/f4.py"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_file_frequency_negative_top_raises() -> None:
    with pytest.raises(ValueError, match="top"):
        file_frequency(top=-1)


def test_file_frequency_bool_top_raises() -> None:
    with pytest.raises(ValueError, match="top"):
        file_frequency(top=True)  # type: ignore[arg-type]


def test_file_frequency_empty_path_raises() -> None:
    # Propagated from find_file_edits.
    with pytest.raises(ValueError, match="path"):
        file_frequency(path="")


def test_file_frequency_unknown_agent_raises() -> None:
    with pytest.raises(ValueError, match="agent"):
        file_frequency(agent="mystery")


def test_file_frequency_bad_since_raises() -> None:
    with pytest.raises(ValueError, match="since"):
        file_frequency(since="not-a-date")


# ---------------------------------------------------------------------------
# Core scan integration (hermetic tree)
# ---------------------------------------------------------------------------


def test_file_frequency_no_sessions_empty(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = file_frequency(path="/")
    assert result["files"] == []
    assert result["total_edits"] == 0
    assert result["total_files"] == 0
    assert result["total_sessions"] == 0
    assert result["total_agents"] == 0
    assert result["agents"] == []


def test_file_frequency_ranks_and_counts(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Session 1 edits hot.py twice (two distinct intents) + cold.py once.
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ff-s1",
        turns=[
            ("make hot change one", "Edit", "/repo/hot.py"),
            ("make hot change two", "Edit", "/repo/hot.py"),
            ("touch cold", "Write", "/repo/cold.py"),
        ],
    )
    # Session 2 edits hot.py once (same intent text as a session-1 turn).
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ff-s2",
        turns=[("make hot change one", "Edit", "/repo/hot.py")],
        proj="proj-ff2",
        base_ts="2026-06-14T11:0",
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)

    result = file_frequency(path="/repo/", top=8)
    assert result["total_edits"] == 4
    assert result["total_files"] == 2
    assert result["total_sessions"] == 2
    assert result["total_agents"] == 1
    assert result["agents"] == ["claude"]

    files = result["files"]
    assert [f["file"] for f in files] == ["/repo/hot.py", "/repo/cold.py"]
    hot = files[0]
    assert hot["edits"] == 3
    assert hot["sessions"] == 2
    # "make hot change one" (x2 across sessions) + "make hot change two" = 2
    assert hot["intents"] == 2
    assert hot["agents"] == ["claude"]
    cold = files[1]
    assert cold["edits"] == 1
    assert cold["sessions"] == 1


def test_file_frequency_top_truncates_files_not_totals(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ff-top",
        turns=[
            ("edit a", "Edit", "/r/a.py"),
            ("edit b", "Edit", "/r/b.py"),
            ("edit c", "Edit", "/r/c.py"),
        ],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = file_frequency(path="/r/", top=1)
    assert len(result["files"]) == 1
    # totals reflect the full match set, not the truncated list
    assert result["total_files"] == 3
    assert result["total_edits"] == 3


def test_file_frequency_agent_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ff-af",
        turns=[("edit x", "Edit", "/r/x.py")],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = file_frequency(path="/r/", agent="claude")
    assert result["total_edits"] == 1
    assert result["agents"] == ["claude"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_file_frequency_human(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ff-cli-h",
        turns=[
            ("hot one", "Edit", "/repo/hot.py"),
            ("hot two", "Edit", "/repo/hot.py"),
        ],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    rc, out, err = _run_inproc(
        ["file-frequency", "--path", "/repo/", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "/repo/hot.py" in out
    assert "scanned:" in out
    assert "agents" in out  # header line


def test_cli_file_frequency_json(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ff-cli-j",
        turns=[("edit it", "Edit", "/repo/file.py")],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    rc, out, err = _run_inproc(
        ["file-frequency", "--path", "/repo/", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["total_edits"] == 1
    assert payload["total_files"] == 1
    assert payload["files"][0]["file"] == "/repo/file.py"
    assert payload["files"][0]["agents"] == ["claude"]


def test_cli_file_frequency_no_match_stderr(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_claude(monkeypatch, tmp_sessions_dir)
    rc, out, err = _run_inproc(
        ["file-frequency", "--path", "/nope/"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert "no edits" in err.lower()


def test_cli_file_frequency_bad_iso_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, err = _run_inproc(
        ["file-frequency", "--since", "not-a-date"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "iso" in err.lower()


def test_cli_file_frequency_negative_top_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, err = _run_inproc(
        ["file-frequency", "--top", "-1"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "top" in err.lower()
