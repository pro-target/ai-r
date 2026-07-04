"""Tests for ``ai_r.find_tool_calls`` core, MCP wrapper, and CLI command.

The cross-agent scan is the only new logic — most of the helpers
(``previous_user_intent``, ``parse_iso_bound``, ``to_utc_aware``) are
imported from :mod:`ai_r.find_file_edits` and exercised in their
existing tests; this module only covers the new validation + iteration
behaviour.

Layout mirrors ``tests/test_find_file_edits`` (the file that was the
reference design for this one): one section for the core scan, one for
the MCP wrapper, one for the CLI.  All fixtures used here come from
:mod:`tests.conftest`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r import cli as cli_module
from ai_r.find_tool_calls import find_tool_calls
from ai_r.mcp_server import find_tool_calls as mcp_find_tool_calls


# ---------------------------------------------------------------------------
# Helpers (local — kept private to this module)
# ---------------------------------------------------------------------------


def _run_inproc(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run ``cli.main`` in-process; return ``(rc, stdout, stderr)``.

    Mirrors the helper used in ``test_cli`` / ``test_mcp`` so the
    coverage lines for our new command count toward the report.
    """
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


def _write_claude_tool_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    user_text: str,
    tool_name: str,
    tool_input: dict,
    ts_user: str = "2026-06-14T10:00:00Z",
    ts_call: str = "2026-06-14T10:00:05Z",
    second_user_text: str | None = None,
    second_tool_name: str | None = None,
    second_tool_input: dict | None = None,
    second_ts_user: str = "2026-06-14T10:01:00Z",
    second_ts_call: str = "2026-06-14T10:01:05Z",
) -> None:
    """Write a Claude JSONL with a user msg + assistant tool_use.

    If ``second_*`` kwargs are set, appends a second user msg + a second
    assistant turn with another tool_use; useful for the multi-tool-call
    test.
    """
    records: list[dict] = [
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
                    {"type": "text", "text": f"Calling {tool_name}."},
                    {"type": "tool_use", "name": tool_name, "input": tool_input},
                ],
            },
            "timestamp": ts_call,
            "sessionId": uuid,
        },
    ]
    if second_user_text is not None and second_tool_name is not None:
        records.append(
            {
                "type": "user",
                "message": {"role": "user", "content": second_user_text},
                "timestamp": second_ts_user,
                "sessionId": uuid,
            }
        )
        records.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"Calling {second_tool_name}."},
                        {
                            "type": "tool_use",
                            "name": second_tool_name,
                            "input": (
                                second_tool_input
                                if second_tool_input is not None
                                else {}
                            ),
                        },
                    ],
                },
                "timestamp": second_ts_call,
                "sessionId": uuid,
            }
        )
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-ftc" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_claude_session_with_outcomes(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    ok_output: str = "ok-stdout-here",
    err_output: str = "boom: command failed",
    ok_command: str = "true",
    err_command: str = "false",
) -> None:
    """Write a Claude JSONL with two ``Bash`` calls, each correlated to a
    ``tool_result`` on a following user record by ``tool_use_id``:

    * ``bash-ok``   → ``tool_result`` ``is_error=False`` (success)
    * ``bash-err``  → ``tool_result`` ``is_error=True``  (failure)

    Mirrors the real Claude on-disk layout (call in an assistant record,
    result in the next user record) so the correlation path is exercised
    end to end rather than through a synthetic in-memory ``Message``.
    """
    records: list[dict] = [
        {
            "type": "user",
            "message": {"role": "user", "content": "run both commands"},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running the good one."},
                    {
                        "type": "tool_use",
                        "id": "bash-ok",
                        "name": "Bash",
                        "input": {"command": ok_command},
                    },
                ],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "bash-ok",
                        "is_error": False,
                        "content": ok_output,
                    }
                ],
            },
            "timestamp": "2026-06-14T10:00:06Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running the bad one."},
                    {
                        "type": "tool_use",
                        "id": "bash-err",
                        "name": "Bash",
                        "input": {"command": err_command},
                    },
                ],
            },
            "timestamp": "2026-06-14T10:01:05Z",
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "bash-err",
                        "is_error": True,
                        "content": err_output,
                    }
                ],
            },
            "timestamp": "2026-06-14T10:01:06Z",
            "sessionId": uuid,
        },
    ]
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-ftc" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_find_tool_calls_neither_set_raises() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        find_tool_calls()


