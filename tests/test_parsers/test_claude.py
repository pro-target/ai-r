"""Tests for the Claude session parser.

Covers:

* Discovery against the real ``~/.claude/projects`` tree (read-only).
* Title, role and message-count extraction from synthetic fixtures.
* UUID validation: empty, slashes, path-traversal attempts.
* The ``search`` and ``session_exists`` helpers.
"""
from __future__ import annotations

import json
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from ai_r.parsers import AgentName, claude
from ai_r.parsers.claude import (
    _extract_text_from_user_message,
    _normalise_title,
    _parse_iso_timestamp,
    _scan_file,
)


# ---------------------------------------------------------------------------
# Real-data smoke
# ---------------------------------------------------------------------------


def test_list_sessions_real(real_claude_dir: Path) -> None:
    # ``real_claude_dir`` auto-skips when the host has no Claude data (conftest).
    # The autouse ``_isolate_ai_r_home`` fixture redirects AI_R_HOME at a fake
    # tree, so we pass ``base_dir`` explicitly to read the real one.
    sessions = claude.list_sessions(base_dir=str(real_claude_dir))
    assert sessions, "expected at least one Claude session on this host"
    for s in sessions[:5]:
        assert s.agent is AgentName.CLAUDE
        assert s.title
        assert s.path.endswith(".jsonl")
        # Recent sessions must be sorted by date desc.
    dates = [s.date for s in sessions]
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# Synthetic fixture (writes into AI_R_HOME / tmp)
# ---------------------------------------------------------------------------


def test_parse_message_role_user(fake_claude_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    sessions = claude.list_sessions(base_dir=base)
    assert len(sessions) == 1
    session = sessions[0]
    assert session.uuid == "test-claude-1"
    assert session.agent is AgentName.CLAUDE
    assert session.title == "Hello, world"
    assert session.message_count == 2  # one user, one assistant
    assert session.extra.get("project_slug") == "proj-a"


def test_parse_message_role_assistant(fake_claude_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    session = claude.read_session("test-claude-1", base_dir=base)
    assert session.message_count == 2  # both records counted
    assert session.title  # first user text becomes title


def test_extract_title(fake_claude_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    # Strip the user record, leaving only an assistant line.
    jsonl = fake_claude_session
    with jsonl.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "ai-title",
                    "aiTitle": "Auto-generated title",
                    "timestamp": "2026-06-14T09:00:00Z",
                    "sessionId": "test-claude-1",
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "noise"}],
                    },
                    "timestamp": "2026-06-14T09:00:05Z",
                    "sessionId": "test-claude-1",
                }
            )
            + "\n"
        )
    session = claude.read_session("test-claude-1", base_dir=base)
    assert session.title == "Auto-generated title"
    # Only the assistant record counts.
    assert session.message_count == 1


