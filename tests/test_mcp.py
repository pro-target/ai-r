"""MCP-server tests using the in-memory client.

The in-memory transport avoids spawning a real stdio server, so the
suite stays fast and hermetic.  We still call the registered tool
functions exactly as a real MCP client would: through
``client.call_tool`` and ``client.list_tools``.

Helper functions (``_extract_messages``, ``_iso``, ``_coerce_agent`` etc.)
are imported and tested directly because the in-memory transport runs
the server in a side thread whose coverage is *not* attributed to the
test process.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp.shared.memory import create_connected_server_and_client_session

from ai_r.mcp_server import (
    _build_haystack,
    _BODY_SEARCH_MESSAGE_CAP,
    _codex_text,
    _coerce_agent,
    _extract_messages,
    _extract_snippet,
    _iso,
    _match,
    _MESSAGES_CAP,
    _MESSAGES_HARD_CAP,
    _parse_query,
    _pi_text,
    _session_summary,
    _target_agents,
    find_file_edits,
    get_body,
    list_sessions,
    mcp,
    plan,
    query,
    read_session,
    search_sessions,
)
from ai_r.parsers import AgentName


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _tool_names() -> list[str]:
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.list_tools()
        return [t.name for t in result.tools]


async def _call(name: str, args: dict) -> list[str]:
    """Invoke a tool and return the *text* of every content block."""
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(name, args)
        return [c.text for c in result.content]


def _run(coro):
    """Run a coroutine in a fresh event loop.  Avoids the deprecated
    ``asyncio.get_event_loop()`` semantics in Python 3.12+.
    """
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_mcp_tool_registration() -> None:
    names = _run(_tool_names())
    assert "list_sessions" in names
    assert "read_session" in names
    assert "search_sessions" in names

    # The internal _tool_manager dict mirrors the public surface and
    # is the actual call dispatch table.
    internals = set(mcp._tool_manager._tools.keys())
    assert {"list_sessions", "read_session", "search_sessions"} <= internals


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def test_mcp_list_sessions_claude() -> None:
    """Real sessions come back as summary dicts inside a pagination envelope."""
    texts = _run(_call("list_sessions", {"agent": "claude"}))
    envelopes = [json.loads(t) for t in texts if t.strip().startswith("{")]
    if envelopes:
        data = envelopes[0]
        assert data["offset"] == 0
        assert isinstance(data["total"], int)
        assert isinstance(data["truncated"], bool)
        sessions = data["sessions"]
        if sessions:
            s = sessions[0]
            assert "uuid" in s
            assert "agent" in s
            assert "title" in s
            assert "date" in s
            assert "message_count" in s
            assert s["agent"] == "CLAUDE"


# ---------------------------------------------------------------------------
# read_session
# ---------------------------------------------------------------------------


def _first_claude_uuid() -> str | None:
    base = Path("~/.claude/projects").expanduser()
    if not base.is_dir():
        return None
    for jsonl in base.glob("*/*.jsonl"):
        return jsonl.stem
    return None


def test_mcp_read_session_existing() -> None:
    uuid = _first_claude_uuid()
    if uuid is None:
        pytest.skip("no real Claude session on this host")
    # The autouse ``_isolate_ai_r_home`` fixture points
    # ``AI_R_HOME`` at an empty fake tree, so the Claude parser
    # would not find the real session.  Unset it for the duration of
    # this test.
    saved_home = os.environ.get("AI_R_HOME")
    os.environ.pop("AI_R_HOME", None)
    try:
        texts = _run(_call("read_session", {"uuid": uuid, "agent": "claude"}))
    finally:
        if saved_home is not None:
            os.environ["AI_R_HOME"] = saved_home

    payload = json.loads(texts[0])
    assert payload["uuid"] == uuid
    assert payload["agent"] == "CLAUDE"
    assert "messages" in payload
    assert isinstance(payload["messages"], list)


def test_mcp_read_session_invalid_uuid() -> None:
    """The empty uuid is caught first and reported as ``invalid_argument``."""
    texts = _run(_call("read_session", {"uuid": "", "agent": "claude"}))
    payload = json.loads(texts[0])
    assert payload.get("error") == "invalid_argument"


def test_mcp_read_session_unknown_agent() -> None:
    texts = _run(_call("read_session", {"uuid": "x", "agent": "mystery"}))
    payload = json.loads(texts[0])
    assert payload.get("error") == "invalid_argument"


# ---------------------------------------------------------------------------
# search_sessions
# ---------------------------------------------------------------------------


def test_mcp_search_claude() -> None:
    texts = _run(
        _call("search_sessions", {"query": "claude", "agent": "claude"})
    )
    payload = json.loads(texts[0])
    results = payload["results"]
    if results:
        m = results[0]
        assert "uuid" in m and "title" in m


def test_mcp_search_empty_query() -> None:
    """An empty query short-circuits to an empty result set."""
    texts = _run(_call("search_sessions", {"query": ""}))
    assert json.loads(texts[0]) == {"results": [], "count": 0}


# ---------------------------------------------------------------------------
# Server identity
# ---------------------------------------------------------------------------


def test_mcp_server_name() -> None:
    assert mcp.name == "ai-r"


# ---------------------------------------------------------------------------
# Direct unit tests for the helper functions.
#
# The in-memory transport runs the registered tools in a side thread
# whose coverage is not attributed to this process.  Calling the
# helpers (and the tool callables themselves) directly from the test
# thread restores the coverage attribution.
# ---------------------------------------------------------------------------


def test_iso_helper_naive_datetime() -> None:
    """Naive datetimes are emitted with a trailing ``Z``."""
    assert _iso(datetime(2026, 6, 14, 10, 0, 0)) == "2026-06-14T10:00:00Z"


def test_iso_helper_aware_datetime() -> None:
    """Aware datetimes use their own offset."""
    dt = datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
    assert _iso(dt) == "2026-06-14T10:00:00+00:00"


def test_coerce_agent_lowercases() -> None:
    assert _coerce_agent("CLAUDE") is AgentName.CLAUDE
    assert _coerce_agent("codex") is AgentName.CODEX
    assert _coerce_agent(" OpenCode ") is AgentName.OPENCODE
    assert _coerce_agent("pi") is AgentName.PI
    with pytest.raises(ValueError):
        _coerce_agent("mystery")
    with pytest.raises(ValueError):
        _coerce_agent("")


def test_target_agents_all_when_none() -> None:
    """Omitting ``agent`` returns every supported agent."""
    targets = _target_agents(None)
    assert set(targets) == set(AgentName)
    assert _target_agents("") == list(AgentName)


def test_target_agents_single() -> None:
    targets = _target_agents("claude")
    assert targets == [AgentName.CLAUDE]


def test_session_summary_projects_fields() -> None:
    from ai_r.parsers.models import Session
    s = Session(
        uuid="abc",
        agent=AgentName.CLAUDE,
        title="hello",
        date=datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        path="/x",
        message_count=3,
    )
    summary = _session_summary(s)
    assert summary["uuid"] == "abc"
    assert summary["agent"] == "CLAUDE"
    assert summary["title"] == "hello"
    assert summary["date"] == "2026-06-14T10:00:00+00:00"
    assert summary["message_count"] == 3


def test_codex_text_variants() -> None:
    assert _codex_text([{"type": "text", "text": "a"}]) == "a"
    assert _codex_text([{"text": "no-type"}]) == "no-type"
    # Strings are passed through unchanged.
    assert _codex_text("just a string") == "just a string"
    # None / non-iterable scalars return empty.
    assert _codex_text(None) == ""  # type: ignore[arg-type]
    assert _codex_text(123) == ""  # type: ignore[arg-type]
    assert _codex_text([]) == ""


def test_pi_text_variants() -> None:
    assert _pi_text([{"type": "text", "text": "a"}]) == "a"
    assert _pi_text([{"type": "thinking", "thinking": "ignored"}]) == ""
    assert _pi_text([{"text": "no-type"}]) == "no-type"
    assert _pi_text("just a string") == "just a string"
    assert _pi_text(None) == ""  # type: ignore[arg-type]


# --- _extract_messages dispatcher ------------------------------------------


def test_extract_messages_dispatches_to_claude(
    fake_claude_session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatcher routes Claude sessions through read_messages(uuid)."""
    from ai_r.parsers.models import Session

    base = fake_claude_session.parent.parent  # .../projects (parent of proj-a)
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: base
    )
    s = Session(
        uuid="test-claude-1",
        agent=AgentName.CLAUDE,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path=str(fake_claude_session),
        message_count=1,
    )
    assert _extract_messages(s)[0]["content"] == "Hello, world"