def test_find_tool_calls_both_set_raises() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        find_tool_calls(tool_name="X", tool_name_pattern="Y")


def test_find_tool_calls_empty_tool_name_raises() -> None:
    with pytest.raises(ValueError, match="tool_name"):
        find_tool_calls(tool_name="  ")


def test_find_tool_calls_empty_pattern_raises() -> None:
    with pytest.raises(ValueError, match="pattern"):
        find_tool_calls(tool_name_pattern="  ")


def test_find_tool_calls_negative_limit_raises() -> None:
    with pytest.raises(ValueError, match="limit"):
        find_tool_calls(tool_name="X", limit=-1)


def test_find_tool_calls_bad_since_raises() -> None:
    with pytest.raises(ValueError, match="since"):
        find_tool_calls(tool_name="X", since="not-a-date")


def test_find_tool_calls_bad_until_raises() -> None:
    with pytest.raises(ValueError, match="until"):
        find_tool_calls(tool_name="X", until="not-a-date")


def test_find_tool_calls_unknown_agent_raises() -> None:
    with pytest.raises(ValueError, match="agent"):
        find_tool_calls(tool_name="X", agent="mystery")


# ---------------------------------------------------------------------------
# Empty / no-match
# ---------------------------------------------------------------------------


def test_find_tool_calls_no_sessions_returns_empty(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hermetic tree with no Claude/Pi/Codex/OpenCode/Antigravity
    sessions must yield an empty ``records`` list, ``count=0``,
    ``truncated=False``."""
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="anything")
    diagnostics = result.pop("diagnostics")
    assert result == {
        "records": [],
        "count": 0,
        "truncated": False,
        "output_truncated": False,
    }
    # A zero-match result must explain itself (F1.1): what was scanned +
    # why nothing matched.
    assert diagnostics["hints"]
    assert {e["agent"] for e in diagnostics["scanned"]} == {
        "claude", "codex", "opencode", "antigravity", "pi",
    }


def test_find_tool_calls_no_match_returns_empty(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session with a different tool name produces no records."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-nomatch",
        user_text="hello",
        tool_name="Read",
        tool_input={"file_path": "/x"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="NonExistent")
    diagnostics = result.pop("diagnostics")
    assert result == {
        "records": [],
        "count": 0,
        "truncated": False,
        "output_truncated": False,
    }
    assert diagnostics["filters"]["tool_name"] == "NonExistent"


# ---------------------------------------------------------------------------
# Single + multiple tool calls
# ---------------------------------------------------------------------------


def test_find_tool_calls_single_record(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One tool call in one session surfaces as one record with the
    expected shape (agent, session, tool, input, intent, assistant)."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-single",
        user_text="Please add the header to the readme",
        tool_name="Edit",
        tool_input={"file_path": "/x/README.md", "old_string": "a", "new_string": "b"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Edit", agent="claude")
    assert result["count"] == 1
    assert result["truncated"] is False
    hit = result["records"][0]
    assert hit["agent"] == "claude"
    assert hit["session_uuid"] == "ftc-single"
    assert hit["session_title"]  # non-empty
    assert hit["session_date"]  # non-empty
    assert hit["message_index"] == 1
    assert hit["tool"] == "Edit"
    assert hit["input"] == {
        "file_path": "/x/README.md",
        "old_string": "a",
        "new_string": "b",
    }
    assert hit["intent"] == "Please add the header to the readme"
    assert hit["assistant"] == "Calling Edit."
    assert hit["timestamp"] is not None
    assert hit["timestamp"].endswith("+00:00")


def test_find_tool_calls_multiple_in_one_session(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tool calls (different names) in the same session yield two
    records with their respective ``intent`` values."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-multi",
        user_text="first intent",
        tool_name="Bash",
        tool_input={"command": "ls"},
        second_user_text="second intent",
        second_tool_name="Bash",
        second_tool_input={"command": "pwd"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert result["count"] == 2
    intents = [r["intent"] for r in result["records"]]
    assert intents == ["first intent", "second intent"]


# ---------------------------------------------------------------------------
# Filter: agent / tool_name / pattern / since / until / limit
# ---------------------------------------------------------------------------


def test_find_tool_calls_agent_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``agent=claude``, only Claude rows appear even when the
    shared tree holds sessions in other agents' layouts."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-af",
        user_text="claude call",
        tool_name="Bash",
        tool_input={"command": "true"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert {r["agent"] for r in result["records"]} == {"claude"}


def test_find_tool_calls_exact_name_case_insensitive(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tool_name`` is matched case-insensitively: a session with
    ``Bash`` (capital B) matches the filter ``bash``."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-case",
        user_text="x",
        tool_name="Bash",
        tool_input={"command": "echo hi"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="bash", agent="claude")
    assert result["count"] == 1
    assert result["records"][0]["tool"] == "Bash"


def test_find_tool_calls_pattern_substring(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tool_name_pattern`` is a substring match (case-insensitive)."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-sub",
        user_text="a",
        tool_name="mcp__ai-r__search_sessions",
        tool_input={"query": "x"},
    )
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-sub-2",
        user_text="b",
        tool_name="Bash",
        tool_input={"command": "echo"},
        ts_user="2026-06-14T11:00:00Z",
        ts_call="2026-06-14T11:00:05Z",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name_pattern="mcp__ai-r__")
    assert result["count"] == 1
    assert result["records"][0]["session_uuid"] == "ftc-sub"
    assert result["records"][0]["tool"] == "mcp__ai-r__search_sessions"


def test_find_tool_calls_pattern_excludes_non_matches(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pattern that only matches the prefix filters out unrelated
    tools in the same session pool."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-pat-1",
        user_text="a",
        tool_name="mcp__ai-r__search_sessions",
        tool_input={"query": "x"},
    )
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-pat-2",
        user_text="b",
        tool_name="Bash",
        tool_input={"command": "echo"},
        ts_user="2026-06-14T11:00:00Z",
        ts_call="2026-06-14T11:00:05Z",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name_pattern="mcp__")
    uuids = [r["session_uuid"] for r in result["records"]]
    assert "ftc-pat-1" in uuids
    assert "ftc-pat-2" not in uuids


def test_find_tool_calls_since_until_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """since/until bound the call timestamp inclusively."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-b-a",
        user_text="a",
        tool_name="Bash",
        tool_input={"command": "x"},
        ts_user="2026-06-14T09:00:00Z",
        ts_call="2026-06-14T09:00:05Z",
    )
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-b-b",
        user_text="b",
        tool_name="Bash",
        tool_input={"command": "x"},
        ts_user="2026-06-14T11:00:00Z",
        ts_call="2026-06-14T11:00:05Z",
    )
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-b-c",
        user_text="c",
        tool_name="Bash",
        tool_input={"command": "x"},
        ts_user="2026-06-14T12:00:00Z",
        ts_call="2026-06-14T12:00:05Z",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(
        tool_name="Bash",
        agent="claude",
        since="2026-06-14T10:00:00Z",
        until="2026-06-14T11:30:00Z",
    )
    uuids = {r["session_uuid"] for r in result["records"]}
    assert uuids == {"ftc-b-b"}


