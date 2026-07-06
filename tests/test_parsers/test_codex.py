"""Tests for the Codex session parser."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai_r.parsers import AgentName, codex
from ai_r.parsers.codex import (
    _extract_text_from_parts,
    _is_system_noise,
    _is_valid_uuid,
    _parse_iso_timestamp,
    _scan_file,
)


def test_list_sessions_real(real_codex_dir: Path) -> None:
    sessions = codex.list_sessions(base_dir=str(real_codex_dir))
    assert sessions, "expected at least one Codex session on this host"
    for s in sessions[:3]:
        assert s.agent is AgentName.CODEX
        assert s.title
        assert "rollout-" in s.path
    # Most-recent first.
    dates = [s.date for s in sessions]
    assert dates == sorted(dates, reverse=True)


def test_list_sessions_synthetic(fake_codex_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    sessions = codex.list_sessions(base_dir=base)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.uuid == "test-codex-1"
    assert s.agent is AgentName.CODEX
    assert s.title == "Roll out please"  # first user message becomes title
    assert s.message_count == 2  # 1 user + 1 assistant (system noise ignored)
    assert s.extra.get("cwd") == "/tmp/work"


def test_parse_message(fake_codex_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    s = codex.read_session("test-codex-1", base_dir=base)
    assert s.title == "Roll out please"
    assert s.message_count == 2
    assert s.uuid == "test-codex-1"


def test_extract_uuid_from_filename(fake_codex_session: Path) -> None:
    """The filename embeds the uuid — verify the regex extracts it."""
    name = fake_codex_session.name  # rollout-2026-06-14T10-00-00-test-codex-1.jsonl
    m = re.search(r"rollout-\d{4}-\d{2}-\d{2}T[\d-]+-(.+)\.jsonl$", name)
    assert m is not None
    assert m.group(1) == "test-codex-1"


def test_list_sessions_dedupes_same_uuid(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
    base.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": "2026-06-14T10:00:00Z",
        "type": "session_meta",
        "payload": {"id": "dup", "timestamp": "2026-06-14T10:00:00Z"},
    }
    (base / "rollout-2026-06-14T10-00-00-dup.jsonl").write_text(
        json.dumps(payload) + "\n", encoding="utf-8"
    )
    (base / "rollout-2026-06-14T11-00-00-dup.jsonl").write_text(
        json.dumps(payload) + "\n", encoding="utf-8"
    )
    sessions = codex.list_sessions(base_dir=str(tmp_sessions_dir / ".codex" / "sessions"))
    assert len(sessions) == 1


def test_session_meta_without_id_returns_none(tmp_path: Path) -> None:
    base = tmp_path / "x"
    base.mkdir()
    (base / "rollout-test.jsonl").write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"timestamp": "2026-06-14T10:00:00Z"}}
        )
        + "\n",
        encoding="utf-8",
    )
    sessions = codex.list_sessions(base_dir=str(base))
    assert sessions == []


def test_read_session_invalid_uuid(tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    with pytest.raises(ValueError):
        codex.read_session("../escape", base_dir=base)
    with pytest.raises(ValueError):
        codex.read_session("", base_dir=base)


def test_read_session_missing(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        codex.read_session("nope", base_dir=str(tmp_sessions_dir / ".codex" / "sessions"))


def test_search_filters_titles(fake_codex_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    out = codex.search("roll out", base_dir=base)
    assert len(out) == 1
    assert codex.search("zzz", base_dir=base) == []
    assert codex.search("", base_dir=base) == []


def test_extract_text_from_parts() -> None:
    parts = [
        {"type": "input_text", "text": "hello"},
        {"type": "output_text", "text": "world"},
        {"type": "image", "text": "ignored"},
        {"text": "no-type"},
        "not-a-dict",
    ]
    assert _extract_text_from_parts(parts) == "hello\nworld\nno-type"


def test_extract_text_from_parts_non_list() -> None:
    """The function only iterates a list; everything else is empty."""
    assert _extract_text_from_parts(None) == ""  # type: ignore[arg-type]
    assert _extract_text_from_parts("a string") == ""  # type: ignore[arg-type]
    assert _extract_text_from_parts(123) == ""  # type: ignore[arg-type]


def test_is_system_noise() -> None:
    assert _is_system_noise("<permissions")
    assert _is_system_noise("## Apps")
    assert _is_system_noise("<command-message>")
    assert _is_system_noise("<system-reminder>")
    assert not _is_system_noise("ordinary prompt text")


def test_is_valid_uuid() -> None:
    assert _is_valid_uuid("abc-123")
    assert not _is_valid_uuid("")
    assert not _is_valid_uuid(" has space")
    assert not _is_valid_uuid("has/slash")
    assert not _is_valid_uuid("has\\slash")
    assert not _is_valid_uuid(None)  # type: ignore[arg-type]


def test_parse_iso_timestamp_variants() -> None:
    assert _parse_iso_timestamp("2026-06-14T10:00:00Z") is not None
    assert _parse_iso_timestamp("2026-06-14T10:00:00.123456Z") is not None
    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("nope") is None
    assert _parse_iso_timestamp(123) is None  # type: ignore[arg-type]


def test_scan_file_returns_none_on_unreadable(tmp_path: Path) -> None:
    # Point at a path that doesn't exist; the inner ``open`` will raise OSError.
    ghost = tmp_path / "nope.jsonl"
    assert _scan_file(ghost) is None


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


def test_read_messages_basic(fake_codex_session: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    msgs = codex.read_messages("test-codex-1", base_dir=base)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].text == "Roll out please"
    assert msgs[1].role == "assistant"
    assert msgs[1].text == "Done."


def test_read_messages_preserves_tool_calls(
    fake_codex_session_with_tools: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    msgs = codex.read_messages("codex-tools-1", base_dir=base)
    # user message + function_call + function_call_output
    assert len(msgs) == 3
    assert msgs[0].role == "user"
    call = msgs[1]
    assert call.role == "assistant"
    assert len(call.tool_use) == 1
    assert call.tool_use[0]["name"] == "shell"
    assert call.tool_use[0]["input"] == "pytest"
    out = msgs[2]
    assert out.role == "tool"
    assert len(out.tool_result) == 1
    assert out.tool_result[0]["content"] == "5 passed"


def test_read_messages_missing_raises(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        codex.read_messages("nope", base_dir=str(tmp_sessions_dir / ".codex" / "sessions"))


def test_read_messages_invalid_uuid(tmp_sessions_dir: Path) -> None:
    with pytest.raises(ValueError):
        codex.read_messages("../escape", base_dir=str(tmp_sessions_dir / ".codex" / "sessions"))


def test_extract_event_msg_user_message(codex_event_msg_jsonl: Path) -> None:
    msgs = codex._extract_messages_from_rollout(codex_event_msg_jsonl)
    user_msgs = [m for m in msgs if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].text == "hello world from response_item"
    assert sum(1 for m in msgs if m.role == "assistant") == 1


def test_extract_event_msg_short_message_filtered(tmp_path: Path) -> None:
    """The ``len(msg) <= 10`` gate drops short-but-valid prompts (codex.py:395).

    Documents current behaviour: a 3-char ``event_msg`` user_message is
    discarded while an 11+ char one survives. (Audit 2026-06-21 gap.)
    """
    rollout = tmp_path / "rollout-short.jsonl"
    records = [
        {
            "timestamp": "2026-06-14T10:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "yes"},
        },
        {
            "timestamp": "2026-06-14T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "yes please continue"},
        },
    ]
    rollout.write_text(
        "".join(json.dumps(rec) + "\n" for rec in records), encoding="utf-8"
    )
    msgs = codex._extract_messages_from_rollout(rollout)
    texts = [m.text for m in msgs if m.role == "user"]
    assert "yes" not in texts
    assert "yes please continue" in texts


def test_discover_archived_sessions(tmp_sessions_dir: Path) -> None:
    active_uuid = "active-uuid-1"
    archived_uuid = "archived-uuid-1"
    active = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T10-00-00-{active_uuid}.jsonl"
    )
    archived = (
        tmp_sessions_dir
        / ".codex"
        / "archived_sessions"
        / "2026"
        / "05"
        / "01"
        / f"rollout-2026-05-01T10-00-00-{archived_uuid}.jsonl"
    )
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-14T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": active_uuid, "timestamp": "2026-06-14T10:00:00Z"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-01T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": archived_uuid, "timestamp": "2026-05-01T10:00:00Z"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = codex.list_sessions(
        base_dir=str(tmp_sessions_dir / ".codex" / "sessions")
    )
    uuids = [s.uuid for s in sessions]
    assert active_uuid in uuids
    assert archived_uuid in uuids
    assert len(sessions) == 2


def test_dedup_key_len_default_is_256() -> None:
    assert codex.get_dedup_key_len() == 256


def test_dedup_key_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime env changes are honored — no module reload required."""
    monkeypatch.setenv("AI_R_DEDUP_KEY_LEN", "1024")
    try:
        assert codex.get_dedup_key_len() == 1024
        assert len(codex._dedup_key("x" * 5000)) == 1024
    finally:
        monkeypatch.delenv("AI_R_DEDUP_KEY_LEN", raising=False)
    assert codex.get_dedup_key_len() == 256


