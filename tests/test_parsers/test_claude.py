"""Tests for the Claude session parser.

Covers:

* Discovery against the real ``~/.claude/projects`` tree (read-only).
* Title, role and message-count extraction from synthetic fixtures.
* UUID validation: empty, slashes, path-traversal attempts.
* The ``search`` and ``session_exists`` helpers.
"""
from __future__ import annotations

import json
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
    """An inline ``isSidechain: True`` record classifies the session."""
    base = tmp_sessions_dir / ".claude" / "projects" / "proj-a"
    jsonl = base / "inline-sidechain.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "inline task"},
            "timestamp": "2026-06-14T12:00:00Z",
            "parentUuid": "inline-parent-9",
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
    assert session.parent_uuid == "inline-parent-9"


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