def test_find_tool_calls_limit(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit`` caps the result list and sets ``truncated``."""
    for i in range(4):
        _write_claude_tool_session(
            tmp_sessions_dir, f"ftc-cap-{i}",
            user_text=f"u{i}",
            tool_name="Bash",
            tool_input={"command": f"e{i}"},
            ts_user=f"2026-06-14T10:0{i}:00Z",
            ts_call=f"2026-06-14T10:0{i}:30Z",
        )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Bash", agent="claude", limit=2)
    assert len(result["records"]) == 2
    assert result["count"] == 4
    assert result["truncated"] is True
    ts_list = [r["timestamp"] for r in result["records"]]
    assert ts_list == sorted(ts_list)


# ---------------------------------------------------------------------------
# Input shape: dict vs JSON-encoded string
# ---------------------------------------------------------------------------


def test_find_tool_calls_input_dict_passthrough(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude-style dict input is returned unchanged in the record."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-dict",
        user_text="u",
        tool_name="Edit",
        tool_input={"file_path": "/d", "old_string": "o", "new_string": "n"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Edit", agent="claude")
    assert result["count"] == 1
    inp = result["records"][0]["input"]
    assert isinstance(inp, dict)
    assert inp["file_path"] == "/d"
    assert inp["old_string"] == "o"


def test_find_tool_calls_huge_input_not_parsed(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool input string larger than the size cap is returned as-is
    (no ``json.loads`` is attempted) to prevent memory exhaustion on
    Codex sessions with multi-MB ``function_call.arguments`` payloads."""
    import json as _json

    uuid = "ftc-huge"
    rollout = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T12-00-00-{uuid}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    big = "a" * 1_100_000
    records = [
        {
            "timestamp": "2026-06-14T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": uuid, "cwd": "/tmp/ftc"},
        },
        {
            "timestamp": "2026-06-14T12:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": "big payload"}],
            },
        },
        {
            "timestamp": "2026-06-14T12:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "search_sessions",
                "arguments": big,
            },
        },
    ]
    rollout.write_text(
        "\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir",
        lambda bd=None: [tmp_sessions_dir / ".codex" / "sessions"],
    )
    result = find_tool_calls(tool_name="search_sessions", agent="codex")
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["tool"] == "search_sessions"
    # The oversized string is NOT json-parsed (memory-exhaustion guard): it
    # stays a str rather than a decoded structure.  It is then per-field
    # char-capped (never inlined at full 1.1 MB), sliced with a marker and
    # flagged in ``truncated_fields``.
    assert isinstance(hit["input"], str)
    assert len(hit["input"]) < len(big)
    assert hit["input"].startswith("a")
    assert hit["input"].endswith("…[truncated]")
    assert "input" in hit["truncated_fields"]