def test_count_messages(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".claude" / "projects" / "proj-a"
    base.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i in range(5):
        lines.append(
            json.dumps(
                {
                    "type": "user" if i % 2 == 0 else "assistant",
                    "message": {
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": f"msg-{i}",
                    },
                    "timestamp": f"2026-06-14T10:0{i}:00Z",
                    "sessionId": "x",
                }
            )
        )
    (base / "counted.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sessions = claude.list_sessions(base_dir=str(tmp_sessions_dir / ".claude" / "projects"))
    assert len(sessions) == 1
    assert sessions[0].message_count == 5


def test_invalid_uuid_raises(tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    for bad in ("", " ", "../escape", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            claude.read_session(bad, base_dir=base)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def test_parse_iso_timestamp_tolerates_z() -> None:
    ts = _parse_iso_timestamp("2026-06-14T10:00:00.123Z")
    assert ts is not None
    assert ts.year == 2026 and ts.month == 6 and ts.day == 14


def test_parse_iso_timestamp_always_tz_aware() -> None:
    # Regression: the ``raw[:23]`` truncation used to cut the tz suffix
    # BEFORE the ``Z`` replace, so every transcript date came out naive
    # and crashed the sort against aware Desktop-overlay dates.
    z_form = _parse_iso_timestamp("2026-06-14T10:00:00.123Z")
    assert z_form is not None and z_form.utcoffset() == timedelta(0)
    # Naive input is pinned to UTC.
    naive = _parse_iso_timestamp("2026-06-14T10:00:00")
    assert naive is not None and naive.tzinfo is timezone.utc
    # Explicit offsets are honoured (same instant as 07:00 UTC).
    offset = _parse_iso_timestamp("2026-06-14T10:00:00+03:00")
    assert offset is not None
    assert offset.utcoffset() == timedelta(hours=3)
    assert offset == z_form.replace(
        hour=7, minute=0, second=0, microsecond=0
    )


def test_parse_iso_timestamp_returns_none_on_garbage() -> None:
    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("not-a-date") is None
    assert _parse_iso_timestamp(None) is None  # type: ignore[arg-type]


def test_normalise_title_collapses_and_truncates() -> None:
    assert _normalise_title("hello\nworld") == "hello world"
    assert _normalise_title("") == "Untitled"
    assert _normalise_title("x" * 200) == "x" * 100


def test_extract_text_from_user_message_string() -> None:
    assert _extract_text_from_user_message({"content": "hi"}) == "hi"


def test_extract_text_from_user_message_list() -> None:
    """Returns the *first* non-system, non-empty text part."""
    msg = {
        "content": [
            {"type": "text", "text": "first wins"},
            {"type": "text", "text": "second"},
        ]
    }
    assert _extract_text_from_user_message(msg) == "first wins"


def test_extract_text_from_user_message_skips_system() -> None:
    """Lines that start with ``<`` (e.g. ``<system-reminder>``) are skipped."""
    msg = {
        "content": [
            {"type": "text", "text": "<system-reminder>nope</system-reminder>"},
            {"type": "text", "text": "second"},
        ]
    }
    assert _extract_text_from_user_message(msg) == "second"


def test_extract_text_from_user_message_no_match() -> None:
    assert _extract_text_from_user_message({"content": [{"type": "text", "text": ""}]}) == ""
    assert _extract_text_from_user_message({"content": []}) == ""


def test_scan_file_handles_malformed_lines(tmp_path: Path) -> None:
    bad = tmp_path / "weird.jsonl"
    bad.write_text(
        "this is not json\n"
        '{"type":"user","message":{"role":"user","content":"hello"},"timestamp":"2026-06-14T10:00:00Z"}\n',
        encoding="utf-8",
    )
    session = _scan_file(bad)
    assert session is not None
    assert session.title == "hello"
    assert session.message_count == 1


def test_session_exists(tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    _ = tmp_sessions_dir  # noqa: F841  (uses the dir layout)
    # create a session
    (tmp_sessions_dir / ".claude" / "projects" / "proj-a" / "present.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "x"},
                "timestamp": "2026-06-14T10:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert claude.session_exists("present", base_dir=base) is True
    assert claude.session_exists("absent", base_dir=base) is False
    assert claude.session_exists("../escape", base_dir=base) is False


def test_search_returns_matches_case_insensitive(fake_claude_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    out = claude.search("HELLO", base_dir=base)
    assert len(out) == 1
    assert out[0].uuid == "test-claude-1"
    # Empty query -> empty
    assert claude.search("", base_dir=base) == []


def test_list_sessions_missing_dir(tmp_path: Path) -> None:
    assert claude.list_sessions(base_dir=str(tmp_path / "nope")) == []


def test_read_session_missing_raises(tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    with pytest.raises(FileNotFoundError):
        claude.read_session("definitely-not-here", base_dir=base)


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


def test_read_messages_basic(fake_claude_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    msgs = claude.read_messages("test-claude-1", base_dir=base)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].text == "Hello, world"
    assert msgs[0].tool_use == ()
    assert msgs[1].role == "assistant"
    assert msgs[1].text == "Hi there!"


def test_read_messages_preserves_tool_use_and_result(
    fake_claude_session_with_tools: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    msgs = claude.read_messages("claude-tools-1", base_dir=base)
    assert len(msgs) == 3

    assistant = msgs[1]
    assert assistant.role == "assistant"
    assert assistant.text == "I'll run them now."
    assert len(assistant.tool_use) == 1
    tu = assistant.tool_use[0]
    assert tu["name"] == "Bash"
    # input dict serialized to JSON string
    assert '"pytest"' in tu["input"]

    user_result = msgs[2]
    assert user_result.role == "user"
    assert len(user_result.tool_result) == 1
    assert user_result.tool_result[0]["content"] == "5 passed"
    # No ``is_error`` in the source block → defaults to False.
    assert user_result.tool_result[0]["is_error"] is False


def test_read_messages_missing_raises(tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    with pytest.raises(FileNotFoundError):
        claude.read_messages("nope", base_dir=base)


def test_read_messages_invalid_uuid(tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    with pytest.raises(ValueError):
        claude.read_messages("../escape", base_dir=base)


def test_message_is_frozen(fake_claude_session: Path, tmp_sessions_dir: Path) -> None:
    """Message is a frozen dataclass — attribute mutation is rejected."""
    from ai_r.parsers.models import Message

    base = str(tmp_sessions_dir / ".claude" / "projects")
    msgs = claude.read_messages("test-claude-1", base_dir=base)
    assert isinstance(msgs[0], Message)
    with pytest.raises(Exception):
        msgs[0].role = "tool"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Incremental (byte-offset) reads
# ---------------------------------------------------------------------------


def test_read_session_incremental(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".claude" / "projects" / "proj-a"
    base.mkdir(parents=True, exist_ok=True)
    jsonl = base / "incremental.jsonl"
    records: list[dict] = []
    for i in range(10):
        records.append(
            {
                "type": "user" if i % 2 == 0 else "assistant",
                "message": {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"msg-{i}",
                },
                "timestamp": f"2026-06-14T10:00:{i:02d}Z",
                "sessionId": "incremental",
            }
        )
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )

    base_dir = str(tmp_sessions_dir / ".claude" / "projects")
    initial_size = jsonl.stat().st_size

    msgs1, offset1 = claude.read_session_incremental("incremental", base_dir=base_dir)
    assert len(msgs1) == 10
    assert offset1 == initial_size
    assert msgs1[0].text == "msg-0"
    assert msgs1[9].text == "msg-9"

    with jsonl.open("a", encoding="utf-8") as fh:
        for i in range(10, 13):
            fh.write(
                json.dumps(
                    {
                        "type": "user" if i % 2 == 0 else "assistant",
                        "message": {
                            "role": "user" if i % 2 == 0 else "assistant",
                            "content": f"msg-{i}",
                        },
                        "timestamp": f"2026-06-14T10:00:{i:02d}Z",
                        "sessionId": "incremental",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    new_size = jsonl.stat().st_size
    assert new_size > initial_size

    msgs2, offset2 = claude.read_session_incremental(
        "incremental", from_offset=offset1, base_dir=base_dir
    )
    assert len(msgs2) == 3
    assert offset2 == new_size
    assert msgs2[0].text == "msg-10"
    assert msgs2[1].text == "msg-11"
    assert msgs2[2].text == "msg-12"


def test_incremental_empty_initial(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".claude" / "projects" / "proj-a"
    base.mkdir(parents=True, exist_ok=True)
    jsonl = base / "empty-initial.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "x"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    base_dir = str(tmp_sessions_dir / ".claude" / "projects")
    size = claude.get_session_size("empty-initial", base_dir=base_dir)
    assert size == jsonl.stat().st_size
    msgs, offset = claude.read_session_incremental(
        "empty-initial", from_offset=size, base_dir=base_dir
    )
    assert msgs == []
    assert offset == size


def test_get_session_size_matches_stat(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".claude" / "projects" / "proj-a"
    base.mkdir(parents=True, exist_ok=True)
    jsonl = base / "sized.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "y"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    base_dir = str(tmp_sessions_dir / ".claude" / "projects")
    assert claude.get_session_size("sized", base_dir=base_dir) == jsonl.stat().st_size


# ---------------------------------------------------------------------------
# extract_title priority chain
# ---------------------------------------------------------------------------


def test_extract_title_priority(tmp_path: Path) -> None:
    """``custom-title`` wins over ``ai-title`` and user-message text."""
    jsonl = tmp_path / "titles.jsonl"
    jsonl.write_text(
        '{"type":"custom-title","customTitle":"My custom title"}\n'
        '{"type":"ai-title","aiTitle":"AI generated title"}\n'
        '{"type":"user","message":{"role":"user","content":"user text"}}\n',
        encoding="utf-8",
    )
    assert claude.extract_title([], jsonl) == "My custom title"
    messages = claude._extract_messages_from_jsonl(jsonl)
    assert claude.extract_title(messages, jsonl) == "My custom title"


# ---------------------------------------------------------------------------
# claude_derive: decision + task summarisation
# ---------------------------------------------------------------------------


def test_summarize_task_skip_stopword_tail() -> None:
    """A trailing ``thanks`` falls through to the prior user message."""
    from ai_r.parsers.claude_derive import summarize_task
    from ai_r.parsers.models import Message

    messages = [
        Message(
            role="user",
            text="Refactor the parser to handle custom-title events please",
            tool_use=(),
            tool_result=(),
        ),
        Message(role="user", text="thanks", tool_use=(), tool_result=()),
    ]
    out = summarize_task(messages)
    assert "Refactor" in out
    assert "parser" in out
    assert "thanks" not in out.lower()


def test_extract_decisions_tech_filter() -> None:
    """Decision sentences with a tech token are kept; noise is dropped."""
    from ai_r.parsers.claude_derive import extract_decisions
    from ai_r.parsers.models import Message

    messages = [
        Message(
            role="assistant",
            text=(
                "I decided to use port 8080 for the api server. "
                "The fridge should hum louder. "
                "We chose docker over bare metal."
            ),
            tool_use=(),
            tool_result=(),
        ),
    ]
    decisions = extract_decisions(messages)
    assert any("port 8080" in d for d in decisions)
    assert any("docker" in d.lower() for d in decisions)
    assert not any("fridge" in d.lower() for d in decisions)


# ---------------------------------------------------------------------------
# Subagent tree (kind + parent_uuid) — directory form + inline sidechain
# ---------------------------------------------------------------------------


def test_list_sessions_discovers_subagent_dir_form(
    fake_claude_subagent: Path, tmp_sessions_dir: Path
) -> None:
    """A ``subagents/agent-*.jsonl`` file is discovered and tagged subagent."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    sessions = claude.list_sessions(base_dir=base)
    assert len(sessions) == 1
    sub = sessions[0]
    assert sub.uuid == "agent-sub-1"
    assert sub.kind == "subagent"
    assert sub.parent_uuid == "parent-claude-1"
    # project slug skips the per-session + subagents folders.
    assert sub.extra.get("project_slug") == "proj-a"


def test_top_level_session_defaults_to_agent_kind(
    fake_claude_session: Path, tmp_sessions_dir: Path
) -> None:
    """A normal session is ``kind='agent'`` with no parent."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    session = claude.read_session("test-claude-1", base_dir=base)
    assert session.kind == "agent"
    assert session.parent_uuid is None


def test_inline_sidechain_marks_subagent(tmp_sessions_dir: Path) -> None:
    """An inline ``isSidechain: True`` record classifies the session.

    ``parent_uuid`` is the spawner from ``sessionId`` — NOT the
    message-level ``parentUuid`` (a message uuid / chain root).
    """
    base = tmp_sessions_dir / ".claude" / "projects" / "proj-a"
    jsonl = base / "inline-sidechain.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "inline task"},
            "timestamp": "2026-06-14T12:00:00Z",
            "sessionId": "spawner-sid-9",
            "parentUuid": None,
            "isSidechain": True,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
            },
            "timestamp": "2026-06-14T12:00:01Z",
            "sessionId": "spawner-sid-9",
            "parentUuid": "msg-root-uuid",
            "isSidechain": True,
        },
    ]
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    session = _scan_file(jsonl)
    assert session is not None
    assert session.kind == "subagent"
    # Spawner = sessionId, never the message-level parentUuid.
    assert session.parent_uuid == "spawner-sid-9"


def test_subagent_dir_form_parent_is_spawner_not_message_uuid(
    tmp_sessions_dir: Path,
) -> None:
    """Directory form: ``parent_uuid`` is the folder (spawner), and the
    message-level ``parentUuid`` (a message uuid) is ignored."""
    parent_sid = "parent-sid-dir"
    agent_id = "agent-dir-1"
    jsonl = (
        tmp_sessions_dir
        / ".claude"
        / "projects"
        / "proj-a"
        / parent_sid
        / "subagents"
        / f"{agent_id}.jsonl"
    )
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "subtask"},
            "timestamp": "2026-06-14T11:00:00Z",
            "sessionId": parent_sid,
            "parentUuid": None,
            "isSidechain": True,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            },
            "timestamp": "2026-06-14T11:00:05Z",
            "sessionId": parent_sid,
            "parentUuid": "some-message-uuid",
            "isSidechain": True,
        },
    ]
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    session = _scan_file(jsonl)
    assert session is not None
    assert session.kind == "subagent"
    assert session.parent_uuid == parent_sid
    assert session.parent_uuid != "some-message-uuid"


def test_subagent_flat_form_parent_from_session_id(
    tmp_sessions_dir: Path,
) -> None:
    """Flat form ``projects/<slug>/subagents/agent-*.jsonl``: no per-session
    wrapper folder → path yields None → spawner comes from ``sessionId``.

    Regression test for the A2 defect (previously message-uuid / None).
    """
    parent_sid = "parent-sid-flat"
    agent_id = "agent-flat-1"
    jsonl = (
        tmp_sessions_dir
        / ".claude"
        / "projects"
        / "proj-a"
        / "subagents"
        / f"{agent_id}.jsonl"
    )
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "flat subtask"},
            "timestamp": "2026-06-14T13:00:00Z",
            "sessionId": parent_sid,
            "parentUuid": None,
            "isSidechain": True,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            },
            "timestamp": "2026-06-14T13:00:01Z",
            "sessionId": parent_sid,
            "parentUuid": "chain-root-msg",
            "isSidechain": True,
        },
    ]
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    session = _scan_file(jsonl)
    assert session is not None
    assert session.kind == "subagent"
    assert session.parent_uuid == parent_sid
    assert session.parent_uuid != "chain-root-msg"


def test_subagent_self_parent_guard(tmp_sessions_dir: Path) -> None:
    """When ``sessionId`` equals the file's own uuid (no wrapper folder),
    the session must NOT become its own parent → ``parent_uuid is None``."""
    agent_id = "agent-self-1"
    jsonl = (
        tmp_sessions_dir
        / ".claude"
        / "projects"
        / "proj-a"
        / "subagents"
        / f"{agent_id}.jsonl"
    )
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "self task"},
            "timestamp": "2026-06-14T14:00:00Z",
            "sessionId": agent_id,
            "parentUuid": None,
            "isSidechain": True,
        },
    ]
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    session = _scan_file(jsonl)
    assert session is not None
    assert session.kind == "subagent"
    assert session.parent_uuid is None


def test_isSidechain_false_stays_agent(tmp_sessions_dir: Path) -> None:
    """``isSidechain: False`` present on every record must NOT mark subagent.

    Guards the value-not-presence rule: real Claude data carries the key
    set to ``False`` on normal records.
    """
    base = tmp_sessions_dir / ".claude" / "projects" / "proj-a"
    jsonl = base / "normal-with-key.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "normal task"},
            "timestamp": "2026-06-14T12:00:00Z",
            "parentUuid": "some-parent",
            "isSidechain": False,
        },
    ]
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    session = _scan_file(jsonl)
    assert session is not None
    assert session.kind == "agent"
    assert session.parent_uuid is None