def test_dedup_key_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_R_DEDUP_KEY_LEN", "not-a-number")
    try:
        assert codex.get_dedup_key_len() == 256
    finally:
        monkeypatch.delenv("AI_R_DEDUP_KEY_LEN", raising=False)


def test_dedup_key_non_positive_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_R_DEDUP_KEY_LEN", "0")
    try:
        assert codex.get_dedup_key_len() == 256
    finally:
        monkeypatch.delenv("AI_R_DEDUP_KEY_LEN", raising=False)


def test_event_msg_filters_system_noise(tmp_sessions_dir: Path) -> None:
    """event_msg.user_message payloads starting with system-noise prefixes
    must be skipped, not projected as user-role messages."""
    uuid_str = "system-noise-uuid"
    rollout = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T12-00-00-{uuid_str}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-06-14T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": uuid_str, "timestamp": "2026-06-14T12:00:00Z"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-14T12:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "<command-message>orchestrator</command-message>\n<command-name>/orchestrator</command-name>",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-14T12:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "<system-reminder>agent runtime injected this</system-reminder>",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-14T12:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "a real user prompt with enough length to pass the gate",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    msgs = codex._extract_messages_from_rollout(rollout)
    user_texts = [m.text for m in msgs if m.role == "user"]
    assert user_texts == ["a real user prompt with enough length to pass the gate"]


