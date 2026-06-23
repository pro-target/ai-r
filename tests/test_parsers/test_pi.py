"""Tests for the Pi session parser."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.parsers import AgentName, pi
from ai_r.parsers.pi import (
    _extract_text,
    _is_valid_uuid,
    _parse_epoch_millis,
    _parse_iso_timestamp,
    _scan_file,
)


def test_list_sessions_real(real_pi_dir: Path | None) -> None:
    if real_pi_dir is None:
        pytest.skip("no real Pi sessions on this host")
    sessions = pi.list_sessions(base_dir=str(real_pi_dir))
    assert sessions, "expected at least one Pi session on this host"
    for s in sessions[:3]:
        assert s.agent is AgentName.PI
        assert s.title
        assert s.path.endswith(".jsonl")
    dates = [s.date for s in sessions]
    assert dates == sorted(dates, reverse=True)


def test_list_sessions_synthetic(fake_pi_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    sessions = pi.list_sessions(base_dir=base)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.uuid == "test-pi-1"
    assert s.agent is AgentName.PI
    assert s.title == "Add Pi support"
    assert s.message_count == 2
    assert s.extra.get("cwd") == "/tmp/work"


def test_read_session(fake_pi_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    s = pi.read_session("test-pi-1", base_dir=base)
    assert s.uuid == "test-pi-1"
    assert s.title == "Add Pi support"


def test_session_info_name_overrides_title(fake_pi_session: Path, tmp_sessions_dir: Path) -> None:
    with fake_pi_session.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_info", "name": "Named Pi session"}) + "\n")
    s = pi.read_session("test-pi-1", base_dir=str(tmp_sessions_dir / ".pi" / "agent" / "sessions"))
    assert s.title == "Named Pi session"


def test_list_sessions_dedupes_same_uuid(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".pi" / "agent" / "sessions" / "--x--"
    base.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "id": "dup", "timestamp": "2026-06-14T10:00:00Z"}
    for name in ("a_dup.jsonl", "b_dup.jsonl"):
        (base / name).write_text(json.dumps(header) + "\n", encoding="utf-8")
    sessions = pi.list_sessions(base_dir=str(tmp_sessions_dir / ".pi" / "agent" / "sessions"))
    assert len(sessions) == 1


def test_read_session_invalid_uuid(tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    with pytest.raises(ValueError):
        pi.read_session("../escape", base_dir=base)
    with pytest.raises(ValueError):
        pi.read_session("", base_dir=base)


def test_read_session_missing(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pi.read_session("nope", base_dir=str(tmp_sessions_dir / ".pi" / "agent" / "sessions"))


def test_search_filters_titles(fake_pi_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    assert len(pi.search("pi support", base_dir=base)) == 1
    assert pi.search("zzz", base_dir=base) == []
    assert pi.search("", base_dir=base) == []


def test_extract_text_skips_thinking_by_default() -> None:
    parts = [
        {"type": "thinking", "thinking": "hidden"},
        {"type": "text", "text": "hello"},
        {"type": "output_text", "text": "world"},
        {"text": "no-type"},
        {"type": "toolCall", "text": "ignored"},
    ]
    assert _extract_text(parts) == "hello\nworld\nno-type"
    assert _extract_text(parts, include_thinking=True).startswith("hidden")
    assert _extract_text("plain") == "plain"
    assert _extract_text(None) == ""


def test_is_valid_uuid() -> None:
    assert _is_valid_uuid("019ee70a-79cc-78f9")
    assert not _is_valid_uuid("")
    assert not _is_valid_uuid(" has-space")
    assert not _is_valid_uuid("has/slash")
    assert not _is_valid_uuid("has\\slash")
    assert not _is_valid_uuid(None)  # type: ignore[arg-type]


def test_parse_timestamps() -> None:
    assert _parse_iso_timestamp("2026-06-14T10:00:00.000Z") is not None
    assert _parse_iso_timestamp("bad") is None
    assert _parse_epoch_millis(1_718_360_002_000) is not None
    assert _parse_epoch_millis("bad") is None


def test_parse_timestamps_always_tz_aware() -> None:
    """Every parsed timestamp must be tz-aware so list_sessions can sort."""
    iso = _parse_iso_timestamp("2026-06-14T10:00:00.000Z")
    bare = _parse_iso_timestamp("2026-06-14T10:00:00")  # no offset
    epoch = _parse_epoch_millis(1_718_360_002_000)
    assert iso is not None and iso.tzinfo is not None
    assert bare is not None and bare.tzinfo is not None
    assert epoch is not None and epoch.tzinfo is not None


def test_scan_file_returns_none_on_unreadable(tmp_path: Path) -> None:
    assert _scan_file(tmp_path / "missing.jsonl") is None


def test_scan_file_without_header_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text('{"type":"message"}\n', encoding="utf-8")
    assert _scan_file(p) is None


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


def test_read_messages_basic(fake_pi_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    msgs = pi.read_messages("test-pi-1", base_dir=base)
    # user + assistant (the toolResult record is surfaced as a tool message)
    assert len(msgs) == 3
    assert msgs[0].role == "user"
    assert msgs[0].text == "Add Pi support"
    assert msgs[1].role == "assistant"
    assert msgs[1].text == "Done."


def test_read_messages_preserves_tool_calls(
    fake_pi_session_with_tools: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    msgs = pi.read_messages("pi-tools-1", base_dir=base)
    assert len(msgs) == 3
    assistant = msgs[1]
    assert assistant.role == "assistant"
    assert assistant.text == "Running now"
    assert len(assistant.tool_use) == 1
    assert assistant.tool_use[0]["name"] == "shell"
    assert assistant.tool_use[0]["input"] == "pytest"
    tool = msgs[2]
    assert tool.role == "tool"
    assert len(tool.tool_result) == 1
    assert tool.tool_result[0]["content"] == "5 passed"


def test_read_messages_missing_raises(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pi.read_messages("nope", base_dir=str(tmp_sessions_dir / ".pi" / "agent" / "sessions"))


def test_read_messages_invalid_uuid(tmp_sessions_dir: Path) -> None:
    with pytest.raises(ValueError):
        pi.read_messages("../escape", base_dir=str(tmp_sessions_dir / ".pi" / "agent" / "sessions"))