def test_read_subagent_session_by_uuid(
    fake_claude_subagent: Path, tmp_sessions_dir: Path
) -> None:
    """A subagent session can be resolved + read by its agent-* uuid."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    session = claude.read_session("agent-sub-1", base_dir=base)
    assert session.kind == "subagent"
    msgs = claude.read_messages("agent-sub-1", base_dir=base)
    assert any(m.role == "user" for m in msgs)


# ---------------------------------------------------------------------------
# Defect #7-A: >1-level spawn chains reconnect via ``.meta.json`` toolUseId
# ---------------------------------------------------------------------------


def _write_spawn_child(
    subagents_dir: Path,
    agent_id: str,
    *,
    session_id: str,
    tool_use_id: str,
    spawn_depth: int,
    emits: tuple[str, ...] = (),
) -> None:
    """Write a subagent transcript + its sidecar ``.meta.json``.

    ``emits`` are ``Task``/``Agent`` tool_use ids this child itself spawns
    (i.e. it is the parent of those grandchildren) — mirrors the real
    on-disk shape where a spawn call's ``id`` == the child's meta
    ``toolUseId``.
    """
    subagents_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = [
        {
            "type": "user",
            "message": {"role": "user", "content": f"task for {agent_id}"},
            "timestamp": "2026-06-14T15:00:00Z",
            "sessionId": session_id,
            "isSidechain": True,
        }
    ]
    content: list[dict] = [{"type": "text", "text": "working"}]
    for i, tuid in enumerate(emits):
        content.append(
            {
                "type": "tool_use",
                "name": "Task",
                "id": tuid,
                "input": {"description": f"spawn {i}"},
            }
        )
    records.append(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": content},
            "timestamp": "2026-06-14T15:00:05Z",
            "sessionId": session_id,
            "isSidechain": True,
        }
    )
    with (subagents_dir / f"{agent_id}.jsonl").open(
        "w", encoding="utf-8"
    ) as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    (subagents_dir / f"{agent_id}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "toolUseId": tool_use_id,
                "spawnDepth": spawn_depth,
            }
        ),
        encoding="utf-8",
    )


def _build_depth2_spawn_tree(tmp_sessions_dir: Path) -> tuple[str, str, str, str]:
    """Top-level session spawns child-A (depth 1); child-A spawns
    grandchild-B (depth 2).  All three transcripts share one flat
    ``subagents/`` folder (the real Claude layout).

    Returns ``(base, top_uuid, child_uuid, grandchild_uuid)``.
    """
    projects = tmp_sessions_dir / ".claude" / "projects"
    slug = projects / "-home-user-proj"
    top_uuid = "top-session-1"
    child_uuid = "agent-child-A"
    grandchild_uuid = "agent-grandchild-B"
    tuid_child = "toolu_spawn_child_A"
    tuid_grand = "toolu_spawn_grandchild_B"

    # Top-level transcript: spawns child-A (emits tuid_child).
    top_dir = slug
    top_dir.mkdir(parents=True, exist_ok=True)
    with (top_dir / f"{top_uuid}.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "start"},
                    "timestamp": "2026-06-14T15:00:00Z",
                    "sessionId": top_uuid,
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "spawning"},
                            {
                                "type": "tool_use",
                                "name": "Task",
                                "id": tuid_child,
                                "input": {"description": "child A"},
                            },
                        ],
                    },
                    "timestamp": "2026-06-14T15:00:01Z",
                    "sessionId": top_uuid,
                }
            )
            + "\n"
        )

    subagents = slug / top_uuid / "subagents"
    # child-A: depth 1, spawned by top (toolUseId == tuid_child); it in turn
    # spawns grandchild-B (emits tuid_grand).
    _write_spawn_child(
        subagents,
        child_uuid,
        session_id=top_uuid,
        tool_use_id=tuid_child,
        spawn_depth=1,
        emits=(tuid_grand,),
    )
    # grandchild-B: depth 2, spawned by child-A (toolUseId == tuid_grand).
    # Its sessionId is the TOP uuid (real Claude behaviour) → without the
    # meta join it would collapse to the top-level session.
    _write_spawn_child(
        subagents,
        grandchild_uuid,
        session_id=top_uuid,
        tool_use_id=tuid_grand,
        spawn_depth=2,
    )
    return str(projects), top_uuid, child_uuid, grandchild_uuid


def test_deep_spawn_reparents_to_true_parent_in_list(
    tmp_sessions_dir: Path,
) -> None:
    """A depth-2 subagent must point at its real spawner (the depth-1
    child), NOT collapse to the top-level session (defect #7-A)."""
    base, top, child, grandchild = _build_depth2_spawn_tree(tmp_sessions_dir)
    by_uuid = {s.uuid: s for s in claude.list_sessions(base_dir=base)}

    assert by_uuid[child].kind == "subagent"
    assert by_uuid[child].parent_uuid == top  # depth 1 → top-level

    gc = by_uuid[grandchild]
    assert gc.kind == "subagent"
    # THE fix: depth-2 child reparents to the depth-1 child, not the root.
    assert gc.parent_uuid == child
    assert gc.parent_uuid != top
    # Internal bookkeeping must never leak.
    assert "_emitted_spawn_ids" not in gc.extra
    assert "_emitted_spawn_ids" not in by_uuid[child].extra


def test_deep_spawn_reparents_on_single_read(
    tmp_sessions_dir: Path,
) -> None:
    """``read_session`` (no session list to join) must ALSO reparent a
    depth>1 subagent via its sibling transcripts (defect #7-A)."""
    base, top, child, grandchild = _build_depth2_spawn_tree(tmp_sessions_dir)
    gc = claude.read_session(grandchild, base_dir=base)
    assert gc.parent_uuid == child
    assert gc.parent_uuid != top
    assert "_emitted_spawn_ids" not in gc.extra
    # A depth-1 child on single read keeps its (correct) top-level parent.
    ch = claude.read_session(child, base_dir=base)
    assert ch.parent_uuid == top


# ---------------------------------------------------------------------------
# Defect #7-B: flat/nested detection is structural, not literal ``projects``
# ---------------------------------------------------------------------------


def test_flat_subagent_under_custom_base_dir(tmp_path: Path) -> None:
    """Flat form under a base_dir whose leaf is NOT ``projects``: the
    spawner must come from ``sessionId``, not the project slug folder name
    (defect #7-B — the literal-``projects`` heuristic misfired)."""
    base = tmp_path / "mystore"  # deliberately not named "projects"
    slug_dir = base / "-slug"
    subagents = slug_dir / "subagents"
    subagents.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "flat task"},
            "timestamp": "2026-06-14T16:00:00Z",
            "sessionId": "spawner-flat-9",
            "isSidechain": True,
        },
    ]
    (subagents / "agent-flat-x.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    sessions = claude.list_sessions(base_dir=str(base))
    assert len(sessions) == 1
    sub = sessions[0]
    assert sub.kind == "subagent"
    # Flat form → spawner from sessionId; the slug folder ("-slug") is NOT
    # a parent uuid and must never be used as one.
    assert sub.parent_uuid == "spawner-flat-9"
    assert sub.parent_uuid != "-slug"
    # project_slug still correctly identifies the slug dir (skip ONE level).
    assert sub.extra.get("project_slug") == "-slug"


def test_nested_subagent_under_custom_base_dir(tmp_path: Path) -> None:
    """Directory form under a custom base_dir: the wrapper folder is a
    per-session uuid and remains the parent (structural detection)."""
    base = tmp_path / "store2"
    subagents = base / "-slug" / "parent-uuid-Z" / "subagents"
    subagents.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "nested task"},
            "timestamp": "2026-06-14T16:30:00Z",
            "sessionId": "parent-uuid-Z",
            "isSidechain": True,
        },
    ]
    (subagents / "agent-nested-y.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    sessions = claude.list_sessions(base_dir=str(base))
    assert len(sessions) == 1
    sub = sessions[0]
    assert sub.kind == "subagent"
    assert sub.parent_uuid == "parent-uuid-Z"
    # Directory form → skip TWO levels to reach the slug.
    assert sub.extra.get("project_slug") == "-slug"


# ---------------------------------------------------------------------------
# Thinking blocks + per-message token usage (F3.3 breakdown groundwork)
# ---------------------------------------------------------------------------


def _write_claude_session(
    tmp_sessions_dir: Path, sid: str, records: list[dict]
) -> str:
    """Write ``records`` as ``<sid>.jsonl`` under the fake projects tree."""
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-think" / f"{sid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(tmp_sessions_dir / ".claude" / "projects")


_THINK_USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 10,
    "cache_creation_input_tokens": 5,
}


def test_thinking_block_fills_thinking_not_text(tmp_sessions_dir: Path) -> None:
    """``thinking`` blocks land in ``Message.thinking``; ``text`` unchanged;
    ``redacted_thinking`` (no plaintext) is skipped."""
    base = _write_claude_session(
        tmp_sessions_dir,
        "claude-think-1",
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "solve it"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            {
                "type": "assistant",
                "requestId": "req-1",
                "message": {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "let me reason"},
                        {"type": "redacted_thinking", "data": "opaque-blob"},
                        {"type": "text", "text": "the answer"},
                    ],
                    "usage": _THINK_USAGE,
                },
                "timestamp": "2026-06-14T10:00:05Z",
            },
        ],
    )
    msgs = claude.read_messages("claude-think-1", base_dir=base)
    assert len(msgs) == 2
    assert msgs[0].thinking == ""
    assistant = msgs[1]
    assert assistant.thinking == "let me reason"
    assert assistant.text == "the answer"          # text semantics unchanged
    assert "opaque-blob" not in assistant.thinking  # redacted: no plaintext


