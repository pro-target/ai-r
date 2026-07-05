"""End-to-end CLI tests, driven through ``subprocess``.

Why subprocess and not ``cli.main(argv)`` directly?  The CLI is the
executable surface that ships to operators, so testing the *real*
binary entry point — ``python -m ai_r.cli`` — catches issues
that in-process testing would miss: missing ``__future__`` imports,
``sys.path`` munging, ``argparse`` quirks, the works.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ai_r import cli as cli_module
from ai_r import __version__
from ai_r.cli.commands.detect_session import _pick_single, _run_detect_session
from ai_r.parsers.models import AgentName
from ai_r.session import AmbiguousSessionError, SessionCandidate


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """Invoke ``python -m ai_r.cli`` with the given args.

    The autouse ``_isolate_ai_r_home`` fixture sets
    ``AI_R_HOME`` in the *test* process; the subprocess would
    inherit it and look at an empty fake tree.  We explicitly strip
    the variable from the child environment unless the caller asked
    for it.
    """
    cmd = [sys.executable, "-m", "ai_r.cli", *args]
    full_env = os.environ.copy()
    full_env.pop("AI_R_HOME", None)
    full_env.pop("OPENCODE_DB", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
    )


def _first_claude_uuid() -> str | None:
    """Pick a real session uuid from the local Claude tree (if any)."""
    base = Path("~/.claude/projects").expanduser()
    if not base.is_dir():
        return None
    for jsonl in base.glob("*/*.jsonl"):
        return jsonl.stem
    return None


def _write_claude_session(home: Path, uuid: str, title: str) -> None:
    path = home / ".claude" / "projects" / "proj-a" / f"{uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": title},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": uuid,
            }
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# In-process helper — runs ``cli.main`` in this process so coverage
# lines count toward the report.
# ---------------------------------------------------------------------------


_ENV_KEYS = (
    "AI_R_HOME",
    "OPENCODE_DB",
)


def _run_inproc(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run ``cli.main(argv)`` in-process; return (rc, stdout, stderr)."""
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
            except SystemExit as exc:  # argparse calls sys.exit
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, stdout.getvalue(), stderr.getvalue()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Version / help
# ---------------------------------------------------------------------------


def test_cli_version() -> None:
    p = _run_cli("--version")
    assert p.returncode == 0, p.stderr
    assert "ai-r" in p.stdout
    assert __version__ in p.stdout