def test_find_tool_calls_input_json_string_is_parsed(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex-style input (JSON string) is decoded into a dict."""
    import json as _json

    uuid = "ftc-codex"
    rollout = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T12-00-00-{uuid}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-06-14T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": uuid, "cwd": "/tmp/ftc"},
        },
        {
            "timestamp": "2026-06-14T12:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": "Run a search"}],
            },
        },
        {
            "timestamp": "2026-06-14T12:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "search_sessions",
                "arguments": _json.dumps({"query": "pwa", "limit": 5}),
            },
        },
    ]
    rollout.write_text(
        "\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir",
        lambda bd=None: [tmp_sessions_dir / ".codex" / "sessions"],
    )
    result = find_tool_calls(tool_name="search_sessions", agent="codex")
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["agent"] == "codex"
    assert hit["tool"] == "search_sessions"
    assert isinstance(hit["input"], dict)
    assert hit["input"]["query"] == "pwa"
    assert hit["input"]["limit"] == 5
    assert hit["intent"] == "Run a search"


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


def test_find_tool_calls_intent_is_previous_user_message(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``intent`` is the text of the user message that immediately
    preceded the assistant tool call."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-intent",
        user_text="Refactor the auth module please",
        tool_name="Edit",
        tool_input={"file_path": "/x", "old_string": "a", "new_string": "b"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Edit", agent="claude")
    assert result["count"] == 1
    assert result["records"][0]["intent"] == "Refactor the auth module please"


def test_find_tool_calls_intent_none_when_no_previous_user(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool call on the very first message has no preceding user msg:
    ``intent`` is ``None``."""
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Starting cold."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "echo"},
                    },
                ],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": "ftc-cold",
        },
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-cold" / "ftc-cold.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert result["count"] == 1
    assert result["records"][0]["intent"] is None


# ---------------------------------------------------------------------------
# Outcome: is_error + output (correlated tool_result)
# ---------------------------------------------------------------------------