def test_extract_messages_surfaces_tool_input_and_intent(
    fake_claude_session_with_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature 1: tool_use content shows the call input + assistant intent.

    The fixture's assistant message runs ``Bash`` with
    ``{"command": "pytest"}`` after a user "Run the tests" turn, so the
    projected assistant message must (a) embed the command in its content
    and (b) carry ``intent`` = the previous user request.
    """
    from ai_r.parsers.models import Session

    base = fake_claude_session_with_tools.parent.parent  # .../projects
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: base
    )
    s = Session(
        uuid="claude-tools-1",
        agent=AgentName.CLAUDE,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path=str(fake_claude_session_with_tools),
        message_count=3,
    )
    msgs = _extract_messages(s)
    assistant = next(m for m in msgs if m["role"] == "assistant")
    # Feature 1.1: the Bash command (the key input) is surfaced, not a
    # bare ``[tool_use: Bash]`` placeholder.
    assert "Bash" in assistant["content"]
    assert "pytest" in assistant["content"]
    assert "command=" in assistant["content"]
    # Feature 1.2: intent = the previous user request.
    assert assistant.get("intent") == "Run the tests"


def test_extract_messages_surfaces_timestamp(
    fake_claude_session_with_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature 2: every projected message carries an ISO timestamp."""
    from ai_r.parsers.models import Session

    base = fake_claude_session_with_tools.parent.parent  # .../projects
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: base
    )
    s = Session(
        uuid="claude-tools-1",
        agent=AgentName.CLAUDE,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path=str(fake_claude_session_with_tools),
        message_count=3,
    )
    msgs = _extract_messages(s)
    assistant = next(m for m in msgs if m["role"] == "assistant")
    ts = assistant["timestamp"]
    assert ts  # non-empty ISO string
    assert ts.startswith("2026-06-14T10:00:05")


def test_extract_messages_dispatches_to_codex(
    fake_codex_session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatcher routes Codex sessions through read_messages(uuid)."""
    from ai_r.parsers.models import Session

    base = fake_codex_session.parent.parent.parent  # .../sessions
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [base]
    )
    s = Session(
        uuid="test-codex-1",
        agent=AgentName.CODEX,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path=str(fake_codex_session),
        message_count=1,
    )
    msgs = _extract_messages(s)
    assert msgs
    assert msgs[0]["role"] == "user"


def test_extract_messages_dispatches_to_pi(
    fake_pi_session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatcher routes Pi sessions through read_messages(uuid)."""
    from ai_r.parsers.models import Session

    base = fake_pi_session.parent  # .../sessions/--tmp-work--
    monkeypatch.setattr(
        "ai_r.parsers.pi._resolve_base_dir", lambda bd=None: base
    )
    s = Session(
        uuid="test-pi-1",
        agent=AgentName.PI,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path=str(fake_pi_session),
        message_count=1,
    )
    msgs = _extract_messages(s)
    assert msgs
    assert msgs[0]["content"] == "Add Pi support"


def test_extract_messages_dispatches_to_opencode(
    fake_opencode_db_with_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenCode dispatches via the parser registry; {role, content} only."""
    monkeypatch.setenv("OPENCODE_DB", str(fake_opencode_db_with_tools))
    from ai_r.parsers.models import Session

    s = Session(
        uuid="oc-tools-1",
        agent=AgentName.OPENCODE,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path=str(fake_opencode_db_with_tools),
        message_count=3,
    )
    msgs = _extract_messages(s)
    assert msgs, "expected non-empty opencode messages"
    for m in msgs:
        assert {"role", "content"} <= set(m.keys())
        assert set(m.keys()) <= {"role", "content", "timestamp", "intent", "qa"}
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]


def test_extract_messages_dispatches_to_antigravity(
    fake_antigravity_brain: Path,
) -> None:
    """Antigravity dispatches via the parser registry; {role, content} only.

    The autouse ``_isolate_ai_r_home`` fixture points
    ``AI_R_HOME`` at the same fake tree ``fake_antigravity_brain``
    builds under, so the parser's ``read_messages`` finds the brain.
    """
    from ai_r.parsers.models import Session

    s = Session(
        uuid="test-ag-1",
        agent=AgentName.ANTIGRAVITY,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path=str(fake_antigravity_brain),
        message_count=2,
    )
    msgs = _extract_messages(s)
    assert msgs, "expected non-empty antigravity messages"
    for m in msgs:
        assert {"role", "content"} <= set(m.keys())
        assert set(m.keys()) <= {"role", "content", "timestamp", "intent", "qa"}


def test_extract_messages_all_agents_dispatch() -> None:
    """Every supported agent resolves to a parser in the registry.

    The dispatcher has no "unsupported agent" path anymore — all 5
    agents route through ``_PARSERS``.  A nonexistent uuid yields ``[]``
    via the try/except, not via a missing-parser branch.
    """
    from ai_r.parsers.models import Session

    for agent in AgentName:
        s = Session(
            uuid="never-real-xyzzy",
            agent=agent,
            title="t",
            date=datetime.now(tz=timezone.utc),
            path="/nonexistent",
            message_count=0,
        )
        result = _extract_messages(s)
        assert isinstance(result, list)


# --- list_sessions / read_session / search_sessions error paths -----------


def test_list_sessions_invalid_agent_returns_error_dict() -> None:
    """An unknown ``agent`` is surfaced as a structured error dict."""
    result = list_sessions(agent="mystery")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"


def test_list_sessions_invalid_limit() -> None:
    result = list_sessions(agent="claude", limit=-1)
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "limit" in result["message"].lower()


def test_list_sessions_invalid_offset() -> None:
    result = list_sessions(agent="claude", offset=-3)
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "offset" in result["message"].lower()


def test_list_sessions_paginates_and_reports_total(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit``/``offset`` page the results; ``total``/``truncated`` reflect
    the full matching set, not just the page."""
    for i in range(5):
        _write_claude_body_session(
            tmp_sessions_dir=tmp_sessions_dir,
            uuid=f"page-{i}",
            user_text=f"user {i} text",
            title=f"page session {i}",
        )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )

    first_page = list_sessions(agent="claude", limit=2, offset=0)
    assert isinstance(first_page, dict)
    assert first_page["total"] == 5
    assert first_page["offset"] == 0
    assert first_page["limit"] == 2
    assert len(first_page["sessions"]) == 2
    assert first_page["truncated"] is True

    second_page = list_sessions(agent="claude", limit=2, offset=2)
    assert len(second_page["sessions"]) == 2
    assert second_page["truncated"] is True

    last_page = list_sessions(agent="claude", limit=2, offset=4)
    assert len(last_page["sessions"]) == 1
    assert last_page["truncated"] is False

    # pages don't overlap
    uuids_a = {s["uuid"] for s in first_page["sessions"]}
    uuids_b = {s["uuid"] for s in second_page["sessions"]}
    assert uuids_a.isdisjoint(uuids_b)


def test_list_sessions_limit_zero_is_uncapped(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit=0`` returns every session in one page."""
    for i in range(3):
        _write_claude_body_session(
            tmp_sessions_dir=tmp_sessions_dir,
            uuid=f"uncapped-{i}",
            user_text=f"user {i} text",
            title=f"uncapped session {i}",
        )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )

    result = list_sessions(agent="claude", limit=0)
    assert result["total"] == 3
    assert result["limit"] == 0
    assert len(result["sessions"]) == 3
    assert result["truncated"] is False


def test_list_sessions_surfaces_subagent_kind_and_parent(
    fake_claude_subagent: Path,
    tmp_sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent session is summarised with ``kind`` + ``parent_uuid``."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = list_sessions(agent="claude")
    assert result["total"] == 1
    s = result["sessions"][0]
    assert s["uuid"] == "agent-sub-1"
    assert s["kind"] == "subagent"
    assert s["parent_uuid"] == "parent-claude-1"


def test_list_sessions_kind_filter(
    fake_claude_subagent: Path,
    fake_claude_session: Path,
    tmp_sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kind=`` selects only agents / only subagents; omitted returns both."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )

    both = list_sessions(agent="claude")
    assert both["total"] == 2

    only_sub = list_sessions(agent="claude", kind="subagent")
    assert only_sub["total"] == 1
    assert only_sub["sessions"][0]["uuid"] == "agent-sub-1"
    assert only_sub["sessions"][0]["kind"] == "subagent"

    only_agent = list_sessions(agent="claude", kind="agent")
    assert only_agent["total"] == 1
    assert only_agent["sessions"][0]["uuid"] == "test-claude-1"
    assert only_agent["sessions"][0]["kind"] == "agent"


def test_list_sessions_invalid_kind_returns_error_dict() -> None:
    """An unknown ``kind`` is surfaced as a structured error dict."""
    result = list_sessions(agent="claude", kind="banana")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "kind" in result["message"].lower()


def test_read_session_invalid_uuid_returns_error_dict() -> None:
    result = read_session(uuid="", agent="claude")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"


def test_read_session_unknown_agent_returns_error_dict() -> None:
    result = read_session(uuid="x", agent="mystery")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"


def test_read_session_not_found_returns_error_dict() -> None:
    """A nonexistent uuid -> ``not_found`` error dict."""
    # Disable AI_R_HOME so the parser looks at the real tree,
    # then ask for a uuid that is not in it.
    saved_home = os.environ.get("AI_R_HOME")
    os.environ.pop("AI_R_HOME", None)
    try:
        result = read_session(
            uuid="definitely-not-a-real-uuid-xyzzy",
            agent="claude",
        )
    finally:
        if saved_home is not None:
            os.environ["AI_R_HOME"] = saved_home
    assert isinstance(result, dict)
    assert result.get("error") == "not_found"
    assert result.get("agent") == "CLAUDE"


def test_search_sessions_empty_query_returns_empty() -> None:
    """An empty query short-circuits to an empty result set."""
    assert search_sessions(query="") == {"results": [], "count": 0}


def test_search_sessions_invalid_agent_returns_error_dict() -> None:
    result = search_sessions(query="x", agent="mystery")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"


# ---------------------------------------------------------------------------
# read_session regression: capped {role, content} list via the public API
# ---------------------------------------------------------------------------


def test_read_session_returns_capped_role_content_list(
    fake_claude_session: Path, tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``read_session`` must still return a list of ``{role, content}`` dicts
    (no tool_use/tool_result keys leak into MCP output).  Pagination
    fields ``total``/``offset``/``limit`` ride along; the default limit
    echoes :data:`_MESSAGES_CAP` but is no longer a silent hard cap."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = read_session(uuid="test-claude-1", agent="claude")
    assert isinstance(result, dict)
    assert "error" not in result
    msgs = result["messages"]
    assert isinstance(msgs, list)
    assert len(msgs) == 2
    for m in msgs:
        assert {"role", "content"} <= set(m.keys())
        assert set(m.keys()) <= {"role", "content", "timestamp", "intent", "qa"}
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello, world"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "Hi there!"
    # Pagination fields.
    assert result["total"] == 2
    assert result["offset"] == 0
    assert result["limit"] == _MESSAGES_CAP


def test_read_session_pagination_default_returns_all(
    fake_claude_session: Path, tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default offset=0/limit=_MESSAGES_CAP returns the full list."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = read_session(uuid="test-claude-1", agent="claude")
    assert result["total"] == len(result["messages"])
    assert result["offset"] == 0


def test_read_session_pagination_offset_limit_slice(
    fake_claude_session: Path, tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """offset/limit slice the projected message list."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    # offset=1 skips the user message; limit=10 leaves the assistant msg.
    result = read_session(uuid="test-claude-1", agent="claude", offset=1, limit=10)
    msgs = result["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert result["total"] == 2
    assert result["offset"] == 1
    assert result["limit"] == 10


def test_read_session_pagination_limit_caps_at_more_than_100(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session with 150 user messages: default limit returns 100, total
    reports 150, limit>100 returns all.  Proves the cap is no longer
    silent/hard."""
    # Synthesize a Claude session with 150 user messages.
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": f"msg-{i}"},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": "big-1",
        }
        for i in range(150)
    ]
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-big" / "big-1.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )

    default_capped = read_session(uuid="big-1", agent="claude")
    assert default_capped["total"] == 150
    assert len(default_capped["messages"]) == _MESSAGES_CAP  # 100
    assert default_capped["messages"][0]["content"] == "msg-0"
    assert default_capped["messages"][-1]["content"] == f"msg-{_MESSAGES_CAP - 1}"

    # Raise the limit → get everything.
    all_msgs = read_session(uuid="big-1", agent="claude", limit=0)
    assert all_msgs["total"] == 150
    assert len(all_msgs["messages"]) == 150

    # offset past the end → empty list, total still 150.
    tail = read_session(uuid="big-1", agent="claude", offset=145, limit=10)
    assert tail["total"] == 150
    assert len(tail["messages"]) == 5
    assert tail["messages"][0]["content"] == "msg-145"


def test_read_session_pagination_negative_offset_rejected(
    fake_claude_session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = str(fake_claude_session.parent.parent)
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = read_session(uuid="test-claude-1", agent="claude", offset=-1)
    assert result.get("error") == "invalid_argument"


def test_read_session_claude_tool_only_messages_not_blank(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        {
            "type": "ai-title",
            "aiTitle": "Tool only projection",
            "timestamp": "2026-06-14T09:59:59Z",
            "sessionId": "tool-only-1",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": "ls"}],
            },
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": "tool-only-1",
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "ok"}],
            },
            "timestamp": "2026-06-14T10:00:01Z",
            "sessionId": "tool-only-1",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
            },
            "timestamp": "2026-06-14T10:00:02Z",
            "sessionId": "tool-only-1",
        },
    ]
    jsonl = (
        tmp_sessions_dir
        / ".claude"
        / "projects"
        / "proj-tools"
        / "tool-only-1.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )

    result = read_session(uuid="tool-only-1", agent="claude")
    msgs = result["messages"]
    # tool_use input "ls" is not valid JSON → raw-string fallback, so the
    # call surfaces as "[tool_use: Bash ls]".  timestamps are now attached.
    # The result-only user record now surfaces the call outcome instead of a
    # bare ``[tool_result]`` placeholder: a successful result renders as
    # ``[tool_result ok: <snippet>]`` so read_session can tell success/error.
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("assistant", "[tool_use: Bash ls]"),
        ("user", "[tool_result ok: ok]"),
        ("assistant", "done"),
    ]
    assert msgs[0]["timestamp"] == "2026-06-14T10:00:00+00:00"
    assert all(m["content"] for m in msgs)


def test_read_session_claude_tool_error_surfaced(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed tool call renders as ``[tool_result ERROR: <snippet>]`` so
    read_session can answer "did this actually work?"."""
    records = [
        {
            "type": "ai-title",
            "aiTitle": "Tool error projection",
            "timestamp": "2026-06-14T09:59:59Z",
            "sessionId": "tool-err-1",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                             "input": {"command": "pytest"}}],
            },
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": "tool-err-1",
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": "Traceback: boom\nAssertionError"},
            ]},
            "timestamp": "2026-06-14T10:00:01Z",
            "sessionId": "tool-err-1",
        },
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-err" / "tool-err-1.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = read_session(uuid="tool-err-1", agent="claude")
    contents = [m["content"] for m in result["messages"]]
    err = next(c for c in contents if c.startswith("[tool_result ERROR"))
    assert "Traceback: boom" in err
    assert "ok" not in err


