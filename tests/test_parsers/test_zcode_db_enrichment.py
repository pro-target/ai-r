"""ZCode DB enrichment (2026-11): inputIntent quotes, session_input,
tool_usage join, turn_usage stats — hermetic sqlite fixtures.

Each test builds a small DB with the NEW tables (``session_input``,
``tool_usage``, ``turn_usage``); the fail-soft contract is pinned against
the legacy-schema ``fake_zcode_db`` fixture (no new tables → fields just
absent, no crash).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_r.parsers import zcode
from ai_r.quotes import quotes

# ---------------------------------------------------------------------------
# Fixture: a ZCode DB with the new tables populated
# ---------------------------------------------------------------------------

_SID = "sess-zc-enrich-1"


def _make_db(tmp_sessions_dir: Path) -> Path:
    db_path = tmp_sessions_dir / ".zcode" / "cli" / "db" / "db.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, directory TEXT,
            task_type TEXT NOT NULL DEFAULT 'interactive',
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL, sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL, sequence INTEGER
        );
        CREATE TABLE session_input (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
            delivery TEXT NOT NULL, payload TEXT NOT NULL,
            admitted_sequence INTEGER NOT NULL, promoted_sequence INTEGER,
            promoted_message_id TEXT, status TEXT NOT NULL,
            status_reason TEXT, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        );
        CREATE TABLE tool_usage (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
            trace_id TEXT, tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
            side_effect_scope TEXT, read_only INTEGER, destructive INTEGER,
            approval_status TEXT, status TEXT NOT NULL, started_at INTEGER,
            first_output_at INTEGER, completed_at INTEGER, duration_ms INTEGER,
            time_to_first_output_ms INTEGER, exit_code INTEGER,
            output_bytes INTEGER NOT NULL DEFAULT 0,
            stdout_bytes INTEGER NOT NULL DEFAULT 0,
            stderr_bytes INTEGER NOT NULL DEFAULT 0,
            truncated INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            retryable INTEGER NOT NULL DEFAULT 0,
            cancelled_by_user INTEGER NOT NULL DEFAULT 0,
            error_type TEXT, error_code TEXT, error_message TEXT
        );
        CREATE TABLE turn_usage (
            session_id TEXT NOT NULL, turn_id TEXT NOT NULL, trace_id TEXT,
            user_message_id TEXT, status TEXT NOT NULL, started_at INTEGER,
            first_model_start_at INTEGER, first_token_at INTEGER,
            completed_at INTEGER, duration_ms INTEGER,
            time_to_first_token_ms INTEGER,
            model_request_count INTEGER NOT NULL DEFAULT 0,
            model_retry_count INTEGER NOT NULL DEFAULT 0,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            tool_error_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
            computed_total_tokens INTEGER NOT NULL DEFAULT 0,
            retryable INTEGER NOT NULL DEFAULT 0,
            cancelled_by_user INTEGER NOT NULL DEFAULT 0,
            context_exceeded INTEGER NOT NULL DEFAULT 0,
            error_type TEXT, error_code TEXT,
            PRIMARY KEY (session_id, turn_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, NULL, 'Enriched session', "
        "'/home/user/proj', 'interactive', 1_790_100_000_000, "
        "1_790_100_500_000)",
        (_SID,),
    )
    # User message with the structured input intent (quotes + origin).
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
        ("enr-m-0", _SID, 1_790_100_100_000, 1_790_100_100_000,
         json.dumps({
             "role": "user",
             "time": {"created": 1_790_100_100_000},
             "metadata": {"inputIntent": {
                 "inputOrigin": "desktop",
                 "conversationQuotes": [
                     {"text": "Rotate the key BEFORE scrubbing history",
                      "comment": "Done, rotated."},
                     {"text": "no comment field here"},
                 ],
             }},
         }), 0),
    )
    # Assistant message with a tool call (callID joins tool_usage).
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
        ("enr-m-1", _SID, 1_790_100_200_000, 1_790_100_200_000,
         json.dumps({
             "role": "assistant",
             "time": {"created": 1_790_100_200_000},
             "modelId": "GLM-5.3",
             "tokens": {"total": 90, "input": 70, "output": 20,
                        "reasoning": 0, "cache": {"read": 0, "write": 0}},
         }), 1),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("enr-p-0", "enr-m-0", _SID, 1_790_100_100_000,
         1_790_100_100_000,
         json.dumps({"type": "text", "text": "rotate the key first"}), 0),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("enr-p-1", "enr-m-1", _SID, 1_790_100_200_000,
         1_790_100_200_000,
         json.dumps({"type": "tool", "tool": "Bash",
                     "callID": "call_enr_1",
                     "state": {"status": "completed",
                               "input": {"command": "rm -rf /tmp/x"},
                               "output": "ok"}}), 0),
    )
    conn.executemany(
        "INSERT INTO session_input VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?)",
        [
            ("si-1", _SID, "sendText", "startNow",
             json.dumps({"text": "never mind, cancel this"}),
             1, 1, "enr-m-0", "promoted", None,
             1_790_100_050_000, 1_790_100_100_000),
            ("si-2", _SID, "sendText", "startNow",
             json.dumps({"text": "typo, discard"}),
             2, None, None, "cancelled", "user_cancelled",
             1_790_100_150_000, 1_790_100_160_000),
            ("si-3", _SID, "sendText", "startNow",
             json.dumps({"text": "queued then dropped"}),
             3, None, None, "discarded", "superseded",
             1_790_100_170_000, 1_790_100_180_000),
            ("si-4", _SID, "backgroundNotification", "startNow",
             json.dumps({"text":
                         "<task-notification>\n"
                         "<task-id>agent_abc123</task-id>\n"
                         "<tool-use-id>call_enr_1</tool-use-id>\n"
                         "<status>completed</status>\n"
                         "<summary>Builder finished the port</summary>\n"
                         "</task-notification>"}),
             4, None, None, "promoted", None,
             1_790_100_300_000, 1_790_100_300_000),
        ],
    )
    conn.execute(
        "INSERT INTO tool_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tu-1", _SID, "turn-1", "tr-1", "call_enr_1", "Bash",
         "session", 0, 1, "allowed_once", "completed",
         1_790_100_200_000, 1_790_100_201_000, 1_790_100_205_000,
         5_000, 1_000, 0, 100, 60, 40, 0, 0, 0, 1, None, None, None),
    )
    conn.executemany(
        "INSERT INTO turn_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (_SID, "turn-1", "tr-1", "enr-m-0", "completed",
             1_790_100_100_000, 1_790_100_100_500, 1_790_100_101_000,
             1_790_100_205_000, 105_000, 1_000, 1, 1, 1, 0,
             70, 20, 0, 0, 0, 90, 0, 0, 0, None, None),
            (_SID, "turn-2", "tr-2", None, "error",
             1_790_100_400_000, 1_790_100_400_500, None,
             1_790_100_402_000, 2_000, None, 1, 2, 0, 0,
             0, 0, 0, 0, 0, 0, 1, 0, 1, "context_length_exceeded", None),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def enrich_db(tmp_sessions_dir: Path) -> Path:
    return _make_db(tmp_sessions_dir)


# ---------------------------------------------------------------------------
# 1. message.data.metadata.inputIntent → Message.extra
# ---------------------------------------------------------------------------


def test_input_intent_quotes_and_origin(enrich_db: Path) -> None:
    msgs = zcode.read_messages(_SID, override=str(enrich_db))
    user_msg = msgs[0]
    assert user_msg.role == "user"
    assert user_msg.extra["input_origin"] == "desktop"
    quotes_seen = user_msg.extra["conversation_quotes"]
    assert quotes_seen == [
        {"text": "Rotate the key BEFORE scrubbing history",
         "comment": "Done, rotated."},
        # A quote without a comment field keeps only the text.
        {"text": "no comment field here"},
    ]
    # No intent on the assistant message → empty extra, never fabricated.
    assert msgs[1].extra == {}


def test_quotes_preset_prefers_structured_zcode(enrich_db: Path) -> None:
    out = quotes(agent="zcode", session=_SID)
    assert out["count"] == 2
    assert out["by_source_kind"] == {"structured": 2}
    rec = out["quotes"][0]
    assert rec["source_kind"] == "structured"
    assert "Rotate the key BEFORE scrubbing history" in rec["quote"]
    assert rec["comment"] == "Done, rotated."
    # No heuristic assistant source was invented.
    assert rec["source_id"] is None


# ---------------------------------------------------------------------------
# 2. session_input → read_session extras
# ---------------------------------------------------------------------------


def test_session_input_extras(enrich_db: Path) -> None:
    s = zcode.read_session(_SID, override=str(enrich_db))
    cancelled = s.extra["unpromoted_inputs"]
    assert len(cancelled) == 2
    assert {c["status"] for c in cancelled} == {"cancelled", "discarded"}
    assert all("text" in c and c["text"] for c in cancelled)
    notes = s.extra["background_notifications"]
    assert len(notes) == 1
    note = notes[0]
    assert note["task_id"] == "agent_abc123"
    assert note["tool_use_id"] == "call_enr_1"
    assert note["status"] == "completed"
    assert note["summary"] == "Builder finished the port"


# ---------------------------------------------------------------------------
# 3. tool_usage join → tool_use entries
# ---------------------------------------------------------------------------


def test_tool_usage_join(enrich_db: Path) -> None:
    msgs = zcode.read_messages(_SID, override=str(enrich_db))
    calls = msgs[1].tool_use
    assert len(calls) == 1
    call = calls[0]
    assert call["tool_use_id"] == "call_enr_1"
    assert call["approval_status"] == "allowed_once"
    assert call["destructive"] is True
    assert call["cancelled_by_user"] is True
    assert call["exit_code"] == 0
    assert call["duration_ms"] == 5_000


# ---------------------------------------------------------------------------
# 4. turn_usage → read_token_usage
# ---------------------------------------------------------------------------


def test_turn_usage_enrichment(enrich_db: Path) -> None:
    usage = zcode.read_token_usage(_SID, override=str(enrich_db))
    assert usage is not None
    assert usage["total"] == 90  # message tokens still the base
    assert usage["turns"] == 2
    assert usage["model_retry_count"] == 3
    assert usage["context_exceeded"] == 1
    assert usage["turn_duration_ms"] == 107_000
    assert usage["turn_error_types"] == {"context_length_exceeded": 1}


# ---------------------------------------------------------------------------
# 5. Legacy schema (no new tables) → fail-soft
# ---------------------------------------------------------------------------


def test_legacy_schema_fail_soft(fake_zcode_db: Path) -> None:
    # Messages/tokens/session read exactly as before; new fields absent.
    msgs = zcode.read_messages(
        "sess_test-zc-1", override=str(fake_zcode_db)
    )
    assert msgs
    assert all(m.extra == {} for m in msgs)
    assert all(
        "approval_status" not in c
        for m in msgs for c in m.tool_use
    )
    s = zcode.read_session("sess_test-zc-1", override=str(fake_zcode_db))
    assert "unpromoted_inputs" not in s.extra
    assert "background_notifications" not in s.extra
    usage = zcode.read_token_usage(
        "sess_test-zc-1", override=str(fake_zcode_db)
    )
    assert usage is not None
    assert "turns" not in usage


def test_message_error_in_extra(tmp_path: Path) -> None:
    db_file = tmp_path / "zcode.sqlite"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, directory TEXT,
            task_type TEXT NOT NULL DEFAULT 'interactive',
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL, sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL, sequence INTEGER
        );
        """
    )
    sid = "sess_err_test"
    conn.execute(
        "INSERT INTO session VALUES (?, NULL, 'Err Test', '/tmp', 'interactive', 1000, 2000)",
        (sid,)
    )
    msg_data = {
        "role": "assistant",
        "error": {
            "name": "AiSdkModelAdapterError",
            "data": {"message": "rate limit exceeded", "code": "rate_limited"}
        }
    }
    conn.execute(
        "INSERT INTO message VALUES ('m1', ?, 1000, 1000, ?, 1)",
        (sid, json.dumps(msg_data))
    )
    conn.commit()
    conn.close()

    msgs = zcode.read_messages(sid, override=str(db_file))
    assert len(msgs) == 1
    assert "error" in msgs[0].extra
    assert msgs[0].extra["error"]["name"] == "AiSdkModelAdapterError"
    assert msgs[0].extra["error"]["data"]["code"] == "rate_limited"