def test_find_tool_calls_surfaces_is_error_and_output(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call correlated to a failing ``tool_result`` surfaces
    ``is_error=True`` + its ``output``; a call correlated to a
    succeeding one surfaces ``is_error=False`` + its ``output``.

    This is the honest-git-stats signal: Total/Success/Error is buildable
    from the per-record ``is_error`` flag across sessions.
    """
    _write_claude_session_with_outcomes(tmp_sessions_dir, "ftc-outcome")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert result["count"] == 2
    by_input = {
        json.dumps(r["input"], sort_keys=True): r for r in result["records"]
    }
    ok = by_input[json.dumps({"command": "true"}, sort_keys=True)]
    err = by_input[json.dumps({"command": "false"}, sort_keys=True)]

    assert ok["is_error"] is False
    assert ok["output"] == "ok-stdout-here"
    assert "output" not in ok["truncated_fields"]

    assert err["is_error"] is True
    assert err["output"] == "boom: command failed"


def test_find_tool_calls_no_result_defaults_to_not_error(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call with no correlated ``tool_result`` (no id / no following
    result) defaults to ``is_error=False`` with an empty ``output`` and
    does not crash."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-noresult",
        user_text="just call",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["is_error"] is False
    assert hit["output"] == ""
    assert "output" not in hit["truncated_fields"]


def test_find_tool_calls_output_char_capped(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized ``tool_result`` content is sliced to the output cap
    and flagged in ``truncated_fields``."""
    from ai_r.find_tool_calls import _OUTPUT_CHARS_CAP

    big = "z" * (_OUTPUT_CHARS_CAP + 500)
    uuid = "ftc-bigout"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "run it"},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running."},
                    {
                        "type": "tool_use",
                        "id": "big-call",
                        "name": "Bash",
                        "input": {"command": "cat huge"},
                    },
                ],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "big-call",
                        "is_error": False,
                        "content": big,
                    }
                ],
            },
            "timestamp": "2026-06-14T10:00:06Z",
            "sessionId": uuid,
        },
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-ftc" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert result["count"] == 1
    hit = result["records"][0]
    assert isinstance(hit["output"], str)
    assert len(hit["output"]) < len(big)
    assert hit["output"].endswith("…[truncated]")
    assert "output" in hit["truncated_fields"]


def _write_claude_single_call(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    tool_name: str,
    tool_input: dict,
    output: str,
    is_error: bool,
    user_text: str = "do the thing",
) -> None:
    """Write a Claude JSONL with ONE tool call correlated to a result.

    Gives per-call control of ``input`` / ``output`` / ``is_error`` so
    tests can build precise filter scenarios (long outputs, harness
    markers, error-at-the-end, ...).
    """
    call_id = f"call-{uuid}"
    records: list[dict] = [
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"Calling {tool_name}."},
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": tool_name,
                        "input": tool_input,
                    },
                ],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "is_error": is_error,
                        "content": output,
                    }
                ],
            },
            "timestamp": "2026-06-14T10:00:06Z",
            "sessionId": uuid,
        },
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-ftc" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _patch_claude(tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )


# ---------------------------------------------------------------------------
# New filters: input_contains / output_contains / output_excludes / is_error
# ---------------------------------------------------------------------------


def test_find_tool_calls_input_contains_matches(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``input_contains`` keeps only calls whose serialized input has the
    substring; non-matches are dropped."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ic-git",
        user_text="u",
        tool_name="Bash",
        tool_input={"command": "git status"},
    )
    _write_claude_tool_session(
        tmp_sessions_dir, "ic-ls",
        user_text="u",
        tool_name="Bash",
        tool_input={"command": "ls -la"},
        ts_user="2026-06-14T11:00:00Z",
        ts_call="2026-06-14T11:00:05Z",
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)
    result = find_tool_calls(
        tool_name="Bash", agent="claude", input_contains="git"
    )
    assert result["count"] == 1
    assert result["records"][0]["session_uuid"] == "ic-git"