def test_read_session_mcp_drops_tool_messages(
    fake_pi_session: Path, tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi toolResult records become ``tool`` Message objects; MCP output
    must NOT surface them (only user/assistant), preserving the historical
    output shape."""
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.pi._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = read_session(uuid="test-pi-1", agent="pi")
    msgs = result["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    for m in msgs:
        assert {"role", "content"} <= set(m.keys())
        assert set(m.keys()) <= {"role", "content", "timestamp", "intent", "qa"}


def test_message_and_read_messages_reexported() -> None:
    """``Message`` and the per-parser ``read_messages`` are public API."""
    from ai_r.parsers import Message, antigravity, claude, codex, opencode, pi

    sample = Message(role="user", text="hi")
    assert sample.role == "user"
    assert sample.tool_use == ()
    assert sample.tool_result == ()
    for mod in (claude, codex, opencode, antigravity, pi):
        assert callable(getattr(mod, "read_messages"))


def test_messages_cap_constant_unchanged() -> None:
    from ai_r.mcp_server import _MESSAGES_CAP

    assert _MESSAGES_CAP == 100


def test_extract_messages_hard_caps_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_r.parsers.models import Message, Session

    monkeypatch.setattr(
        "ai_r.parsers.claude.read_messages",
        lambda _uuid: [
            Message(role="user", text=f"message {i}")
            for i in range(_MESSAGES_HARD_CAP + 5)
        ],
    )
    session = Session(
        uuid="cap-me",
        agent=AgentName.CLAUDE,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path="/tmp/fake.jsonl",
        message_count=_MESSAGES_HARD_CAP + 5,
    )

    result = _extract_messages(session, limit=0)

    assert len(result) == _MESSAGES_HARD_CAP
    assert result[-1]["content"] == f"message {_MESSAGES_HARD_CAP - 1}"


# ---------------------------------------------------------------------------
# search_sessions: extended API (scope/operator/limit, body search)
# ---------------------------------------------------------------------------


def _write_claude_body_session(
    tmp_sessions_dir: Path,
    uuid: str,
    user_text: str,
    assistant_blocks: list | None = None,
    title: str = "Body search test",
) -> None:
    """Write a Claude JSONL session with a known user message + optional tool calls.

    ``assistant_blocks`` lets the caller inject structured content
    (tool_use, text, etc.) into the assistant turn; defaults to a plain
    text reply.  The session gets an ``ai-title`` event so the resolved
    title is deterministic regardless of the user text.
    """
    records: list[dict] = [
        {
            "type": "ai-title",
            "aiTitle": title,
            "timestamp": "2026-06-14T09:59:59Z",
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
    ]
    if assistant_blocks is None:
        assistant_blocks = [
            {"type": "text", "text": "ok"},
        ]
    records.append(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": assistant_blocks},
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": uuid,
        }
    )
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-x" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_search_sessions_backward_compat_title_only(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default args (scope=title, operator=AND) preserve the historical
    title-substring behaviour and do not surface a ``snippet`` key."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="bcc-1",
        user_text="hello world",
        title="claude indexer notes",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions("claude")
    assert isinstance(result, dict)
    rows = result["results"]
    assert rows, "expected at least one match against the title"
    assert result["count"] == len(rows)
    for s in rows:
        assert "snippet" not in s
        assert "claude" in s["title"].lower()


def test_search_sessions_body_and_match(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope=body + AND (default) finds a session whose message text
    contains both required terms, and surfaces a ``snippet``."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="body-and-1",
        user_text="please add a pwa manifest for the dashboard",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions(
        "pwa manifest", agent="claude", scope="body"
    )
    assert isinstance(result, dict)
    assert result["results"], "expected body match"
    matched = [s for s in result["results"] if s["uuid"] == "body-and-1"]
    assert matched, "the synthesized session must be in the results"
    assert "snippet" in matched[0]
    assert "pwa manifest" in matched[0]["snippet"].lower()


def test_search_sessions_body_or_match(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope=body + OR matches when at least one positive term appears."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="body-or-1",
        user_text="please add a pwa manifest for the dashboard",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions(
        "pwa ipsum",
        agent="claude",
        scope="body",
        operator="OR",
    )
    matched = [s for s in result["results"] if s["uuid"] == "body-or-1"]
    assert matched, "OR: only pwa appears, must still match"


def test_search_sessions_body_marks_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_r.parsers.models import Message, Session

    session = Session(
        uuid="body-truncated-1",
        agent=AgentName.CLAUDE,
        title="t",
        date=datetime.now(tz=timezone.utc),
        path="/tmp/fake.jsonl",
        message_count=_BODY_SEARCH_MESSAGE_CAP + 1,
    )

    monkeypatch.setattr(
        "ai_r.parsers.claude.list_sessions",
        lambda: [session],
    )
    monkeypatch.setattr(
        "ai_r.parsers.claude.read_messages",
        lambda _uuid: [
            Message(role="user", text="needle")
            for _ in range(_BODY_SEARCH_MESSAGE_CAP + 1)
        ],
    )

    result = search_sessions("needle", agent="claude", scope="body")

    rows = result["results"]
    assert rows
    assert rows[0]["uuid"] == "body-truncated-1"
    assert rows[0]["body_truncated"] is True


def test_search_sessions_body_not_match(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """operator=NOT excludes any session containing the term."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="body-not-1",
        user_text="please add a pwa manifest for the dashboard",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions(
        "pwa", agent="claude", scope="body", operator="NOT"
    )
    matched = [s for s in result["results"] if s["uuid"] == "body-not-1"]
    assert not matched, "NOT: pwa is in the body, must not match"


def test_search_sessions_body_negative_prefix(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Google-style ``-term`` excludes a session when the negative term
    appears in the body, regardless of positive matches."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="body-neg-1",
        user_text="please add a pwa manifest for the dashboard",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    excluded = search_sessions(
        "pwa -manifest", agent="claude", scope="body"
    )
    assert not [s for s in excluded["results"] if s["uuid"] == "body-neg-1"], (
        "-manifest must exclude a session that contains manifest"
    )
    included = search_sessions(
        "pwa -claude", agent="claude", scope="body"
    )
    assert [s for s in included["results"] if s["uuid"] == "body-neg-1"], (
        "-claude must NOT exclude a session that has no 'claude' in body"
    )


def test_search_sessions_all_negative_prefix_excludes_title(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope=all applies ``-term`` to title matches, not just body matches."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="all-neg-title-1",
        user_text="body only says pwa",
        title="pwa claude title",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions(
        "pwa -claude", agent="claude", scope="all"
    )
    assert not [s for s in result["results"] if s["uuid"] == "all-neg-title-1"]


def test_search_sessions_body_quoted_phrase(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quoted phrases are matched as a single literal term."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="body-quote-1",
        user_text="the secret token is foo bar baz and you must keep it safe",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions('"foo bar"', agent="claude", scope="body")
    matched = [s for s in result["results"] if s["uuid"] == "body-quote-1"]
    assert matched, "quoted phrase 'foo bar' must be located as a single term"


def test_search_sessions_body_tool_use_match(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Body search descends into ``tool_use[*].input`` so invocations
    buried in tool calls are discoverable."""
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="body-tool-1",
        user_text="run the tests please",
        assistant_blocks=[
            {"type": "text", "text": "Running."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
        ],
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions("pytest", agent="claude", scope="body")
    matched = [s for s in result["results"] if s["uuid"] == "body-tool-1"]
    assert matched, "tool_use input must be searchable"
    assert "snippet" in matched[0]


def test_search_sessions_limit(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit`` caps the number of results returned."""
    # Synthesize 5 distinct Claude sessions; only the title differs
    # because body+title are also matched by 'limitcap'.
    for i in range(5):
        _write_claude_body_session(
            tmp_sessions_dir=tmp_sessions_dir,
            uuid=f"limitcap-{i}",
            user_text=f"user {i} text",
            title=f"limitcap session {i}",
        )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions("limitcap", agent="claude", limit=2)
    assert len(result["results"]) <= 2
    assert result["count"] == len(result["results"])


# ---------------------------------------------------------------------------
# search_sessions: relevance ranking (BM25) + sort modes
# ---------------------------------------------------------------------------


def _write_claude_dated_body_session(
    tmp_sessions_dir: Path,
    uuid: str,
    user_text: str,
    when: str,
    title: str = "ranking test",
) -> None:
    """Like ``_write_claude_body_session`` but with a caller-set timestamp.

    Lets a test control both *recency* (``when``) and *relevance*
    (``user_text``) so the two can be played off against each other.
    """
    records = [
        {
            "type": "ai-title",
            "aiTitle": title,
            "timestamp": when,
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": when,
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            "timestamp": when,
            "sessionId": uuid,
        },
    ]
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-x" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_search_sessions_relevance_beats_recency(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A less-recent but more-relevant session ranks above a fresher but
    barely-relevant one when ``sort='relevance'`` (the default)."""
    # OLDER session: term appears many times (high relevance).
    _write_claude_dated_body_session(
        tmp_sessions_dir,
        uuid="rank-relevant-old",
        user_text="kafka kafka kafka kafka pipeline",
        when="2026-01-01T10:00:00Z",
        title="old",
    )
    # NEWER session: term appears once amid lots of noise (low relevance).
    _write_claude_dated_body_session(
        tmp_sessions_dir,
        uuid="rank-fresh-new",
        user_text="kafka " + " ".join(f"noise{i}" for i in range(60)),
        when="2026-06-20T10:00:00Z",
        title="new",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions("kafka", agent="claude", scope="body")
    uuids = [s["uuid"] for s in result["results"]]
    assert set(uuids) == {"rank-relevant-old", "rank-fresh-new"}
    # Relevance default: the older-but-denser match wins despite the other
    # being newer.
    assert uuids[0] == "rank-relevant-old"


def test_search_sessions_sort_date_reproduces_pre_reform_order(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sort='date'`` restores newest-first order regardless of BM25
    score (the historical pre-ranking behaviour)."""
    _write_claude_dated_body_session(
        tmp_sessions_dir,
        uuid="date-relevant-old",
        user_text="kafka kafka kafka kafka pipeline",
        when="2026-01-01T10:00:00Z",
        title="old",
    )
    _write_claude_dated_body_session(
        tmp_sessions_dir,
        uuid="date-fresh-new",
        user_text="kafka " + " ".join(f"noise{i}" for i in range(60)),
        when="2026-06-20T10:00:00Z",
        title="new",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions("kafka", agent="claude", scope="body", sort="date")
    uuids = [s["uuid"] for s in result["results"]]
    # Newest-first: the fresher session leads even though it's less relevant.
    assert uuids == ["date-fresh-new", "date-relevant-old"]


def test_search_sessions_limit_applied_after_ranking(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit`` keeps the TOP-ranked matches, not the newest ones.

    The most relevant session is the oldest, so a date-truncating limit
    would drop it; a rank-then-limit keeps it.
    """
    # Most relevant, but oldest -> would be dropped by a date-order limit.
    _write_claude_dated_body_session(
        tmp_sessions_dir,
        uuid="lim-top-old",
        user_text="kafka kafka kafka kafka kafka pipeline",
        when="2026-01-01T10:00:00Z",
        title="old",
    )
    # Two fresher, far less relevant sessions.
    _write_claude_dated_body_session(
        tmp_sessions_dir,
        uuid="lim-mid-new",
        user_text="kafka " + " ".join(f"noise{i}" for i in range(80)),
        when="2026-06-20T10:00:00Z",
        title="newer",
    )
    _write_claude_dated_body_session(
        tmp_sessions_dir,
        uuid="lim-low-newest",
        user_text="kafka " + " ".join(f"chaff{i}" for i in range(120)),
        when="2026-06-25T10:00:00Z",
        title="newest",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions("kafka", agent="claude", scope="body", limit=1)
    assert len(result["results"]) == 1
    # The single survivor is the top-ranked (oldest, densest) session —
    # proof that the limit ran AFTER ranking, not before.
    assert result["results"][0]["uuid"] == "lim-top-old"


def test_search_sessions_invalid_sort() -> None:
    result = search_sessions("x", sort="bogus")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "sort" in result["message"].lower()


def test_search_sessions_invalid_scope() -> None:
    result = search_sessions("x", scope="bogus")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "scope" in result["message"].lower()


def test_search_sessions_invalid_operator() -> None:
    result = search_sessions("x", operator="XOR")
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "operator" in result["message"].lower()


def test_search_sessions_invalid_limit() -> None:
    result = search_sessions("x", limit=-1)
    assert isinstance(result, dict)
    assert result.get("error") == "invalid_argument"
    assert "limit" in result["message"].lower()


def test_search_sessions_snippet_truncated(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long body yields a ``snippet`` of at most 200 characters."""
    filler = "x" * 500
    _write_claude_body_session(
        tmp_sessions_dir=tmp_sessions_dir,
        uuid="body-long-1",
        user_text=f"the {filler} needle {filler} end",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = search_sessions("needle", agent="claude", scope="body")
    matched = [s for s in result["results"] if s["uuid"] == "body-long-1"]
    assert matched
    snippet = matched[0]["snippet"]
    assert len(snippet) <= 200
    assert "needle" in snippet


# ---------------------------------------------------------------------------
# Direct unit tests for the new helpers
# ---------------------------------------------------------------------------


def test_parse_query_basic() -> None:
    assert _parse_query("pwa manifest") == (["pwa", "manifest"], [])


def test_parse_query_boolean_words_are_literal() -> None:
    assert _parse_query("pwa OR manifest") == (["pwa", "or", "manifest"], [])


def test_parse_query_negative() -> None:
    assert _parse_query("pwa -claude") == (["pwa"], ["claude"])


def test_parse_query_quoted() -> None:
    assert _parse_query('"foo bar" baz') == (["foo bar", "baz"], [])


def test_parse_query_empty() -> None:
    assert _parse_query("") == ([], [])
    assert _parse_query(None or "") == ([], [])  # type: ignore[arg-type]


def test_match_and_or_not() -> None:
    hay = "the quick brown fox"
    assert _match(hay, ["quick", "fox"], [], "AND") is True
    assert _match(hay, ["quick", "missing"], [], "AND") is False
    assert _match(hay, ["quick", "missing"], [], "OR") is True
    assert _match(hay, ["missing", "absent"], [], "OR") is False
    assert _match(hay, ["quick", "fox"], [], "NOT") is False
    assert _match(hay, ["alpha", "beta"], [], "NOT") is True


def test_match_negative_filter_in_all_operators() -> None:
    """Negative terms must be excluded regardless of operator."""
    hay = "the quick brown fox"
    # AND: positive present, but negative present -> exclude
    assert _match(hay, ["quick"], ["fox"], "AND") is False
    # OR: positive present, but negative present -> exclude
    assert _match(hay, ["quick"], ["fox"], "OR") is False
    # NOT: term present (positive or negative) -> exclude
    assert _match(hay, [], ["fox"], "NOT") is False
    assert _match(hay, ["alpha"], ["fox"], "NOT") is False
    # Sanity: nothing in hay, only negative.
    assert _match("nothing", [], ["fox"], "AND") is True
    assert _match("nothing", [], ["fox"], "NOT") is True


def test_build_haystack_includes_tool_use_and_result() -> None:
    """Haystack must contain text + tool_use input + tool_result content."""
    from ai_r.parsers.models import Message

    msgs = [
        Message(
            role="user",
            text="plain user text",
            tool_use=(),
            tool_result=(),
        ),
        Message(
            role="assistant",
            text="",
            tool_use=({"name": "Bash", "input": '{"command": "pytest"}'},),
            tool_result=(),
        ),
        Message(
            role="user",
            text="",
            tool_use=(),
            tool_result=({"content": "5 passed"},),
        ),
    ]
    haystack = _build_haystack(msgs)
    assert "plain user text" in haystack
    assert "pytest" in haystack
    assert "5 passed" in haystack


def test_build_haystack_caps_chars() -> None:
    from ai_r.parsers.models import Message

    msgs = [
        Message(role="user", text="abc"),
        Message(role="assistant", text="def"),
    ]

    assert _build_haystack(msgs, max_chars=4) == "abc\n"


def test_extract_snippet_centers_on_term() -> None:
    hay = "lorem ipsum dolor sit amet, consectetur adipiscing elit"
    snippet = _extract_snippet(hay, ["consectetur"], max_len=200)
    assert "consectetur" in snippet
    # The snippet is lowercased (haystack was lowercased by _build_haystack
    # in real usage; the helper itself does not lowercase the input).
    assert "dolor" in snippet or "adipiscing" in snippet
    # No match -> empty string.
    assert _extract_snippet("nothing here", ["nope"]) == ""


# ---------------------------------------------------------------------------
# find_file_edits
# ---------------------------------------------------------------------------


def _write_claude_edit_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    user_text: str,
    edit_path: str,
    old_string: str = "old",
    new_string: str = "new",
    ts_user: str = "2026-06-14T10:00:00Z",
    ts_edit: str = "2026-06-14T10:00:05Z",
) -> None:
    """Write a Claude JSONL with one user message + one assistant Edit call."""
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
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-fe" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_pi_edit_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    user_text: str,
    edit_path: str,
    user_ts_ms: int = 1_718_360_002_000,
    edit_ts_ms: int = 1_718_360_004_000,
) -> None:
    """Write a Pi JSONL with a str_replace tool call (Pi-style Edit)."""
    jsonl = (
        tmp_sessions_dir
        / ".pi"
        / "agent"
        / "sessions"
        / "--tmp-fe--"
        / f"2026-06-14T10-00-00-000Z_{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "session",
            "id": uuid,
            "timestamp": "2026-06-14T10:00:00.000Z",
            "cwd": "/tmp/fe",
        },
        {
            "type": "message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
                "timestamp": user_ts_ms,
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
                "timestamp": edit_ts_ms,
            },
        },
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_opencode_edit_db(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    user_text: str,
    edit_path: str,
    user_ms: int,
    edit_ms: int,
) -> Path:
    """Write a minimal OpenCode DB with a ``patch``-type tool part."""
    db_path = tmp_sessions_dir / "opencode_fe.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id           TEXT PRIMARY KEY,
            parent_id    TEXT,
            title        TEXT,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE message (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES session(id),
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data         TEXT
        );
        CREATE TABLE part (
            id           TEXT PRIMARY KEY,
            message_id   TEXT NOT NULL REFERENCES message(id),
            session_id   TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data         TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, NULL, ?, ?, ?)",
        (uuid, "fe session", user_ms, edit_ms + 1000),
    )
    conn.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        [
            ("u1", uuid, user_ms, user_ms,
             json.dumps({"role": "user"})),
            ("a1", uuid, edit_ms, edit_ms,
             json.dumps({"role": "assistant"})),
        ],
    )
    conn.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("u1-p0", "u1", uuid, user_ms, user_ms,
             json.dumps({"type": "text", "text": user_text})),
            ("a1-p0", "a1", uuid, edit_ms, edit_ms,
             json.dumps({"type": "patch", "hash": "h1",
                         "files": [{"path": edit_path, "added": 1, "removed": 1}]})),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def test_find_file_edits_registered() -> None:
    names = _run(_tool_names())
    assert "find_file_edits" in names


def test_find_file_edits_empty_path_returns_error() -> None:
    result = find_file_edits(path="")
    assert result.get("error") == "invalid_argument"


def test_find_file_edits_no_match_returns_empty(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No edit tool calls anywhere -> empty records, count=0, not truncated."""
    _write_claude_edit_session(
        tmp_sessions_dir, "no-match-1",
        user_text="hi", edit_path="/tmp/nowhere.txt",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = find_file_edits(path="/definitely/not/a/real/path/zzz")
    diagnostics = result.pop("diagnostics")
    assert result == {"records": [], "count": 0, "truncated": False}
    # A zero-match result must explain itself (F1.1).
    assert diagnostics["filters"]["path"] == "/definitely/not/a/real/path/zzz"
    assert diagnostics["hints"]


def test_find_file_edits_claude_match(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-agent default: Claude Edit call surfaces with intent and ts."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-1",
        user_text="Add the README header",
        edit_path="/tmp/ai-r/README.md",
        ts_user="2026-06-14T10:00:00Z",
        ts_edit="2026-06-14T10:00:05Z",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = find_file_edits(path="README.md", include_input=True)
    assert result["count"] >= 1
    assert result["truncated"] is False
    hit = next(r for r in result["records"] if r["session_uuid"] == "cfe-1")
    assert hit["agent"] == "claude"
    assert hit["tool"] == "Edit"
    assert hit["file"] == "/tmp/ai-r/README.md"
    assert hit["intent"] == "Add the README header"
    assert hit["assistant"] == "Editing now."
    assert hit["timestamp"] == "2026-06-14T10:00:05+00:00"
    assert hit["input"]["old_string"] == "old"
    assert hit["input"]["new_string"] == "new"


def test_find_file_edits_agent_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agent`` filter narrows results to a single agent."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-2",
        user_text="Edit README", edit_path="/tmp/agent-filter/README.md",
    )
    _write_pi_edit_session(
        tmp_sessions_dir, "pfe-2",
        user_text="Edit pi file", edit_path="/tmp/agent-filter/README.md",
    )
    base_claude = str(tmp_sessions_dir / ".claude" / "projects")
    base_pi = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base_claude)
    )
    monkeypatch.setattr(
        "ai_r.parsers.pi._resolve_base_dir", lambda bd=None: Path(base_pi)
    )
    claude_only = find_file_edits(path="agent-filter", agent="claude")
    assert {r["agent"] for r in claude_only["records"]} == {"claude"}
    pi_only = find_file_edits(path="agent-filter", agent="pi")
    assert {r["agent"] for r in pi_only["records"]} == {"pi"}


def test_find_file_edits_path_substring(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``path`` is a substring filter; only matching files surface."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-sub-a",
        user_text="A", edit_path="/tmp/x/proj/README.md",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-sub-b",
        user_text="B", edit_path="/tmp/x/other/CHANGELOG.md",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = find_file_edits(path="README")
    uuids = [r["session_uuid"] for r in result["records"]]
    assert "cfe-sub-a" in uuids
    assert "cfe-sub-b" not in uuids


def test_find_file_edits_since_until_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """since/until bound the edit timestamp inclusive."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-bound-a",
        user_text="A", edit_path="/tmp/boundary/file.txt",
        ts_user="2026-06-14T09:00:00Z", ts_edit="2026-06-14T09:00:05Z",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-bound-b",
        user_text="B", edit_path="/tmp/boundary/file.txt",
        ts_user="2026-06-14T11:00:00Z", ts_edit="2026-06-14T11:00:05Z",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-bound-c",
        user_text="C", edit_path="/tmp/boundary/file.txt",
        ts_user="2026-06-14T12:00:00Z", ts_edit="2026-06-14T12:00:05Z",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = find_file_edits(
        path="boundary",
        since="2026-06-14T10:00:00Z",
        until="2026-06-14T11:30:00Z",
    )
    uuids = {r["session_uuid"] for r in result["records"]}
    assert uuids == {"cfe-bound-b"}


def test_find_file_edits_limit_caps_results(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit`` caps the result list and sets ``truncated``."""
    for i in range(4):
        _write_claude_edit_session(
            tmp_sessions_dir, f"cfe-cap-{i}",
            user_text=f"u{i}", edit_path=f"/tmp/cap/file-{i}.txt",
            ts_user=f"2026-06-14T10:0{i}:00Z",
            ts_edit=f"2026-06-14T10:0{i}:30Z",
        )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = find_file_edits(path="cap/file-", limit=2)
    assert len(result["records"]) == 2
    assert result["count"] == 4
    assert result["truncated"] is True
    # Sorted by timestamp ASC: cap-0 first, then cap-1.
    ts_list = [r["timestamp"] for r in result["records"]]
    assert ts_list == sorted(ts_list)


def test_find_file_edits_invalid_iso_bound(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad ISO strings surface as ``invalid_argument``."""
    result = find_file_edits(path="x", since="not-a-date")
    assert result.get("error") == "invalid_argument"
    assert "since" in result["message"].lower()


def test_find_file_edits_invalid_agent(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = find_file_edits(path="x", agent="mystery")
    assert result.get("error") == "invalid_argument"


def test_find_file_edits_opencode_match(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenCode ``patch`` parts are recognised as file edits.

    The synthetic session has a unique ``edit_path`` so the test stays
    robust even if real snap DBs leak through the opencode parser's
    multi-DB discovery.
    """
    unique_marker = "/tmp/oc-find-file-edits-zzz/models.py"
    db_path = _write_opencode_edit_db(
        tmp_sessions_dir, "oc-fe-1",
        user_text="Patch the model",
        edit_path=unique_marker,
        user_ms=1_716_000_100_000,
        edit_ms=1_716_000_200_000,
    )
    monkeypatch.setenv("OPENCODE_DB", str(db_path))
    result = find_file_edits(
        path="find-file-edits-zzz", agent="opencode"
    )
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["agent"] == "opencode"
    assert hit["tool"] == "patch"
    assert hit["file"] == unique_marker
    assert hit["intent"] == "Patch the model"
    # ts must be the part's time, which is tz-aware.
    assert hit["timestamp"] is not None
    assert hit["timestamp"].endswith("+00:00")


def test_find_file_edits_intent_from_immediately_previous_user(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``intent`` is the immediately-previous user message text."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-intent",
        user_text="Refactor the auth module",
        edit_path="/tmp/intent/auth.py",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = find_file_edits(path="intent/auth.py")
    assert result["count"] == 1
    assert result["records"][0]["intent"] == "Refactor the auth module"


def test_find_file_edits_cross_agent_default(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no agent filter) returns hits from multiple agents."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-cross",
        user_text="Edit README in claude",
        edit_path="/tmp/cross/shared.md",
        ts_user="2026-06-14T10:00:00Z",
        ts_edit="2026-06-14T10:00:05Z",
    )
    _write_pi_edit_session(
        tmp_sessions_dir, "pfe-cross",
        user_text="Edit README in pi",
        edit_path="/tmp/cross/shared.md",
        user_ts_ms=1_718_360_002_000,
        edit_ts_ms=1_718_360_004_000,
    )
    base_claude = str(tmp_sessions_dir / ".claude" / "projects")
    base_pi = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base_claude)
    )
    monkeypatch.setattr(
        "ai_r.parsers.pi._resolve_base_dir", lambda bd=None: Path(base_pi)
    )
    result = find_file_edits(path="cross/shared")
    agents = {r["agent"] for r in result["records"]}
    assert {"claude", "pi"} <= agents


def test_find_file_edits_via_mcp_client(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool is reachable through the real MCP client surface."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-mcp",
        user_text="MCP path",
        edit_path="/tmp/mcp-via/path.py",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    texts = _run(_call("find_file_edits", {"path": "mcp-via"}))
    payload = json.loads(texts[0])
    assert payload["count"] >= 1
    assert any(r["session_uuid"] == "cfe-mcp" for r in payload["records"])


# ---------------------------------------------------------------------------
# find_file_edits — codex function_call names
# ---------------------------------------------------------------------------


def _write_codex_edit_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    user_text: str,
    tool_name: str,
    arguments: dict,
    ts_user: str = "2026-06-14T10:00:00Z",
    ts_call: str = "2026-06-14T10:00:05Z",
) -> None:
    """Write a Codex rollout with a single function_call edit tool."""
    rollout = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T10-00-00-{uuid}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-06-14T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": uuid, "cwd": "/tmp/fe"},
        },
        {
            "timestamp": ts_user,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            },
        },
        {
            "timestamp": ts_call,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": tool_name,
                "arguments": json.dumps(arguments),
            },
        },
    ]
    rollout.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_find_file_edits_codex_write_file(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex ``write_file`` function_call surfaces as an edit hit."""
    _write_codex_edit_session(
        tmp_sessions_dir, "cxf-1",
        user_text="Create the new module",
        tool_name="write_file",
        arguments={"file_path": "/tmp/codex-write/models.py", "content": "x = 1\n"},
    )
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [Path(base)]
    )
    result = find_file_edits(path="codex-write", agent="codex")
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["agent"] == "codex"
    assert hit["tool"] == "write_file"
    assert hit["file"] == "/tmp/codex-write/models.py"
    assert hit["intent"] == "Create the new module"


def test_find_file_edits_codex_apply_patch(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex ``apply_patch`` function_call surfaces as an edit hit."""
    _write_codex_edit_session(
        tmp_sessions_dir, "cxf-2",
        user_text="Apply the README patch",
        tool_name="apply_patch",
        arguments={"file_path": "/tmp/codex-patch/README.md", "patch": "..."},
    )
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [Path(base)]
    )
    result = find_file_edits(path="codex-patch", agent="codex")
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["agent"] == "codex"
    assert hit["tool"] == "apply_patch"
    assert hit["file"] == "/tmp/codex-patch/README.md"


def test_find_file_edits_codex_unknown_tool_skipped(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-edit codex function_call name produces no records."""
    _write_codex_edit_session(
        tmp_sessions_dir, "cxf-3",
        user_text="Run a command",
        tool_name="shell",
        arguments={"command": "ls"},
    )
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [Path(base)]
    )
    result = find_file_edits(path="cxf-3", agent="codex")
    assert result["count"] == 0


def test_find_file_edits_codex_exec_command_redirect(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex CLI ``exec_command`` writing via ``>`` surfaces as an edit hit."""
    _write_codex_edit_session(
        tmp_sessions_dir, "cxf-4",
        user_text="Write the module via shell",
        tool_name="exec_command",
        arguments={"cmd": "printf 'x = 1\\n' > /tmp/codex-sh/app.py"},
    )
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [Path(base)]
    )
    result = find_file_edits(path="codex-sh", agent="codex", include_input=True)
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["agent"] == "codex"
    assert hit["tool"] == "exec_command"
    assert hit["file"] == "/tmp/codex-sh/app.py"
    assert hit["input"]["edit"] == "write"
    assert "printf" in hit["input"]["cmd"]


def test_find_file_edits_codex_exec_command_append(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``>>`` is classified as an append edit."""
    _write_codex_edit_session(
        tmp_sessions_dir, "cxf-5",
        user_text="Append to the log",
        tool_name="exec_command",
        arguments={"cmd": "echo bumped >> /tmp/codex-sh2/log.txt"},
    )
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [Path(base)]
    )
    result = find_file_edits(path="codex-sh2", agent="codex", include_input=True)
    assert result["count"] == 1
    assert result["records"][0]["file"] == "/tmp/codex-sh2/log.txt"
    assert result["records"][0]["input"]["edit"] == "append"


def test_find_file_edits_codex_exec_command_multi_redirect(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``exec_command`` writing two files yields two records."""
    _write_codex_edit_session(
        tmp_sessions_dir, "cxf-6",
        user_text="Write two files",
        tool_name="exec_command",
        arguments={"cmd": "echo a > /tmp/codex-sh3/a.txt && echo b > /tmp/codex-sh3/b.txt"},
    )
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [Path(base)]
    )
    result = find_file_edits(path="codex-sh3", agent="codex")
    files = sorted(r["file"] for r in result["records"])
    assert result["count"] == 2
    assert files == ["/tmp/codex-sh3/a.txt", "/tmp/codex-sh3/b.txt"]


def test_find_file_edits_codex_exec_command_quoted_gt_ignored(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``>`` inside quotes (regex/grep pattern) is NOT a redirection."""
    _write_codex_edit_session(
        tmp_sessions_dir, "cxf-7",
        user_text="Scan the headings",
        tool_name="exec_command",
        arguments={"cmd": 'rg "<h[^>]*>" /tmp/codex-sh4/page.html'},
    )
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    monkeypatch.setattr(
        "ai_r.parsers.codex._resolve_base_dir", lambda bd=None: [Path(base)]
    )
    result = find_file_edits(path="codex-sh4", agent="codex")
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# find_file_edits — antigravity tool_use
# ---------------------------------------------------------------------------


def _write_antigravity_edit_brain(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    user_text: str,
    tool_name: str,
    arguments: dict,
    ts_user: str = "2026-06-14T10:00:00Z",
    ts_call: str = "2026-06-14T10:00:05Z",
) -> None:
    """Write a brain with a user prompt and a MODEL_TOOL_CALL edit."""
    brain = tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / uuid
    (brain / ".system_generated" / "logs").mkdir(parents=True)
    transcript = brain / ".system_generated" / "logs" / "transcript_full.jsonl"
    records = [
        {
            "timestamp": ts_user,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": user_text,
        },
        {
            "timestamp": ts_call,
            "source": "MODEL",
            "type": "MODEL_TOOL_CALL",
            "name": tool_name,
            "args": arguments,
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_find_file_edits_antigravity_model_tool_call(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antigravity ``MODEL_TOOL_CALL`` records surface as edit hits."""
    _write_antigravity_edit_brain(
        tmp_sessions_dir, "ag-fe-1",
        user_text="Patch the auth module",
        tool_name="Edit",
        arguments={
            "file_path": "/tmp/ag-edit/auth.py",
            "old_text": "old",
            "new_text": "new",
        },
    )
    result = find_file_edits(path="ag-edit", agent="antigravity")
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["agent"] == "antigravity"
    assert hit["tool"] == "Edit"
    assert hit["file"] == "/tmp/ag-edit/auth.py"
    assert hit["intent"] == "Patch the auth module"


def test_find_file_edits_antigravity_content_part_tool(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antigravity tool call embedded in a content parts list surfaces."""
    brain = (
        tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / "ag-fe-2"
    )
    (brain / ".system_generated" / "logs").mkdir(parents=True)
    transcript = brain / ".system_generated" / "logs" / "transcript_full.jsonl"
    records = [
        {
            "timestamp": "2026-06-14T10:00:00Z",
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "Refactor",
        },
        {
            "timestamp": "2026-06-14T10:00:05Z",
            "source": "MODEL",
            "type": "MODEL_OUTPUT",
            "content": [
                {"type": "text", "text": "Editing now."},
                {
                    "type": "MODEL_TOOL_CALL",
                    "name": "Edit",
                    "args": {
                        "file_path": "/tmp/ag-content/handler.py",
                        "old_text": "a",
                        "new_text": "b",
                    },
                },
            ],
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    result = find_file_edits(path="ag-content", agent="antigravity")
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["agent"] == "antigravity"
    assert hit["tool"] == "Edit"
    assert hit["file"] == "/tmp/ag-content/handler.py"


# ---------------------------------------------------------------------------
# find_file_edits — bound filter naive vs aware normalization
# ---------------------------------------------------------------------------


def test_find_file_edits_naive_ts_vs_aware_bound_no_crash(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parser emits a naive ``msg.timestamp`` while user passes a tz-aware
    ``since`` bound; the consumer must normalise both to UTC-aware before
    comparison — no ``TypeError``, the in-window edit must surface, the
    out-of-window edit must be skipped.
    """
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-naive-1",
        user_text="in window",
        edit_path="/tmp/naive/in.py",
        ts_user="2026-06-14T09:00:00Z",
        ts_edit="2026-06-14T11:00:00Z",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-naive-2",
        user_text="out of window",
        edit_path="/tmp/naive/out.py",
        ts_user="2026-06-14T09:00:00Z",
        ts_edit="2026-06-14T08:00:00Z",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    # Force the claude parser to produce a NAIVE datetime for both
    # sessions (the bug scenario: ISO without Z → no tzinfo).
    import ai_r.parsers.claude as claude_parser
    real_parse = claude_parser._parse_iso_timestamp

    def _naive_parse(raw):
        dt = real_parse(raw)
        if dt is not None:
            return dt.replace(tzinfo=None)
        return None

    monkeypatch.setattr(claude_parser, "_parse_iso_timestamp", _naive_parse)
    # User bound is tz-aware — this is where the TypeError used to fire.
    result = find_file_edits(
        path="naive",
        since="2026-06-14T10:00:00+00:00",
    )
    uuids = {r["session_uuid"] for r in result["records"]}
    assert "cfe-naive-1" in uuids
    assert "cfe-naive-2" not in uuids


def test_find_file_edits_aware_bound_with_aware_record(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity: tz-aware record + tz-aware bound still work end-to-end."""
    _write_claude_edit_session(
        tmp_sessions_dir, "cfe-aware-1",
        user_text="A", edit_path="/tmp/aware/file.txt",
        ts_edit="2026-06-14T10:00:05Z",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = find_file_edits(
        path="aware",
        since="2026-06-14T10:00:00+00:00",
        until="2026-06-14T11:00:00+00:00",
    )
    uuids = {r["session_uuid"] for r in result["records"]}
    assert uuids == {"cfe-aware-1"}


# ---------------------------------------------------------------------------
# find_file_edits — opencode per-tool_use timestamp
# ---------------------------------------------------------------------------


def test_find_file_edits_opencode_per_tool_timestamp(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenCode: tool_use entries carry their own part timestamp; the
    consumer prefers it over the message-level ts.

    Two ``patch`` parts in the same message at different times. The
    message-level ts is the earliest part (per parser spec) — which
    falls *before* the since-bound, so under the old per-message
    timestamp the whole message would be skipped. The per-tool
    timestamp surfaces the second patch only.
    """
    db_path = tmp_sessions_dir / "opencode_pertool.db"
    import sqlite3 as _sql
    conn = _sql.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id           TEXT PRIMARY KEY,
            parent_id    TEXT,
            title        TEXT,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE message (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES session(id),
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data         TEXT
        );
        CREATE TABLE part (
            id           TEXT PRIMARY KEY,
            message_id   TEXT NOT NULL REFERENCES message(id),
            session_id   TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data         TEXT NOT NULL
        );
        """
    )
    uuid = "oc-perttool-1"
    # 2025-06-14 10:00:00Z = 1749895200 sec = 1_749_895_200_000 ms
    since_ms = 1_749_895_200_000
    p1_ms = since_ms - 600_000      # 09:50:00Z (before bound)
    p2_ms = since_ms + 600_000      # 10:10:00Z (after bound)
    user_ms = p1_ms - 60_000
    conn.execute(
        "INSERT INTO session VALUES (?, NULL, ?, ?, ?)",
        (uuid, "per-tool ts", user_ms, p2_ms + 1000),
    )
    conn.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        [
            ("u1", uuid, user_ms, user_ms, json.dumps({"role": "user"})),
            ("a1", uuid, p2_ms, p2_ms, json.dumps({"role": "assistant"})),
        ],
    )
    conn.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("u1-p0", "u1", uuid, user_ms, user_ms,
             json.dumps({"type": "text", "text": "patch two files"})),
            ("a1-p0", "a1", uuid, p1_ms, p1_ms,
             json.dumps({"type": "patch", "hash": "h1",
                         "files": [{"path": "/tmp/pertool/first.py",
                                    "added": 1, "removed": 0}]})),
            ("a1-p1", "a1", uuid, p2_ms, p2_ms,
             json.dumps({"type": "patch", "hash": "h2",
                         "files": [{"path": "/tmp/pertool/second.py",
                                    "added": 1, "removed": 0}]})),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db_path))
    result = find_file_edits(
        path="pertool",
        since="2025-06-14T10:00:00+00:00",  # between p1_ms and p2_ms
        agent="opencode",
    )
    assert result["count"] == 1
    hit = result["records"][0]
    assert hit["file"] == "/tmp/pertool/second.py"
    # ts must be the second part's tz-aware time.
    assert hit["timestamp"] is not None
    assert hit["timestamp"].endswith("+00:00")


# ---------------------------------------------------------------------------
# query (Phase-1 event verb): registration + facet/relative_to behaviour
# ---------------------------------------------------------------------------


def test_query_tool_registered() -> None:
    names = _run(_tool_names())
    assert "query" in names
    assert "query" in set(mcp._tool_manager._tools.keys())


def test_query_type_facet_direct(fake_claude_session_with_tools: Path) -> None:
    # The fixture: one user_turn, one assistant_turn, one tool_call(bash).
    res = query(type="tool_call", agent="claude")
    assert res["count"] == 1
    assert res["events"][0]["type"] == "tool_call(bash)"


def test_query_user_turns_direct(fake_claude_session_with_tools: Path) -> None:
    res = query(type="user_turn", agent="claude")
    assert res["count"] == 1
    assert res["events"][0]["text"] == "Run the tests"


def test_query_relative_prev_direct(
    fake_claude_session_with_tools: Path,
) -> None:
    # Anchor on the bash tool_call; prev/1 user turn == "Run the tests".
    tool_ev = query(type="tool_call", agent="claude")["events"][0]
    res = query(relative_to=tool_ev["id"], direction="prev", n="1")
    assert [e["text"] for e in res["events"]] == ["Run the tests"]


def test_query_invalid_direction_returns_error_dict() -> None:
    res = query(relative_to="x:0", direction="sideways")
    assert res["error"] == "invalid_argument"


def test_query_over_mcp_client(fake_claude_session_with_tools: Path) -> None:
    out = _run(_call("query", {"type": "tool_call", "agent": "claude"}))
    payload = json.loads(out[0])
    assert payload["count"] == 1
    assert payload["events"][0]["type"] == "tool_call(bash)"


# ---------------------------------------------------------------------------
# query reference-by-default (QRY-1): text preview at the MCP boundary
# ---------------------------------------------------------------------------


def _patch_claude_base(tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )


def test_query_long_text_cut_to_preview_and_get_body_full(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long body surfaces as a ~160-char preview + flag; get_body returns it whole."""
    long_text = "slovo " * 900  # ~5.4 KB, no secrets
    _write_claude_body_session(
        tmp_sessions_dir, uuid="preview-long-1", user_text=long_text
    )
    _patch_claude_base(tmp_sessions_dir, monkeypatch)

    res = query(type="user_turn", agent="claude", session="preview-long-1")
    assert res["count"] == 1
    ev = res["events"][0]
    assert ev["text_truncated"] is True
    assert ev["text"].endswith("…")
    assert ev["text"] == long_text[:160] + "…"
    assert len(ev["text"]) == 161
    # id/refs untouched → get_body resolves the FULL body.
    body = get_body(ev["id"])
    assert body.get("error") is None
    full = body.get("text") or body.get("body") or ""
    assert full.rstrip() == long_text.rstrip()
    assert len(full) > 5000


def test_query_short_text_not_flagged(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short body is emitted verbatim: no cut, no ``text_truncated`` key."""
    _write_claude_body_session(
        tmp_sessions_dir, uuid="preview-short-1", user_text="short and sweet"
    )
    _patch_claude_base(tmp_sessions_dir, monkeypatch)

    res = query(type="user_turn", agent="claude", session="preview-short-1")
    ev = res["events"][0]
    assert ev["text"] == "short and sweet"
    assert "text_truncated" not in ev


def test_query_preview_cut_runs_after_redaction(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret at the HEAD of a long body is masked in the preview.

    Redaction happens in the core (emission-time), the preview cut at the
    MCP boundary — so the preview must contain the placeholder, never a
    prefix of the raw secret.
    """
    secret = "ghp_" + "a1b2c3d4e5" * 4  # GITHUB_TOKEN pattern (40 tail chars)
    long_text = "token " + secret + " tail " + "x" * 500
    _write_claude_body_session(
        tmp_sessions_dir, uuid="preview-redact-1", user_text=long_text
    )
    _patch_claude_base(tmp_sessions_dir, monkeypatch)

    res = query(type="user_turn", agent="claude", session="preview-redact-1")
    ev = res["events"][0]
    assert ev["text_truncated"] is True
    assert "[REDACTED_GITHUB_TOKEN]" in ev["text"]
    assert secret not in ev["text"]
    assert res["redactions"].get("GITHUB_TOKEN", 0) >= 1


def test_query_preview_over_mcp_client(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview contract holds over the real MCP transport too."""
    long_text = "payload " * 400
    _write_claude_body_session(
        tmp_sessions_dir, uuid="preview-mcp-1", user_text=long_text
    )
    _patch_claude_base(tmp_sessions_dir, monkeypatch)

    out = _run(
        _call(
            "query",
            {"type": "user_turn", "agent": "claude", "session": "preview-mcp-1"},
        )
    )
    payload = json.loads(out[0])
    ev = payload["events"][0]
    assert ev["text_truncated"] is True
    assert len(ev["text"]) == 161 and ev["text"].endswith("…")


# ---------------------------------------------------------------------------
# plan + get_body (Phase-2 verbs): registration + behaviour
# ---------------------------------------------------------------------------


def test_plan_and_get_body_registered() -> None:
    names = set(_run(_tool_names()))
    assert {"plan", "get_body"}.issubset(names)
    assert {"plan", "get_body"}.issubset(set(mcp._tool_manager._tools.keys()))


def test_plan_tool_direct(fake_claude_plan_redraft: str) -> None:
    res = plan(session=fake_claude_plan_redraft, agent="claude")
    # One slug, drifting titles → 1 final + 3 draft (grouped by slug).
    assert res["count"] == 4
    kinds = [p["kind"] for p in res["plans"]]
    assert kinds.count("final") == 1 and kinds.count("draft") == 3
    assert kinds.count("completed_major") == 0


def test_plan_tool_kind_filter(fake_claude_plan_redraft: str) -> None:
    res = plan(session=fake_claude_plan_redraft, agent="claude", kind="final")
    assert res["count"] == 1
    assert res["plans"][0]["kind"] == "final"


def test_plan_tool_invalid_group_returns_error(
    fake_claude_plan_redraft: str,
) -> None:
    res = plan(session=fake_claude_plan_redraft, group="slug")
    assert res["error"] == "invalid_argument"


def test_get_body_tool_direct(fake_claude_plan_redraft: str) -> None:
    final = plan(session=fake_claude_plan_redraft, kind="final")["plans"][0]
    body = get_body(final["id"])
    assert body["type"] == "plan_event"
    assert "Final plan." in body["body"]


def test_get_body_shallow_over_mcp_client(
    fake_claude_plan_redraft: str,
) -> None:
    drafts = plan(session=fake_claude_plan_redraft, kind="draft")["plans"]
    out = _run(
        _call("get_body", {"id": drafts[0]["id"], "shallow": True})
    )
    payload = json.loads(out[0])
    # Shallow → returns the final plan, drafts elided.
    assert "Final plan." in payload["body"]
    assert payload["dropped_drafts"]


def test_get_body_empty_id_returns_error() -> None:
    assert get_body("")["error"] == "invalid_argument"


def test_plan_over_mcp_client(fake_codex_plan_session: str) -> None:
    out = _run(_call("plan", {"session": fake_codex_plan_session, "agent": "codex"}))
    payload = json.loads(out[0])
    assert payload["count"] == 3
    assert payload["plans"][-1]["kind"] == "final"
