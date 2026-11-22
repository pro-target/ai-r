"""Tests for ``ai_r usage`` — the mentions/uses rollup per needle.

``usage`` composes two existing cores: ``search_sessions`` (mentions —
sessions whose title/body mentions the needle) and ``find_tool_calls``
(uses — tool calls whose input mentions the needle).  This module covers
the composition: the needle validation, the agent/project/days filters,
and the CLI surface.  All fixtures are hermetic Claude JSONL trees (the
``tmp_sessions_dir`` + ``_resolve_base_dir`` monkeypatch pattern from
``tests/test_find_tool_calls``).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_r import cli as cli_module
from ai_r.usage import usage


# ---------------------------------------------------------------------------
# Helpers (local — kept private to this module)
# ---------------------------------------------------------------------------


def _run_inproc(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run ``cli.main`` in-process; return ``(rc, stdout, stderr)``.

    Mirrors the helper used in ``test_cli`` / ``test_find_tool_calls`` so
    the coverage lines for the new command count toward the report.
    """
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
            except SystemExit as exc:  # argparse calls sys.exit
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, stdout.getvalue(), stderr.getvalue()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fresh_ts() -> str:
    """A timestamp one hour ago — safely inside any ``--days >= 1``."""
    return (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _isolate(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point every parser that ignores ``AI_R_HOME`` away from the host tree.

    Claude honours ``AI_R_HOME`` but the tests patch its projects dir
    explicitly anyway (the pattern from ``test_find_tool_calls``).
    OpenCode resolves its DB from the well-known native/snap paths —
    NOT from ``AI_R_HOME`` — so without this patch a cross-agent
    ``usage()`` on a dev host sees the real 600+ session store.
    """
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    monkeypatch.setattr(
        "ai_r.parsers.opencode._resolve_db_paths",
        lambda base_dir=None, override=None: [],
    )


def _write_claude_session(
    home: Path,
    uuid: str,
    *,
    user_text: str,
    cwd: str = "/home/u/dev/proj-alpha",
    ts: str = "2026-06-14T10:00:00Z",
    tool_calls: list[tuple[str, dict]] | None = None,
) -> None:
    """Write a Claude JSONL session: one user msg + optional tool calls.

    ``tool_calls`` is a list of ``(tool_name, input)`` pairs, each on its
    own assistant turn.  The record-level ``cwd`` drives the session's
    ``project_dir`` (the ``--project`` filter's haystack).
    """
    records: list[dict] = [
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": ts,
            "sessionId": uuid,
            "cwd": cwd,
        }
    ]
    for i, (name, tool_input) in enumerate(tool_calls or [], start=1):
        records.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"Calling {name}."},
                        {"type": "tool_use", "name": name, "input": tool_input},
                    ],
                },
                "timestamp": ts.replace("00:00", f"00:{10 + i:02d}"),
                "sessionId": uuid,
                "cwd": cwd,
            }
        )
    jsonl = home / ".claude" / "projects" / "proj-usage" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# Core: mentions vs uses
# ---------------------------------------------------------------------------


def test_mention_without_use_counts_mention_only(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Needle in the user text, no tool input mentioning it → 1 mention,
    0 uses (conversation about it, never used)."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-mention",
        user_text="please look at the memory-chunker skill",
        tool_calls=[("Bash", {"command": "ls /tmp"})],
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    result = usage("memory-chunker")
    assert result["mentions"] == 1
    assert result["uses"] == 0
    assert result["by_agent"]["claude"]["mentions"] == 1
    assert result["by_agent"]["claude"]["uses"] == 0


def test_use_counts_tool_calls_not_sessions(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tool calls whose input mentions the needle in ONE session →
    mentions=1 (the session), uses=2 (the calls)."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-uses",
        user_text="do the thing",
        tool_calls=[
            ("Read", {"file_path": "/x/memory-chunker/SKILL.md"}),
            ("Bash", {"command": "grep -r memory-chunker /x"}),
        ],
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    result = usage("memory-chunker")
    assert result["mentions"] == 1
    assert result["uses"] == 2


def test_agent_filter_zeroes_other_agents(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agent="codex"`` scans only the (empty) Codex tree → all zeroes,
    and the by-agent rollup names only codex."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-af",
        user_text="check memory-chunker",
        tool_calls=[("Bash", {"command": "memory-chunker --help"})],
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    result = usage("memory-chunker", agent="codex")
    assert result["mentions"] == 0
    assert result["uses"] == 0
    assert set(result["by_agent"]) == {"codex"}


def test_project_filter_is_substring_of_project_dir(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions in different project dirs; ``project="alpha"`` keeps
    only the one whose ``project_dir`` contains the substring."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-proj-a",
        user_text="open memory-chunker",
        cwd="/home/u/dev/alpha",
    )
    _write_claude_session(
        tmp_sessions_dir,
        "u-proj-b",
        user_text="open memory-chunker",
        cwd="/home/u/dev/beta",
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    result = usage("memory-chunker", project="alpha")
    assert result["mentions"] == 1
    assert result["by_agent"]["claude"]["sessions"] == 1


def test_days_filter_drops_old_sessions(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``days=7`` keeps the fresh session, drops the 2020 one."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-fresh",
        user_text="run memory-chunker",
        ts=_fresh_ts(),
    )
    _write_claude_session(
        tmp_sessions_dir,
        "u-old",
        user_text="run memory-chunker",
        ts="2020-01-01T10:00:00Z",
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    result = usage("memory-chunker", days=7)
    assert result["mentions"] == 1
    assert result["by_agent"]["claude"]["sessions"] == 1


def test_blank_needle_raises_value_error(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank needle is a loud argument error, not a full-corpus scan."""
    _isolate(tmp_sessions_dir, monkeypatch)
    with pytest.raises(ValueError):
        usage("   ")
    with pytest.raises(ValueError):
        usage("memory-chunker", days=0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_usage_human(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ai-r usage <needle>`` prints the needle | mentions | uses line
    plus the per-agent breakdown (no --agent given)."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-cli-h",
        user_text="inspect memory-chunker",
        tool_calls=[("Bash", {"command": "ls memory-chunker"})],
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    rc, out, err = _run_inproc(
        ["usage", "memory-chunker"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "memory-chunker" in out
    assert "1" in out  # mentions
    assert "claude" in out  # per-agent breakdown


def test_cli_usage_json(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ai-r usage <needle> --json`` emits a machine-readable payload."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-cli-j",
        user_text="inspect memory-chunker",
        tool_calls=[("Bash", {"command": "ls memory-chunker"})],
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    rc, out, err = _run_inproc(
        ["usage", "memory-chunker", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["needle"] == "memory-chunker"
    assert payload["mentions"] == 1
    assert payload["uses"] == 1
    assert payload["by_agent"]["claude"]["uses"] == 1


def test_cli_usage_blank_needle_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank needle is an argument error: exit 2, message on stderr."""
    _isolate(tmp_sessions_dir, monkeypatch)
    rc, out, err = _run_inproc(
        ["usage", "  "],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "ai-r:" in err
    assert not out.strip()


def test_cli_usage_agent_flag(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--agent codex`` restricts the scan; with the flag given, no
    per-agent breakdown is printed (the filter already names it)."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-cli-a",
        user_text="inspect memory-chunker",
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    rc, out, err = _run_inproc(
        ["usage", "memory-chunker", "--agent", "codex"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload_lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(payload_lines) == 1  # just the summary line


def test_cli_usage_days_flag(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--days`` keeps only fresh sessions."""
    _write_claude_session(
        tmp_sessions_dir,
        "u-cli-d",
        user_text="run memory-chunker",
        ts=_fresh_ts(),
    )
    _write_claude_session(
        tmp_sessions_dir,
        "u-cli-d-old",
        user_text="run memory-chunker",
        ts="2020-01-01T10:00:00Z",
    )
    _isolate(tmp_sessions_dir, monkeypatch)
    rc, out, err = _run_inproc(
        ["usage", "memory-chunker", "--days", "7", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["mentions"] == 1
    assert payload["filters"]["days"] == 7