def test_module_invocation() -> None:
    """``python -m ai_r --version`` exits 0 (module entry point works)."""
    p = subprocess.run(
        [sys.executable, "-m", "ai_r", "--version"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert p.returncode == 0, p.stderr
    assert "ai-r" in p.stdout


def test_cli_help() -> None:
    p = _run_cli("--help")
    assert p.returncode == 0, p.stderr
    assert "list" in p.stdout
    assert "read" in p.stdout
    assert "search" in p.stdout


def test_cli_no_subcommand_returns_1() -> None:
    p = _run_cli()
    assert p.returncode != 0
    assert "usage" in p.stderr.lower() or "usage" in p.stdout.lower()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_cli_list_claude(tmp_sessions_dir: Path) -> None:
    # Hermetic: seed a fake home and point AI_R_HOME at it, so the test
    # does not depend on real sessions existing on the runner.
    _write_claude_session(
        tmp_sessions_dir,
        "46d7b4fc-70bc-4cb9-90f4-bca5e0c7e51a",
        "Hermetic list session",
    )
    p = _run_cli(
        "list",
        "--agent",
        "claude",
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert p.returncode == 0, p.stderr
    assert "UUID" in p.stdout
    assert "AGENT" in p.stdout


def test_cli_list_json() -> None:
    p = _run_cli("list", "--agent", "claude", "--json")
    assert p.returncode == 0, p.stderr
    payload = json.loads(p.stdout)
    assert isinstance(payload, list)
    if payload:  # host has Claude sessions
        for item in payload[:3]:
            assert "uuid" in item
            assert "agent" in item
            assert "date" in item


def test_cli_list_empty() -> None:
    """No sessions in AI_R_HOME -> stderr message, exit 0."""
    rc, out, err = _run_inproc(
        ["list", "--agent", "claude"],
        env={"AI_R_HOME": "/nonexistent"},
    )
    assert rc == 0
    assert "no sessions found" in err.lower()


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_cli_read_existing() -> None:
    uuid = _first_claude_uuid()
    if uuid is None:
        pytest.skip("no real Claude session on this host")
    p = _run_cli("read", "--agent", "claude", uuid)
    assert p.returncode == 0, p.stderr
    assert uuid in p.stdout
    assert "UUID:" in p.stdout


def test_cli_read_json() -> None:
    uuid = _first_claude_uuid()
    if uuid is None:
        pytest.skip("no real Claude session on this host")
    p = _run_cli("read", "--agent", "claude", "--json", uuid)
    assert p.returncode == 0, p.stderr
    payload = json.loads(p.stdout)
    assert payload["uuid"] == uuid
    assert payload["agent"] == "CLAUDE"


def test_cli_read_invalid_uuid_format() -> None:
    """A uuid that fails the regex (e.g. contains whitespace) -> non-zero."""
    p = _run_cli("read", "--agent", "claude", "has spaces")
    assert p.returncode != 0


def test_cli_read_unknown_agent() -> None:
    p = _run_cli("read", "--agent", "mystery", "some-uuid")
    assert p.returncode != 0


def test_cli_read_missing_uuid() -> None:
    p = _run_cli("read", "--agent", "claude")
    assert p.returncode != 0


def test_cli_read_not_found() -> None:
    """Valid uuid format but no such session -> exit 3 (not found)."""
    rc, out, err = _run_inproc(["read", "--agent", "claude", "definitely-not-here"])
    assert rc == 3
    assert "not found" in err.lower()


def test_cli_read_unique_short_claude_uuid(
    tmp_sessions_dir: Path,
) -> None:
    full = "46d7b4fc-70bc-4cb9-90f4-bca5e0c7e51a"
    _write_claude_session(tmp_sessions_dir, full, "Unique short uuid")

    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", "46d7b4fc"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )

    assert rc == 0, err
    assert full in out
    assert "UUID:" in out


def test_cli_read_unique_short_claude_uuid_without_agent(
    tmp_sessions_dir: Path,
) -> None:
    full = "46d7b4fc-70bc-4cb9-90f4-bca5e0c7e51a"
    _write_claude_session(tmp_sessions_dir, full, "Unique short uuid")

    rc, out, err = _run_inproc(
        ["read", "46d7b4fc"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )

    assert rc == 0, err
    assert full in out
    assert "Agent:     CLAUDE" in out


def test_cli_read_ambiguous_short_claude_uuid(
    tmp_sessions_dir: Path,
) -> None:
    first = "46d7b4fc-70bc-4cb9-90f4-bca5e0c7e51a"
    second = "46d7b4fc-1111-4cb9-90f4-bca5e0c7e51a"
    _write_claude_session(tmp_sessions_dir, first, "First")
    _write_claude_session(tmp_sessions_dir, second, "Second")

    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", "46d7b4fc"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )

    assert rc == 2
    assert "ambiguous session prefix" in err
    assert first in err
    assert second in err


def test_cli_read_missing_short_claude_uuid(
    tmp_sessions_dir: Path,
) -> None:
    _write_claude_session(
        tmp_sessions_dir,
        "46d7b4fc-70bc-4cb9-90f4-bca5e0c7e51a",
        "Existing",
    )

    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", "00000000"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )

    assert rc == 3
    assert "not found" in err.lower()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_cli_search_claude() -> None:
    p = _run_cli("search", "claude")
    assert p.returncode == 0, p.stderr
    assert "UUID" in p.stdout or "(no sessions match" in p.stderr


def test_cli_search_no_results() -> None:
    p = _run_cli("search", "this-string-should-match-nothing-xyzzy123")
    assert p.returncode == 0, p.stderr
    assert "no sessions match" in p.stderr.lower()


def test_cli_search_empty_query_rejected() -> None:
    p = _run_cli("search", "")
    assert p.returncode != 0
    assert "search query" in p.stderr.lower()


def test_cli_search_json() -> None:
    p = _run_cli("search", "claude", "--json")
    assert p.returncode == 0, p.stderr
    payload = json.loads(p.stdout)
    assert isinstance(payload, list)


# ---------------------------------------------------------------------------
# In-process coverage-focused tests
# ---------------------------------------------------------------------------


def test_cli_inproc_list_claude(tmp_sessions_dir: Path) -> None:
    """In-process: drives ``cli.main`` directly so coverage counts."""
    # Hermetic: seed a fake home so the listing does not rely on the runner
    # having real Claude sessions.
    _write_claude_session(
        tmp_sessions_dir,
        "46d7b4fc-70bc-4cb9-90f4-bca5e0c7e51a",
        "Hermetic inproc list session",
    )
    rc, out, err = _run_inproc(
        ["list", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert "UUID" in out


def test_cli_inproc_list_json() -> None:
    rc, out, err = _run_inproc(["list", "--agent", "claude", "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert isinstance(payload, list)


def test_cli_inproc_read_existing() -> None:
    uuid = _first_claude_uuid()
    if uuid is None:
        pytest.skip("no real Claude session on this host")
    rc, out, err = _run_inproc(["read", "--agent", "claude", uuid])
    assert rc == 0
    assert uuid in out


def test_cli_inproc_read_json() -> None:
    uuid = _first_claude_uuid()
    if uuid is None:
        pytest.skip("no real Claude session on this host")
    rc, out, err = _run_inproc(["read", "--agent", "claude", "--json", uuid])
    assert rc == 0
    payload = json.loads(out)
    assert payload["uuid"] == uuid


def test_cli_inproc_read_invalid_uuid() -> None:
    rc, out, err = _run_inproc(["read", "--agent", "claude", "has spaces"])
    # Regex check fails -> ValueError -> exit 1.
    assert rc == 1


def test_cli_inproc_read_unknown_agent() -> None:
    """Argparse rejects unknown ``--agent`` choice -> exit 2."""
    rc, out, err = _run_inproc(["read", "--agent", "mystery", "some-uuid"])
    assert rc == 2


def test_cli_inproc_read_missing_uuid_arg() -> None:
    """No uuid -> argparse usage error."""
    rc, out, err = _run_inproc(["read", "--agent", "claude"])
    assert rc != 0


def test_cli_inproc_search_claude() -> None:
    rc, out, err = _run_inproc(["search", "claude"])
    assert rc == 0
    assert "UUID" in out or "no sessions match" in err.lower()


def test_cli_inproc_search_no_results() -> None:
    rc, out, err = _run_inproc(["search", "xyzzy-zzz-nothing-matches-12345"])
    assert rc == 0
    assert "no sessions match" in err.lower()


def test_cli_inproc_search_empty_query() -> None:
    rc, out, err = _run_inproc(["search", ""])
    assert rc == 1
    assert "search query" in err.lower()


def test_cli_inproc_search_json_no_results() -> None:
    rc, out, err = _run_inproc(["search", "xyzzy", "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload == []


def test_cli_inproc_no_subcommand() -> None:
    """No subcommand -> parser prints help and returns 1."""
    rc, out, err = _run_inproc([])
    assert rc == 1
    assert "usage" in err.lower() or "usage" in out.lower()


def test_cli_inproc_build_parser() -> None:
    """The parser factory is exercised by every test above, but we
    add an explicit check that the ``list`` and ``search`` subcommand
    paths handle unknown agents cleanly.
    """
    parser = cli_module.build_parser()
    for cmd in ("list", "search"):
        with pytest.raises(SystemExit):
            parser.parse_args([cmd, "--agent", "mystery"])


# ---------------------------------------------------------------------------
# Result-limiting / date flags (--limit, --days, --from-date, --to-date, --all)
# ---------------------------------------------------------------------------


def _make_claude_session(
    home: Path, session_id: str, when: str, title: str = "session"
) -> str:
    """Write a minimal Claude session JSONL into ``home`` and return its uuid."""
    import json as _json

    jsonl = home / ".claude" / "projects" / "proj-x" / f"{session_id}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "message": {"role": "user", "content": title},
        "timestamp": when,
        "sessionId": session_id,
    }
    with jsonl.open("w", encoding="utf-8") as fh:
        fh.write(_json.dumps(record, ensure_ascii=False))
        fh.write("\n")
    return session_id


def test_cli_list_limit_truncates(
    tmp_sessions_dir: Path,
) -> None:
    """``--limit N`` truncates the table to at most N rows."""
    for n in range(5):
        _make_claude_session(
            tmp_sessions_dir,
            f"lim-{n}",
            "2026-06-14T10:00:00Z",
            title=f"row {n}",
        )
    rc, out, err = _run_inproc(
        ["list", "--agent", "claude", "--limit", "2", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert len(payload) == 2


def test_cli_list_days_filter(
    tmp_sessions_dir: Path,
) -> None:
    """``--days`` keeps only recent sessions (vs datetime.now())."""
    now = datetime.now()
    recent_iso = now.strftime("%Y-%m-%dT10:00:00Z")
    old = now - timedelta(days=30)
    old_iso = old.strftime("%Y-%m-%dT10:00:00Z")
    _make_claude_session(tmp_sessions_dir, "recent-1", recent_iso, title="recent")
    _make_claude_session(tmp_sessions_dir, "old-1", old_iso, title="old")
    rc, out, err = _run_inproc(
        ["list", "--agent", "claude", "--days", "7", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "recent-1" in uuids
    assert "old-1" not in uuids


def test_cli_list_from_to_date_filter(
    tmp_sessions_dir: Path,
) -> None:
    """``--from-date``/``--to-date`` keep sessions within the window."""
    _make_claude_session(
        tmp_sessions_dir, "before-1", "2026-05-01T10:00:00Z", title="before"
    )
    _make_claude_session(
        tmp_sessions_dir, "inside-1", "2026-06-14T10:00:00Z", title="inside"
    )
    _make_claude_session(
        tmp_sessions_dir, "after-1", "2026-07-01T10:00:00Z", title="after"
    )
    rc, out, err = _run_inproc(
        [
            "list",
            "--agent",
            "claude",
            "--from-date",
            "2026-06-01",
            "--to-date",
            "2026-06-30",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "inside-1" in uuids
    assert "before-1" not in uuids
    assert "after-1" not in uuids


def test_cli_list_bad_date_exits_1(
    tmp_sessions_dir: Path,
) -> None:
    """Invalid ``--from-date`` -> exit 1 with a stderr message."""
    rc, out, err = _run_inproc(
        ["list", "--agent", "claude", "--from-date", "not-a-date"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 1
    assert "invalid --from-date" in err.lower()


def test_cli_list_all_flag_accepted(
    tmp_sessions_dir: Path,
) -> None:
    """``--all`` is accepted and behaves as a no-op."""
    _make_claude_session(
        tmp_sessions_dir, "all-1", "2026-06-14T10:00:00Z", title="all"
    )
    rc, out, err = _run_inproc(
        ["list", "--agent", "claude", "--all", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert len(payload) == 1


def test_cli_search_limit_and_days(
    tmp_sessions_dir: Path,
) -> None:
    """``--limit``/``--days`` apply to search results too."""
    now = datetime.now()
    for n in range(3):
        iso = now.strftime("%Y-%m-%dT10:00:00Z")
        _make_claude_session(
            tmp_sessions_dir, f"src-{n}", iso, title="searchme"
        )
    rc, out, err = _run_inproc(
        ["search", "searchme", "--agent", "claude", "--limit", "1", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert len(json.loads(out)) == 1


# ---------------------------------------------------------------------------
# read --messages
# ---------------------------------------------------------------------------


def test_cli_read_messages_human(
    fake_claude_session_with_tools: Path,
    tmp_sessions_dir: Path,
) -> None:
    """``read --messages`` dumps message text + tool_use names."""
    uuid = fake_claude_session_with_tools.stem
    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", uuid, "--messages"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert uuid in out
    assert "[tool_use: Bash]" in out
    assert "Run the tests" in out


def test_cli_read_messages_json(
    fake_claude_session_with_tools: Path,
    tmp_sessions_dir: Path,
) -> None:
    """``read --json --messages`` embeds a ``messages`` list with tool names."""
    uuid = fake_claude_session_with_tools.stem
    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", uuid, "--messages", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["uuid"] == uuid
    msgs = payload["messages"]
    assert isinstance(msgs, list)
    assert any("Bash" in (m["tool_use"]) for m in msgs)


def test_cli_read_messages_missing_session(
    tmp_sessions_dir: Path,
) -> None:
    """``read --messages`` on a missing uuid still exits 3 (metadata path)."""
    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", "no-such-session", "--messages"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 3
    assert "not found" in err.lower()


# ---------------------------------------------------------------------------
# read --with-tokens
# ---------------------------------------------------------------------------


def test_cli_read_with_tokens_json(
    fake_claude_session_with_tools: Path,
    tmp_sessions_dir: Path,
) -> None:
    """``read --with-tokens --json`` embeds a ``tokens`` component block."""
    uuid = fake_claude_session_with_tools.stem
    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", uuid, "--with-tokens", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    tokens = payload["tokens"]
    assert tokens is not None
    assert tokens["source"] == "estimate"
    # Component keys present + a tool_call sub-dict.
    for key in ("user_turn", "assistant_turn", "thinking", "plan", "tool_call"):
        assert key in tokens
    assert isinstance(tokens["tool_call"], dict)


def test_cli_read_with_tokens_human(
    fake_claude_session_with_tools: Path,
    tmp_sessions_dir: Path,
) -> None:
    """``read --with-tokens`` (human) prints the COMPONENT/TOKENS/SOURCE table."""
    uuid = fake_claude_session_with_tools.stem
    rc, out, err = _run_inproc(
        ["read", "--agent", "claude", uuid, "--with-tokens"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "COMPONENT" in out
    assert "TOKENS" in out
    assert "SOURCE" in out
    # The table always ends with a ``total`` row.
    assert "total" in out


# ---------------------------------------------------------------------------
# search — new scope/operator flags (delegated to mcp_server.search_sessions)
# ---------------------------------------------------------------------------


def _write_claude_session_with_body(
    home: Path,
    uuid: str,
    body_lines: list[str],
    title: str = "",
) -> None:
    """Write a Claude session whose message bodies carry ``body_lines``.

    The first line is the user message; alternating roles for the rest.
    The title is the first user message (Claude parser precedence).
    """
    path = home / ".claude" / "projects" / "proj-a" / f"{uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for i, line in enumerate(body_lines):
        role = "user" if i % 2 == 0 else "assistant"
        records.append(
            {
                "type": role,
                "message": {"role": role, "content": line},
                "timestamp": f"2026-06-14T10:00:0{i}Z",
                "sessionId": uuid,
            }
        )
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_cli_search_scope_body_finds_session(
    tmp_sessions_dir: Path,
) -> None:
    """``--scope body`` finds a session whose message text matches."""
    _write_claude_session_with_body(
        tmp_sessions_dir,
        "ses-pwa-1",
        ["How do I add a pwa manifest to my project?"],
        title="pwa manifest help",
    )
    rc, out, err = _run_inproc(
        [
            "search",
            "pwa manifest",
            "--scope",
            "body",
            "--agent",
            "claude",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "ses-pwa-1" in uuids


def test_cli_search_scope_body_no_results(
    tmp_sessions_dir: Path,
) -> None:
    """``--scope body`` with no body match -> stderr message, exit 0."""
    _write_claude_session_with_body(
        tmp_sessions_dir, "ses-empty", ["just plain text"]
    )
    rc, out, err = _run_inproc(
        [
            "search",
            "xyzzy-no-such-token",
            "--scope",
            "body",
            "--agent",
            "claude",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "no sessions match" in err.lower()


def test_cli_search_operator_or(
    tmp_sessions_dir: Path,
) -> None:
    """``--operator or`` matches if ANY term appears in the body."""
    _write_claude_session_with_body(
        tmp_sessions_dir, "ses-or-1", ["foo bar", "ok"]
    )
    rc, out, err = _run_inproc(
        [
            "search",
            "foo baz",
            "--operator",
            "or",
            "--scope",
            "body",
            "--agent",
            "claude",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "ses-or-1" in uuids


def test_cli_search_operator_not(
    tmp_sessions_dir: Path,
) -> None:
    """``--operator not`` excludes sessions whose body contains the term."""
    _write_claude_session_with_body(
        tmp_sessions_dir, "ses-not-1", ["foo and more"]
    )
    rc, out, err = _run_inproc(
        [
            "search",
            "foo",
            "--operator",
            "not",
            "--scope",
            "body",
            "--agent",
            "claude",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "ses-not-1" not in uuids


def test_cli_search_negative_prefix(
    tmp_sessions_dir: Path,
) -> None:
    """A ``-term`` in the query always excludes, regardless of operator."""
    _write_claude_session_with_body(
        tmp_sessions_dir, "ses-neg-has", ["foo bar"]
    )
    _write_claude_session_with_body(
        tmp_sessions_dir, "ses-neg-miss", ["foo baz"]
    )
    rc, out, err = _run_inproc(
        [
            "search",
            "foo -bar",
            "--scope",
            "body",
            "--agent",
            "claude",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "ses-neg-has" not in uuids
    assert "ses-neg-miss" in uuids


def test_cli_search_limit_body(
    tmp_sessions_dir: Path,
) -> None:
    """``--limit`` truncates body-search results."""
    for n in range(5):
        _write_claude_session_with_body(
            tmp_sessions_dir,
            f"ses-lim-{n}",
            ["hello world message"],
        )
    rc, out, err = _run_inproc(
        [
            "search",
            "hello",
            "--scope",
            "body",
            "--agent",
            "claude",
            "--limit",
            "2",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert len(payload) <= 2


def test_cli_search_invalid_scope(
    tmp_sessions_dir: Path,
) -> None:
    """``--scope bogus`` -> exit 1 with a stderr message."""
    rc, out, err = _run_inproc(
        ["search", "anything", "--scope", "bogus", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 1
    assert "unknown --scope" in err.lower()


def test_cli_search_invalid_operator(
    tmp_sessions_dir: Path,
) -> None:
    """``--operator xor`` -> exit 1."""
    rc, out, err = _run_inproc(
        ["search", "anything", "--operator", "xor", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 1
    assert "unknown --operator" in err.lower()


def test_cli_search_invalid_limit(
    tmp_sessions_dir: Path,
) -> None:
    """``--limit -1`` -> exit 1."""
    rc, out, err = _run_inproc(
        ["search", "anything", "--limit", "-1", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 1
    assert "--limit" in err.lower()


def test_cli_search_sort_flag_parses(
    tmp_sessions_dir: Path,
) -> None:
    """``--sort relevance`` and ``--sort date`` both parse and exit 0."""
    _write_claude_session_with_body(
        tmp_sessions_dir, "ses-sort-1", ["kafka pipeline notes"]
    )
    for mode in ("relevance", "date"):
        rc, out, err = _run_inproc(
            [
                "search",
                "kafka",
                "--scope",
                "body",
                "--sort",
                mode,
                "--agent",
                "claude",
                "--json",
            ],
            env={"AI_R_HOME": str(tmp_sessions_dir)},
        )
        assert rc == 0, err
        assert isinstance(json.loads(out), list)
    # An unknown choice is rejected by argparse (exit 2).
    rc, out, err = _run_inproc(
        ["search", "kafka", "--sort", "bogus", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2


def test_cli_search_json_order_matches_mcp(
    tmp_sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI ``--json`` relevance order is identical to a direct
    ``search_sessions`` call on the same query (CLI just delegates)."""
    # Two body matches with different term densities -> non-trivial order.
    _write_claude_session_with_body(
        tmp_sessions_dir,
        "ord-dense",
        ["kafka kafka kafka kafka stream"],
    )
    _write_claude_session_with_body(
        tmp_sessions_dir,
        "ord-sparse",
        ["kafka " + " ".join(f"noise{i}" for i in range(60))],
    )

    rc, out, err = _run_inproc(
        ["search", "kafka", "--scope", "body", "--agent", "claude", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    cli_uuids = [item["uuid"] for item in json.loads(out)]

    # Drive the MCP source-of-truth directly with the same AI_R_HOME.
    from ai_r import mcp_server as _mcp

    monkeypatch.setenv("AI_R_HOME", str(tmp_sessions_dir))
    mcp_result = _mcp.search_sessions(
        query="kafka", agent="claude", scope="body"
    )
    mcp_uuids = [item["uuid"] for item in mcp_result["results"]]

    assert cli_uuids == mcp_uuids
    # Sanity: the denser match leads under relevance default.
    assert cli_uuids[0] == "ord-dense"


def test_cli_search_body_with_date_filter(
    tmp_sessions_dir: Path,
) -> None:
    """``--scope body`` and ``--days`` combine: old session excluded, recent kept."""
    now = datetime.now()
    old_iso = (now - timedelta(days=30)).strftime("%Y-%m-%dT10:00:00Z")
    recent_iso = now.strftime("%Y-%m-%dT10:00:00Z")
    _write_claude_session_at(
        tmp_sessions_dir, "ses-old", old_iso, ["deploy auth token"]
    )
    _write_claude_session_at(
        tmp_sessions_dir, "ses-recent", recent_iso, ["deploy auth token"]
    )
    rc, out, err = _run_inproc(
        [
            "search",
            "deploy",
            "--scope",
            "body",
            "--days",
            "7",
            "--agent",
            "claude",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "ses-old" not in uuids
    assert "ses-recent" in uuids


def _write_claude_session_at(
    home: Path, uuid: str, timestamp: str, body_lines: list[str]
) -> None:
    """Like :func:`_write_claude_session_with_body` but with a custom timestamp."""
    path = home / ".claude" / "projects" / "proj-a" / f"{uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for i, line in enumerate(body_lines):
        role = "user" if i % 2 == 0 else "assistant"
        records.append(
            {
                "type": role,
                "message": {"role": role, "content": line},
                "timestamp": timestamp,
                "sessionId": uuid,
            }
        )
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_cli_search_backward_compat(
    tmp_sessions_dir: Path,
) -> None:
    """``search QUERY`` with no new flags still works (title-only)."""
    _make_claude_session(
        tmp_sessions_dir,
        "ses-compat-1",
        "2026-06-14T10:00:00Z",
        title="claude pair programming",
    )
    rc, out, err = _run_inproc(
        ["search", "claude", "--agent", "claude", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "ses-compat-1" in uuids


def test_cli_search_short_op_alias(
    tmp_sessions_dir: Path,
) -> None:
    """``--op`` is a short alias for ``--operator``."""
    _write_claude_session_with_body(
        tmp_sessions_dir, "ses-alias-1", ["alpha gamma", "ok"]
    )
    rc, out, err = _run_inproc(
        [
            "search",
            "alpha delta",
            "--op",
            "or",
            "--scope",
            "body",
            "--agent",
            "claude",
            "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    uuids = [item["uuid"] for item in payload]
    assert "ses-alias-1" in uuids


# ---------------------------------------------------------------------------
# find-file-edits
# ---------------------------------------------------------------------------


def _write_claude_edit_session(
    home: Path,
    uuid: str,
    *,
    user_text: str,
    edit_path: str,
    old_string: str = "old",
    new_string: str = "new",
    ts_user: str = "2026-06-14T10:00:00Z",
    ts_edit: str = "2026-06-14T10:00:05Z",
) -> None:
    """Minimal Claude JSONL with a user msg + assistant ``Edit`` call."""
    import json as _json

    path = home / ".claude" / "projects" / "proj-fe" / f"{uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": ts_user,
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Editing now."},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": edit_path,
                            "old_string": old_string,
                            "new_string": new_string,
                        },
                    },
                ],
            },
            "timestamp": ts_edit,
            "sessionId": uuid,
        },
    ]
    path.write_text(
        "\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_pi_edit_session(
    home: Path,
    uuid: str,
    *,
    user_text: str,
    edit_path: str,
) -> None:
    """Minimal Pi JSONL with an assistant ``str_replace`` tool call."""
    import json as _json

    jsonl = (
        home
        / ".pi"
        / "agent"
        / "sessions"
        / "--tmp-fe-cli--"
        / f"2026-06-14T10-00-00-000Z_{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "session",
            "id": uuid,
            "timestamp": "2026-06-14T10:00:00.000Z",
            "cwd": "/tmp/fe-cli",
        },
        {
            "type": "message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
                "timestamp": 1_718_360_002_000,
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Replacing now."},
                    {
                        "type": "toolCall",
                        "name": "str_replace",
                        "arguments": {
                            "path": edit_path,
                            "old_string": "old",
                            "new_string": "new",
                        },
                    },
                ],
                "timestamp": 1_718_360_004_000,
            },
        },
    ]
    jsonl.write_text(
        "\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_cli_find_file_edits_basic(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Human-readable output surfaces the matching edit with intent."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-cli-1",
        user_text="Add the header",
        edit_path="/tmp/cli-basic/README.md",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "README.md"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "README.md" in out
    assert "Edit" in out
    assert "Add the header" in out
    assert "1 edit" in out


def test_cli_find_file_edits_json(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` returns a dict with ``records``/``count``/``truncated``."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-cli-json",
        user_text="json test",
        edit_path="/tmp/cli-json/src.py",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "src.py", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert "records" in payload
    assert "count" in payload
    assert "truncated" in payload
    assert payload["count"] == 1
    assert payload["truncated"] is False
    assert payload["records"][0]["file"] == "/tmp/cli-json/src.py"
    assert payload["records"][0]["tool"] == "Edit"


def test_cli_find_file_edits_invalid_bound(
    tmp_sessions_dir: Path,
) -> None:
    """Garbage ``--since`` -> exit 2 with a stderr message."""
    rc, out, err = _run_inproc(
        ["find-file-edits", "anything", "--since", "not-a-date"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "iso 8601" in err.lower() or "iso" in err.lower()


def test_cli_find_file_edits_cross_agent(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``--agent`` flag scans both Claude and Pi (cross-agent default)."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-cli-x",
        user_text="claude edit", edit_path="/tmp/cli-x/shared.py",
    )
    _write_pi_edit_session(
        tmp_sessions_dir, "pfe-cli-x",
        user_text="pi edit", edit_path="/tmp/cli-x/shared.py",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    monkeypatch.setattr(
        "ai_r.parsers.pi._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".pi" / "agent" / "sessions")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "shared.py", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    agents = {r["agent"] for r in payload["records"]}
    assert agents == {"claude", "pi"}


def test_cli_find_file_edits_agent_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--agent claude`` returns only Claude rows even when Pi has matches."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-cli-f",
        user_text="claude edit", edit_path="/tmp/cli-f/shared.py",
    )
    _write_pi_edit_session(
        tmp_sessions_dir, "pfe-cli-f",
        user_text="pi edit", edit_path="/tmp/cli-f/shared.py",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    monkeypatch.setattr(
        "ai_r.parsers.pi._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".pi" / "agent" / "sessions")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "shared.py", "--agent", "claude", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["records"][0]["agent"] == "claude"


# ---------------------------------------------------------------------------
# detect-agent / detect-session / export rounds  (smoke coverage)
# ---------------------------------------------------------------------------


def test_cli_detect_agent_env_named() -> None:
    """``detect-agent`` honours ``AGENT_NAME`` and prints the agent name.

    In-process so the env var reaches ``_detect_agent_with_source``
    cleanly.  ``AGENT_NAME`` is first in the detection cascade, so it
    wins regardless of other host agent env vars.
    """
    rc, out, err = _run_inproc(
        ["detect-agent", "--quiet"],
        env={"AGENT_NAME": "claude"},
    )
    assert rc == 0, err
    assert out.strip() == "claude"


def test_cli_detect_agent_human_format() -> None:
    """Non-quiet mode prints both ``agent:`` and ``source:`` lines."""
    rc, out, err = _run_inproc(
        ["detect-agent"],
        env={"AGENT_NAME": "claude"},
    )
    assert rc == 0, err
    assert "agent:" in out
    assert "source:" in out
    assert "CLAUDE" in out


def test_cli_detect_session_env_override() -> None:
    """``detect-session`` surfaces the ``AI_SESSION_ID`` override candidate.

    ``AI_SESSION_ID`` is the universal (step-1) signal — always emits a
    candidate with ``verified=True``.  We use ``--json`` so the output
    is a stable array we can assert against even if the host also has
    per-session flag files.
    """
    sentinel = "smoke-detected-session-id"
    rc, out, err = _run_inproc(
        ["detect-session", "--json"],
        env={"AI_SESSION_ID": sentinel},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    ids = [c["id"] for c in payload]
    assert sentinel in ids
    # The env-override candidate is always verified.
    match = next(c for c in payload if c["id"] == sentinel)
    assert match["verified"] is True
    assert match["source"] == "AI_SESSION_ID"


def test_cli_detect_session_count_env_override() -> None:
    """``--count`` returns at least 1 when ``AI_SESSION_ID`` is set."""
    rc, out, err = _run_inproc(
        ["detect-session", "--count"],
        env={"AI_SESSION_ID": "smoke-count-session"},
    )
    assert rc == 0, err
    assert int(out.strip()) >= 1


# --- _pick_single: pure unit coverage of the disambiguation modes -------------

def _cand(
    sid: str = "ses_aaaaxxxx",
    *,
    agent: AgentName | None = AgentName.OPENCODE,
    source: str = "ts_file:opencode",
    verified: bool = True,
    is_self: bool = False,
    fingerprint: str | None = "aaaaaaaa",
) -> SessionCandidate:
    return SessionCandidate(
        session_id=sid,
        agent=agent,
        source=source,
        verified=verified,
        is_self=is_self,
        fingerprint=fingerprint,
    )


def test_pick_single_first_returns_head_or_none() -> None:
    head = _cand("ses_headxxxx")
    assert _pick_single([head, _cand()], "first") is head
    assert _pick_single([], "first") is None


def test_pick_single_strict_rejects_ambiguous() -> None:
    only = _cand("ses_onlyxxxx")
    assert _pick_single([only], "strict") is only
    assert _pick_single([], "strict") is None
    with pytest.raises(AmbiguousSessionError):
        _pick_single([_cand(), _cand("ses_bbbbxxxx")], "strict")


def test_pick_single_self_mode() -> None:
    me = _cand("ses_mexxxxxx", is_self=True)
    other = _cand("ses_otherxxx", is_self=False)
    assert _pick_single([other, me], "self") is me
    # No self-marked candidate → None.
    assert _pick_single([other], "self") is None


def test_pick_single_fingerprint_mode() -> None:
    target = _cand("ses_targetxx", fingerprint="deadbeef")
    other = _cand("ses_otherxxx", fingerprint="cafef00d")
    assert _pick_single([other, target], "fingerprint:deadbeef") is target
    # Whitespace around the hash is tolerated.
    assert _pick_single([target], "fingerprint:  deadbeef  ") is target
    # No matching fingerprint → None.
    assert _pick_single([other], "fingerprint:deadbeef") is None


def test_pick_single_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        _pick_single([_cand()], "bogus")


# --- _run_detect_session: CLI rendering of the disambiguation modes -----------

def test_run_detect_session_invalid_agent_exits_nonzero() -> None:
    """An out-of-band invalid ``agent`` hits the handler's coerce-error branch.

    The CLI's ``--agent choices=`` rejects unknown values at parse time, so
    this branch is unreachable via the real entry point — we drive the
    handler with a hand-built namespace to exercise its own guard.
    """
    args = argparse.Namespace(agent="not-an-agent", count=False, json=False)
    assert _run_detect_session(args) != 0


def test_cli_detect_session_first_mode_prints_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SESSION_ID", "ses_pickfirstx")
    monkeypatch.setenv("AI_SESSION_OUTPUT", "first")
    rc, out, err = _run_inproc(["detect-session"])
    assert rc == 0, err
    assert "session=ses_pickfirstx" in out


def test_cli_detect_session_strict_ambiguous_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two candidates under ``strict`` → AmbiguousSessionError → exit code 2."""
    monkeypatch.setenv("AI_SESSION_ID", "ses_pickone_x")
    # A second, per-agent candidate (valid UUID shape) forces ambiguity.
    monkeypatch.setenv(
        "CLAUDE_CODE_SESSION_ID", "abcdef01-2345-6789-abcd-ef0123456789"
    )
    monkeypatch.setenv("AI_SESSION_OUTPUT", "strict")
    rc, out, err = _run_inproc(["detect-session"])
    assert rc == 2, err


def test_cli_detect_session_self_no_match_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``self`` mode with no self-marked candidate → exit non-zero."""
    monkeypatch.setenv("AI_SESSION_ID", "ses_noselfxxx")
    monkeypatch.setenv("AI_SESSION_OUTPUT", "self")
    rc, out, err = _run_inproc(["detect-session"])
    assert rc != 0


def test_cli_detect_session_fingerprint_no_match_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env-override candidate carries ``fingerprint=None`` → never matches."""
    monkeypatch.setenv("AI_SESSION_ID", "ses_nofp_match")
    monkeypatch.setenv("AI_SESSION_OUTPUT", "fingerprint:deadbeef")
    rc, out, err = _run_inproc(["detect-session"])
    assert rc != 0


def test_cli_detect_session_unknown_output_mode_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``AI_SESSION_OUTPUT`` value → ValueError → exit non-zero."""
    monkeypatch.setenv("AI_SESSION_ID", "ses_bogusmode")
    monkeypatch.setenv("AI_SESSION_OUTPUT", "bogus")
    rc, out, err = _run_inproc(["detect-session"])
    assert rc != 0


def test_cli_export_rounds_stdout(
    fake_claude_session_with_tools: Path,
    tmp_sessions_dir: Path,
) -> None:
    """``export rounds`` renders markdown for a session with messages."""
    uuid = fake_claude_session_with_tools.stem
    rc, out, err = _run_inproc(
        ["export", "rounds", "--agent", "claude", uuid],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    # session_to_rounds emits a markdown header + changelog entry.
    assert uuid in out
    assert "#" in out  # some markdown heading


def test_cli_export_rounds_to_file(
    fake_claude_session_with_tools: Path,
    tmp_sessions_dir: Path,
    tmp_path: Path,
) -> None:
    """``--output PATH`` writes the markdown to disk."""
    uuid = fake_claude_session_with_tools.stem
    dest = tmp_path / "rounds.md"
    rc, out, err = _run_inproc(
        ["export", "rounds", "--agent", "claude", uuid, "--output", str(dest)],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert dest.is_file()
    body = dest.read_text(encoding="utf-8")
    assert uuid in body
    assert len(body) > 0


def test_cli_export_rounds_include_round(
    fake_claude_session_with_tools: Path,
    tmp_sessions_dir: Path,
) -> None:
    """``--include-round`` pulls messages via read_messages and enriches output."""
    uuid = fake_claude_session_with_tools.stem
    rc, out, err = _run_inproc(
        ["export", "rounds", "--agent", "claude", uuid, "--include-round"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert uuid in out


def test_cli_export_rounds_not_found(
    tmp_sessions_dir: Path,
) -> None:
    """``export rounds`` on a missing uuid exits 3 (shared resolution path)."""
    rc, out, err = _run_inproc(
        ["export", "rounds", "--agent", "claude", "definitely-not-here"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 3
    assert "not found" in err.lower()