def test_streamed_records_share_tokens_block_and_call_key(
    tmp_sessions_dir: Path,
) -> None:
    """Two records of the SAME streamed API call carry identical tokens
    blocks with the same internal ``_call`` key (downstream dedup unit)."""
    base = _write_claude_session(
        tmp_sessions_dir,
        "claude-stream-1",
        [
            # First record of the call: thinking-only (projections drop it).
            {
                "type": "assistant",
                "requestId": "req-1",
                "message": {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "hmm"}],
                    "usage": _THINK_USAGE,
                },
                "timestamp": "2026-06-14T10:00:05Z",
            },
            # Second record of the SAME call (same id/requestId/usage).
            {
                "type": "assistant",
                "requestId": "req-1",
                "message": {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": _THINK_USAGE,
                },
                "timestamp": "2026-06-14T10:00:06Z",
            },
            # A distinct second call.
            {
                "type": "assistant",
                "requestId": "req-2",
                "message": {
                    "id": "msg-2",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "more"}],
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
                "timestamp": "2026-06-14T10:00:10Z",
            },
        ],
    )
    msgs = claude.read_messages("claude-stream-1", base_dir=base)
    assert len(msgs) == 3
    first, second, third = msgs
    expected = {
        "input": 100, "output": 50, "reasoning": None,
        "cache_read": 10, "cache_write": 5, "total": 165,
        "_call": "msg-1|req-1",
    }
    assert first.tokens == expected
    assert second.tokens == expected           # every record of the call
    assert third.tokens == {
        "input": 10, "output": 20, "reasoning": None,
        "cache_read": 0, "cache_write": 0, "total": 30,
        "_call": "msg-2|req-2",
    }
    # The session SSOT still dedups the streamed call (165 + 30, not 330).
    usage = claude.read_token_usage("claude-stream-1", base_dir=base)
    assert usage is not None and usage["total"] == 195