def test_find_tool_calls_input_contains_case_insensitive(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_tool_session(
        tmp_sessions_dir, "ic-ci",
        user_text="u",
        tool_name="Bash",
        tool_input={"command": "GIT push"},
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)
    result = find_tool_calls(
        tool_name="Bash", agent="claude", input_contains="git"
    )
    assert result["count"] == 1


def test_find_tool_calls_output_contains_and_excludes(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``output_contains`` matches on the output; ``output_excludes``
    drops a record whose output carries a harness marker."""
    _write_claude_single_call(
        tmp_sessions_dir, "oc-hit",
        tool_name="Bash",
        tool_input={"command": "run"},
        output="all tests passed cleanly",
        is_error=False,
    )
    _write_claude_single_call(
        tmp_sessions_dir, "oc-harness",
        tool_name="Bash",
        tool_input={"command": "run"},
        output="passed but AGENT_SECURITY_BOUNDARY_CHECKED noise",
        is_error=False,
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)

    hit = find_tool_calls(
        tool_name="Bash", agent="claude", output_contains="passed"
    )
    assert {r["session_uuid"] for r in hit["records"]} == {"oc-hit", "oc-harness"}

    excl = find_tool_calls(
        tool_name="Bash",
        agent="claude",
        output_contains="passed",
        output_excludes="AGENT_SECURITY_BOUNDARY_CHECKED",
    )
    assert {r["session_uuid"] for r in excl["records"]} == {"oc-hit"}


def test_find_tool_calls_is_error_tristate(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_error`` True/False/None each select the right subset."""
    _write_claude_session_with_outcomes(tmp_sessions_dir, "tri")
    _patch_claude(tmp_sessions_dir, monkeypatch)

    only_err = find_tool_calls(tool_name="Bash", agent="claude", is_error=True)
    assert only_err["count"] == 1
    assert only_err["records"][0]["is_error"] is True

    only_ok = find_tool_calls(tool_name="Bash", agent="claude", is_error=False)
    assert only_ok["count"] == 1
    assert only_ok["records"][0]["is_error"] is False

    both = find_tool_calls(tool_name="Bash", agent="claude", is_error=None)
    assert both["count"] == 2


def test_find_tool_calls_combined_and_filters(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``input_contains='git' + is_error=True`` returns only the real git
    failure, not the passing git call nor the harness-noise call."""
    # Real git failure.
    _write_claude_single_call(
        tmp_sessions_dir, "cmb-gitfail",
        tool_name="Bash",
        tool_input={"command": "git push"},
        output="error: failed to push some refs",
        is_error=True,
    )
    # git call that SUCCEEDED (filtered out by is_error=True).
    _write_claude_single_call(
        tmp_sessions_dir, "cmb-gitok",
        tool_name="Bash",
        tool_input={"command": "git status"},
        output="nothing to commit",
        is_error=False,
    )
    # non-git failure (filtered out by input_contains='git').
    _write_claude_single_call(
        tmp_sessions_dir, "cmb-lsfail",
        tool_name="Bash",
        tool_input={"command": "ls /nope"},
        output="ls: /nope: not found",
        is_error=True,
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)
    result = find_tool_calls(
        tool_name="Bash",
        agent="claude",
        input_contains="git",
        is_error=True,
    )
    assert result["count"] == 1
    assert result["records"][0]["session_uuid"] == "cmb-gitfail"


# ---------------------------------------------------------------------------
# is_error_reliable metadata
# ---------------------------------------------------------------------------


def test_find_tool_calls_is_error_reliable_claude(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude records carry ``is_error_reliable=True``."""
    _write_claude_session_with_outcomes(tmp_sessions_dir, "rel-claude")
    _patch_claude(tmp_sessions_dir, monkeypatch)
    result = find_tool_calls(tool_name="Bash", agent="claude")
    assert result["count"] == 2
    assert all(r["is_error_reliable"] is True for r in result["records"])


def test_find_tool_calls_is_error_reliable_codex_false(
    fake_codex_session_with_tools: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_sessions_dir: Path,
) -> None:
    """Codex records carry ``is_error_reliable=False`` (best-effort flag)."""
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir",
        lambda bd=None: [tmp_sessions_dir / ".codex" / "sessions"],
    )
    result = find_tool_calls(tool_name="shell", agent="codex")
    assert result["count"] == 1
    assert result["records"][0]["is_error_reliable"] is False


# ---------------------------------------------------------------------------
# Smart output truncation
# ---------------------------------------------------------------------------


def test_find_tool_calls_smart_truncation_keeps_trailing_error(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long ERROR output with the error line at the very end, is_error
    True -> adaptive smart mode surfaces the error line and flags
    ``output`` truncated."""
    from ai_r.find_tool_calls import _OUTPUT_CHARS_CAP

    long_body = "\n".join(f"log line {i}" for i in range(3000))
    tail_error = "FATAL: the database connection was refused"
    big = long_body + "\n" + tail_error
    assert len(big) > _OUTPUT_CHARS_CAP
    _write_claude_single_call(
        tmp_sessions_dir, "smart-err",
        tool_name="Bash",
        tool_input={"command": "boot"},
        output=big,
        is_error=True,
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)
    result = find_tool_calls(tool_name="Bash", agent="claude", is_error=True)
    assert result["count"] == 1
    hit = result["records"][0]
    assert tail_error in hit["output"]
    assert "output" in hit["truncated_fields"]
    assert len(hit["output"]) < len(big)


def test_find_tool_calls_success_default_head(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long SUCCESS output defaults to head-truncation: the beginning is
    preserved, marker at the end."""
    from ai_r.find_tool_calls import _OUTPUT_CHARS_CAP

    big = "START-MARKER " + ("x" * (_OUTPUT_CHARS_CAP + 500))
    _write_claude_single_call(
        tmp_sessions_dir, "head-ok",
        tool_name="Bash",
        tool_input={"command": "cat"},
        output=big,
        is_error=False,
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)
    result = find_tool_calls(tool_name="Bash", agent="claude")
    hit = result["records"][0]
    assert hit["output"].startswith("START-MARKER")
    assert hit["output"].endswith("…[truncated]")
    assert "output" in hit["truncated_fields"]


def test_find_tool_calls_output_mode_forced(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``output_mode`` overrides the adaptive default and yields
    the expected shape for head / tail / smart."""
    from ai_r.find_tool_calls import _OUTPUT_CHARS_CAP

    head_txt = "HEAD-START " + ("a" * (_OUTPUT_CHARS_CAP + 400)) + " TAIL-END"
    _write_claude_single_call(
        tmp_sessions_dir, "mode-x",
        tool_name="Bash",
        tool_input={"command": "run"},
        output=head_txt,
        is_error=False,
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)

    head = find_tool_calls(
        tool_name="Bash", agent="claude", output_mode="head"
    )["records"][0]["output"]
    assert head.startswith("HEAD-START")
    assert head.endswith("…[truncated]")

    tail = find_tool_calls(
        tool_name="Bash", agent="claude", output_mode="tail"
    )["records"][0]["output"]
    assert tail.startswith("…[truncated]")
    assert tail.endswith("TAIL-END")

    smart = find_tool_calls(
        tool_name="Bash", agent="claude", output_mode="smart"
    )["records"][0]["output"]
    # smart always keeps the tail.
    assert "TAIL-END" in smart
    assert "…[truncated]…" in smart


# ---------------------------------------------------------------------------
# Validation of new args
# ---------------------------------------------------------------------------


def test_find_tool_calls_empty_input_contains_raises() -> None:
    with pytest.raises(ValueError, match="input_contains"):
        find_tool_calls(tool_name="X", input_contains="  ")


def test_find_tool_calls_empty_output_contains_raises() -> None:
    with pytest.raises(ValueError, match="output_contains"):
        find_tool_calls(tool_name="X", output_contains="")


def test_find_tool_calls_empty_output_excludes_raises() -> None:
    with pytest.raises(ValueError, match="output_excludes"):
        find_tool_calls(tool_name="X", output_excludes="   ")


def test_find_tool_calls_bad_output_mode_raises() -> None:
    with pytest.raises(ValueError, match="output_mode"):
        find_tool_calls(tool_name="X", output_mode="bogus")


# ---------------------------------------------------------------------------
# MCP tool wrapper
# ---------------------------------------------------------------------------


def test_mcp_find_tool_calls_happy_path(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP wrapper returns the dict shape produced by the core scan."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-mcp",
        user_text="mcp intent",
        tool_name="Read",
        tool_input={"file_path": "/tmp/mcp/x.py"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    result = mcp_find_tool_calls(tool_name="Read", agent="claude")
    assert result["count"] == 1
    assert result["records"][0]["session_uuid"] == "ftc-mcp"
    assert result["records"][0]["intent"] == "mcp intent"


def test_mcp_find_tool_calls_invalid_arg_returns_error_dict() -> None:
    """Both/neither filter -> ``ValueError`` -> ``invalid_argument`` dict."""
    neither = mcp_find_tool_calls()
    assert isinstance(neither, dict)
    assert neither.get("error") == "invalid_argument"
    assert "exactly one" in neither["message"].lower()

    both = mcp_find_tool_calls(tool_name="X", tool_name_pattern="Y")
    assert isinstance(both, dict)
    assert both.get("error") == "invalid_argument"


def test_mcp_find_tool_calls_unknown_agent_returns_error_dict() -> None:
    result = mcp_find_tool_calls(tool_name="X", agent="mystery")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "agent" in result["message"].lower()


def test_mcp_find_tool_calls_new_filter_happy_path(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP wrapper passes new filters through to the core."""
    _write_claude_single_call(
        tmp_sessions_dir, "mcp-filt",
        tool_name="Bash",
        tool_input={"command": "git push"},
        output="error: failed to push",
        is_error=True,
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)
    result = mcp_find_tool_calls(
        tool_name="Bash",
        agent="claude",
        input_contains="git",
        is_error=True,
        output_mode="smart",
    )
    assert result["count"] == 1
    assert result["records"][0]["is_error"] is True
    assert result["records"][0]["is_error_reliable"] is True


def test_mcp_find_tool_calls_bad_output_mode_returns_error_dict() -> None:
    result = mcp_find_tool_calls(tool_name="X", output_mode="bogus")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "output_mode" in result["message"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_find_tool_calls_basic_human(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ai-r find-tool-calls <TOOL>`` prints the human-readable
    summary: tool name, intent, count line."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-cli-h",
        user_text="add header",
        tool_name="Edit",
        tool_input={"file_path": "/tmp/cli-h/README.md"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    rc, out, err = _run_inproc(
        ["find-tool-calls", "Edit", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "Edit" in out
    assert "add header" in out
    assert "1 tool call" in out


def test_cli_find_tool_calls_json(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` returns a dict with ``records``/``count``/``truncated``."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-cli-j",
        user_text="json test",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    rc, out, err = _run_inproc(
        ["find-tool-calls", "Bash", "--agent", "claude", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert "records" in payload
    assert "count" in payload
    assert "truncated" in payload
    assert payload["count"] == 1
    assert payload["records"][0]["tool"] == "Bash"
    assert payload["records"][0]["input"] == {"command": "ls"}


def test_cli_find_tool_calls_pattern(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--pattern`` does a substring match."""
    _write_claude_tool_session(
        tmp_sessions_dir, "ftc-cli-pat",
        user_text="search call",
        tool_name="mcp__ai-r__search_sessions",
        tool_input={"query": "x"},
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    rc, out, err = _run_inproc(
        ["find-tool-calls", "--pattern", "mcp__ai-r__", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["records"][0]["tool"] == "mcp__ai-r__search_sessions"


def test_cli_find_tool_calls_neither_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No filter at all -> ValueError -> exit 2."""
    rc, out, err = _run_inproc(
        ["find-tool-calls"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "exactly one" in err.lower()


def test_cli_find_tool_calls_both_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both filters set -> argparse mutex violation -> exit 2.

    Since the CLI subparser enforces mutual exclusion between the
    positional ``tool_name`` and ``--pattern`` at the argparse layer,
    providing both is rejected before the core is reached.
    """
    rc, out, err = _run_inproc(
        ["find-tool-calls", "Bash", "--pattern", "Bash"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "not allowed with argument" in err.lower()


def test_cli_find_tool_calls_bad_iso_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage ``--since`` -> exit 2 with an ISO message on stderr."""
    rc, out, err = _run_inproc(
        ["find-tool-calls", "Bash", "--since", "not-a-date"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "iso" in err.lower()


def test_cli_find_tool_calls_no_match_prints_stderr(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No records -> stderr message, exit 0."""
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    rc, out, err = _run_inproc(
        ["find-tool-calls", "DefinitelyNotATool"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert "no tool calls" in err.lower()


def test_cli_find_tool_calls_errors_only_success_only_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--errors-only`` and ``--success-only`` together -> argparse mutex
    violation -> exit 2."""
    rc, out, err = _run_inproc(
        ["find-tool-calls", "Bash", "--errors-only", "--success-only"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "not allowed with argument" in err.lower()


def test_cli_find_tool_calls_input_contains_errors_only_smart_json(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--input-contains git --errors-only --output-mode smart --json``
    produces valid JSON with just the matching failed git call."""
    _write_claude_single_call(
        tmp_sessions_dir, "cli-filt",
        tool_name="Bash",
        tool_input={"command": "git push"},
        output="error: failed to push",
        is_error=True,
    )
    _write_claude_single_call(
        tmp_sessions_dir, "cli-ok",
        tool_name="Bash",
        tool_input={"command": "git status"},
        output="clean",
        is_error=False,
    )
    _patch_claude(tmp_sessions_dir, monkeypatch)
    rc, out, err = _run_inproc(
        [
            "find-tool-calls", "Bash", "--agent", "claude",
            "--input-contains", "git", "--errors-only",
            "--output-mode", "smart", "--json",
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["count"] == 1
    rec = payload["records"][0]
    assert rec["session_uuid"] == "cli-filt"
    assert rec["is_error"] is True
    assert rec["is_error_reliable"] is True
