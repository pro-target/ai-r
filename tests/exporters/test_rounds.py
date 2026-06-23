"""Tests for the exporters.rounds module and the ``export rounds`` CLI."""
from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Tuple

from ai_r import cli as cli_module
from ai_r.exporters.rounds import session_to_rounds
from ai_r.parsers.models import AgentName, Message, Session


_ENV_KEYS = ("AI_R_HOME", "OPENCODE_DB")


def _run_inproc(
    argv: list[str], env: dict[str, str] | None = None
) -> Tuple[int, str, str]:
    saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    try:
        for k in _ENV_KEYS:
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


def _make_session(
    uuid: str = "abc-123",
    title: str = "Test session",
    date: datetime | None = None,
    extra: dict | None = None,
) -> Session:
    return Session(
        uuid=uuid,
        agent=AgentName.CLAUDE,
        title=title,
        date=date or datetime(2026, 6, 14, 10, 0, 0),
        path="/tmp/fake/abc-123.jsonl",
        message_count=0,
        extra=extra or {},
    )


def test_session_to_rounds_minimal() -> None:
    session = _make_session()
    out = session_to_rounds(session)
    assert "# Session: Test session" in out
    assert "**UUID**: abc-123" in out
    assert "**Agent**: CLAUDE" in out
    assert "**Date**: 2026-06-14" in out
    assert (
        "- 2026-06-14 \u2014 CLAUDE \u2014 Test session [non-actionable]" in out
    )
    assert "## Round:" not in out


def test_session_to_rounds_with_messages() -> None:
    session = _make_session()
    messages: list[Message] = [
        Message(
            role="user",
            text="Refactor the parser module",
            tool_use=(),
            tool_result=(),
        ),
        Message(
            role="assistant",
            text="Reading the current code now",
            tool_use=(
                {
                    "name": "Read",
                    "input": json.dumps(
                        {"file_path": "src/ai_r/parsers/claude.py"}
                    ),
                },
            ),
            tool_result=(),
        ),
        Message(
            role="assistant",
            text="Found a bug to fix",
            tool_use=(),
            tool_result=(),
        ),
        Message(
            role="assistant",
            text="Editing now",
            tool_use=(
                {
                    "name": "Edit",
                    "input": json.dumps(
                        {
                            "file_path": "src/ai_r/parsers/claude.py",
                            "old_string": "x",
                            "new_string": "y",
                        }
                    ),
                },
            ),
            tool_result=(),
        ),
        Message(
            role="user",
            text="Run the tests please",
            tool_use=(),
            tool_result=(),
        ),
    ]
    out = session_to_rounds(session, messages=messages)
    assert "## Round: Test session" in out
    assert "### Goal" in out
    assert "Refactor the parser module" in out
    assert "### Status" in out
    assert "in-progress" in out
    assert "### Files touched" in out
    assert "`src/ai_r/parsers/claude.py`" in out
    assert "### Decisions" in out
    assert "### Open" in out
    assert "Run the tests please" in out
    assert "### Next actions" in out
    assert "Editing now" in out
    assert "### Snapshot" in out


def test_cli_export_rounds_stdout(tmp_sessions_dir: Path) -> None:
    uuid = "test-export-1"
    path = (
        tmp_sessions_dir
        / ".claude"
        / "projects"
        / "proj-a"
        / f"{uuid}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "First message"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": uuid,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rc, out, err = _run_inproc(
        ["export", "rounds", uuid],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "# Session:" in out
    assert uuid in out
    assert "[non-actionable]" in out
    assert "## Round:" not in out


# ---------------------------------------------------------------------------
# Branch coverage (audit 2026-06-21): _render_round status, _extract_file_paths
# edge cases, _snapshot_line cost branch, and the untested ``--output`` flag.
# ---------------------------------------------------------------------------


def test_rounds_render_completed_status() -> None:
    """A session whose last message is ``assistant`` renders Status=completed."""
    session = _make_session()
    messages: list[Message] = [
        Message(role="user", text="do the thing", tool_use=(), tool_result=()),
        Message(role="assistant", text="did it", tool_use=(), tool_result=()),
    ]
    out = session_to_rounds(session, messages=messages)
    assert "### Status" in out
    assert "completed" in out


def test_rounds_extract_file_paths_invalid_json_skipped() -> None:
    """A tool_use whose ``input`` is unparseable JSON is skipped, not fatal."""
    session = _make_session()
    messages: list[Message] = [
        Message(role="user", text="go", tool_use=(), tool_result=()),
        Message(
            role="assistant",
            text="editing",
            tool_use=({"name": "Edit", "input": "{not json"},),
            tool_result=(),
        ),
    ]
    out = session_to_rounds(session, messages=messages)
    assert "### Files touched" in out
    assert "(none)" in out


def test_rounds_extract_file_paths_notebook_path() -> None:
    """``notebook_path`` (a _PATH_KEYS entry) is extracted for NotebookEdit."""
    session = _make_session()
    messages: list[Message] = [
        Message(role="user", text="go", tool_use=(), tool_result=()),
        Message(
            role="assistant",
            text="editing",
            tool_use=(
                {
                    "name": "NotebookEdit",
                    "input": json.dumps({"notebook_path": "nb/analysis.ipynb"}),
                },
            ),
            tool_result=(),
        ),
    ]
    out = session_to_rounds(session, messages=messages)
    assert "`nb/analysis.ipynb`" in out


def test_rounds_snapshot_cost_branch() -> None:
    """``_snapshot_line`` reports cost when present, ``n/a`` otherwise."""
    msgs: list[Message] = [
        Message(role="user", text="go", tool_use=(), tool_result=()),
    ]
    out_cost = session_to_rounds(_make_session(extra={"cost": 0.05}), messages=msgs)
    assert "### Snapshot" in out_cost
    assert "cost: 0.05" in out_cost

    out_na = session_to_rounds(_make_session(extra={}), messages=msgs)
    assert "### Snapshot\nn/a" in out_na


def test_cli_export_rounds_output_writes_file(
    tmp_sessions_dir: Path, tmp_path: Path
) -> None:
    """``export rounds UUID --output OUT`` writes the markdown to OUT."""
    uuid = "test-export-out"
    path = (
        tmp_sessions_dir
        / ".claude"
        / "projects"
        / "proj-a"
        / f"{uuid}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "First message"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": uuid,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out_file = tmp_path / "rounds.md"
    rc, _stdout, stderr = _run_inproc(
        ["export", "rounds", uuid, "--output", str(out_file)],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, stderr
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# Session:" in content
    assert uuid in content