def test_usage_less_records_have_no_tokens(
    fake_claude_session: Path, tmp_sessions_dir: Path
) -> None:
    """No ``message.usage`` in the record → ``tokens`` stays None (honest)."""
    base = str(tmp_sessions_dir / ".claude" / "projects")
    msgs = claude.read_messages("test-claude-1", base_dir=base)
    assert all(m.tokens is None for m in msgs)


def test_qa_linker_passes_thinking_and_tokens_through(
    tmp_sessions_dir: Path,
) -> None:
    """``_link_ask_user_questions`` reconstructs Messages — it must carry
    ``thinking``/``tokens`` (silent data loss otherwise)."""
    base = _write_claude_session(
        tmp_sessions_dir,
        "claude-ask-think-1",
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "Plan the work"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            # Assistant turn: thinking + AskUserQuestion, WITH usage.  The
            # ``_ask_*`` keys force the linker down its reconstruction path.
            {
                "type": "assistant",
                "requestId": "req-1",
                "message": {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "which option?"},
                        {
                            "type": "tool_use",
                            "id": "toolu_ask1",
                            "name": "AskUserQuestion",
                            "input": {
                                "questions": [
                                    {
                                        "question": "Which approach?",
                                        "options": [
                                            {"label": "Option A"},
                                            {"label": "Option B"},
                                        ],
                                    }
                                ]
                            },
                        },
                    ],
                    "usage": _THINK_USAGE,
                },
                "timestamp": "2026-06-14T10:00:05Z",
            },
            # The user's reply: qa lands here (also reconstructed).
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_ask1",
                            "content": '"Which approach?"="Option B"',
                        }
                    ],
                },
                "timestamp": "2026-06-14T10:00:09Z",
            },
        ],
    )
    msgs = claude.read_messages("claude-ask-think-1", base_dir=base)
    assert len(msgs) == 3
    assistant = msgs[1]
    # Scrubbed (reconstructed) — yet thinking/tokens survived.
    assert all(
        not k.startswith("_") for tu in assistant.tool_use for k in tu
    )
    assert assistant.thinking == "which option?"
    assert assistant.tokens is not None
    assert assistant.tokens["total"] == 165
    assert assistant.tokens["_call"] == "msg-1|req-1"
    # And the qa pairing itself still works on the answer message.
    answer = msgs[2]
    assert answer.qa and answer.qa[0]["answer"] == "Option B"
