"""End-to-end CLI tests, driven through ``subprocess``.

Why subprocess and not ``cli.main(argv)`` directly?  The CLI is the
executable surface that ships to operators, so testing the *real*
binary entry point — ``python -m ai_reader.cli`` — catches issues
that in-process testing would miss: missing ``__future__`` imports,
``sys.path`` munging, ``argparse`` quirks, the works.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ai_reader import cli as cli_module


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """Invoke ``python -m ai_reader.cli`` with the given args.

    The autouse ``_isolate_ai_reader_home`` fixture sets
    ``AI_READER_HOME`` in the *test* process; the subprocess would
    inherit it and look at an empty fake tree.  We explicitly strip
    the variable from the child environment unless the caller asked
    for it.
    """
    cmd = [sys.executable, "-m", "ai_reader.cli", *args]
    full_env = os.environ.copy()
    full_env.pop("AI_READER_HOME", None)
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
    "AI_READER_HOME",
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
    assert "ai-reader" in p.stdout
    assert "0.1.0" in p.stdout


def test_module_invocation() -> None:
    """``python -m ai_reader --version`` exits 0 (module entry point works)."""
    p = subprocess.run(
        [sys.executable, "-m", "ai_reader", "--version"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert p.returncode == 0, p.stderr
    assert "ai-reader" in p.stdout


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


def test_cli_list_claude() -> None:
    p = _run_cli("list", "--agent", "claude")
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
    """No sessions in AI_READER_HOME -> stderr message, exit 0."""
    rc, out, err = _run_inproc(
        ["list", "--agent", "claude"],
        env={"AI_READER_HOME": "/nonexistent"},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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


def test_cli_inproc_list_claude() -> None:
    """In-process: drives ``cli.main`` directly so coverage counts."""
    rc, out, err = _run_inproc(["list", "--agent", "claude"])
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 3
    assert "not found" in err.lower()


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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 1
    assert "unknown --scope" in err.lower()


def test_cli_search_invalid_operator(
    tmp_sessions_dir: Path,
) -> None:
    """``--operator xor`` -> exit 1."""
    rc, out, err = _run_inproc(
        ["search", "anything", "--operator", "xor", "--agent", "claude"],
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 1
    assert "unknown --operator" in err.lower()


def test_cli_search_invalid_limit(
    tmp_sessions_dir: Path,
) -> None:
    """``--limit -1`` -> exit 1."""
    rc, out, err = _run_inproc(
        ["search", "anything", "--limit", "-1", "--agent", "claude"],
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 1
    assert "--limit" in err.lower()


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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        "ai_reader.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "README.md"],
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        "ai_reader.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "src.py", "--json"],
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        "ai_reader.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    monkeypatch.setattr(
        "ai_reader.parsers.pi._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".pi" / "agent" / "sessions")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "shared.py", "--json"],
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
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
        "ai_reader.parsers.claude._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".claude" / "projects")),
    )
    monkeypatch.setattr(
        "ai_reader.parsers.pi._resolve_base_dir",
        lambda bd=None: Path(str(tmp_sessions_dir / ".pi" / "agent" / "sessions")),
    )
    rc, out, err = _run_inproc(
        ["find-file-edits", "shared.py", "--agent", "claude", "--json"],
        env={"AI_READER_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["records"][0]["agent"] == "claude"