def test_event_msg_dedup_with_response_item(tmp_sessions_dir: Path) -> None:
    """Same user text in response_item and event_msg → 1 message after dedup,
    regardless of dedup key length."""
    uuid_str = "dedup-cross-type-uuid"
    long_prompt = "x" * 500 + " distinctive-tail-marker-zzz"
    rollout = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T13-00-00-{uuid_str}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-06-14T13:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": uuid_str, "timestamp": "2026-06-14T13:00:00Z"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-14T13:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "text", "text": long_prompt}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-14T13:00:02Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": long_prompt},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    msgs = codex._extract_messages_from_rollout(rollout)
    user_msgs = [m for m in msgs if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].text == long_prompt


# ---------------------------------------------------------------------------
# Reasoning response items → Message.thinking
# ---------------------------------------------------------------------------


def test_reasoning_summary_becomes_thinking(tmp_path: Path) -> None:
    """A ``reasoning`` item's plaintext ``summary[].text`` surfaces as an
    assistant message with ``thinking`` set (text stays empty); Codex has
    no per-message usage → ``tokens`` stays None on every record."""
    rollout = tmp_path / "rollout-reasoning.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "timestamp": "2026-06-14T13:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "summary": [
                            {"type": "summary_text", "text": "plan step one"},
                            {"type": "summary_text", "text": "then step two"},
                        ],
                        "encrypted_content": "gAAAAAB-opaque",
                    },
                },
                {
                    "timestamp": "2026-06-14T13:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    msgs = codex._extract_messages_from_rollout(rollout)
    assert len(msgs) == 2
    reasoning = msgs[0]
    assert reasoning.role == "assistant"
    assert reasoning.text == ""
    assert reasoning.thinking == "plan step one\nthen step two"
    assert "gAAAAAB" not in reasoning.thinking  # ciphertext never surfaces
    assert msgs[1].thinking == ""
    assert all(m.tokens is None for m in msgs)  # cumulative-only format


def test_reasoning_without_summary_is_skipped(tmp_path: Path) -> None:
    """Encrypted-only reasoning items (``summary: []`` — the common real
    shape) yield NO message: absence of plaintext is honest, and blank
    assistant messages would pollute the dialogue."""
    rollout = tmp_path / "rollout-encrypted.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-14T13:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "gAAAAAB-opaque",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert codex._extract_messages_from_rollout(rollout) == []
