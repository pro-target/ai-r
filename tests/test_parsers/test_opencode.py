"""Tests for the OpenCode SQLite parser."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_reader.parsers import AgentName, opencode
from ai_reader.parsers.opencode import (
    _epoch_ms_to_datetime,
    _open_db,
    _resolve_db_paths,
    _row_to_session,
)


def test_list_sessions_real(real_opencode_db: Path) -> None:
    sessions = opencode.list_sessions(override=str(real_opencode_db))
    assert sessions, "expected at least one OpenCode session on this host"
    for s in sessions[:3]:
        assert s.agent is AgentName.OPENCODE
        assert s.title
        assert s.message_count >= 0
    # No duplicates across the same DB.
    ids = [s.uuid for s in sessions]
    assert len(ids) == len(set(ids))


def test_get_session_info(fake_opencode_db: Path) -> None:
    s = opencode.read_session("test-oc-1", override=str(fake_opencode_db))
    assert s.uuid == "test-oc-1"
    assert s.agent is AgentName.OPENCODE
    assert s.title == "First OpenCode session"
    assert s.message_count == 2
    assert s.parent_uuid is None

    child = opencode.read_session("test-oc-2", override=str(fake_opencode_db))
    assert child.parent_uuid == "test-oc-1"
    assert child.message_count == 1


def test_handle_locked_db_gracefully(fake_opencode_db: Path) -> None:
    """Opening the same DB twice should not error."""
    conn_a = _open_db(str(fake_opencode_db))
    conn_b = _open_db(str(fake_opencode_db))
    assert conn_a is not None
    assert conn_b is not None
    conn_a.close()
    conn_b.close()


def test_open_db_missing_file_returns_none(tmp_path: Path) -> None:
    assert _open_db(str(tmp_path / "nope.db")) is None


def test_list_sessions_missing_db_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no real DB and a non-existent base_dir, the function returns [].

    The OpenCode parser always considers the well-known default paths
    (native + snap).  We monkeypatch the discovery helper to return no
    candidates, simulating a host with no OpenCode installation.
    """
    monkeypatch.setattr(
        "ai_reader.parsers.opencode._resolve_db_paths",
        lambda base_dir=None, override=None: [],
    )
    assert opencode.list_sessions(base_dir=str(tmp_path / "no-such-dir")) == []


def test_read_session_invalid_uuid(fake_opencode_db: Path) -> None:
    with pytest.raises(ValueError):
        opencode.read_session("../escape", override=str(fake_opencode_db))
    with pytest.raises(ValueError):
        opencode.read_session("", override=str(fake_opencode_db))


def test_read_session_missing(fake_opencode_db: Path) -> None:
    with pytest.raises(FileNotFoundError):
        opencode.read_session("nope", override=str(fake_opencode_db))


def test_session_exists(fake_opencode_db: Path) -> None:
    assert opencode.session_exists("test-oc-1", override=str(fake_opencode_db)) is True
    assert opencode.session_exists("test-oc-2", override=str(fake_opencode_db)) is True
    assert opencode.session_exists("nope", override=str(fake_opencode_db)) is False
    assert opencode.session_exists("../escape", override=str(fake_opencode_db)) is False


def test_search(
    fake_opencode_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake DB has ``test-oc-1``; hide the real DBs so the search
    result is deterministic.
    """
    monkeypatch.setattr(
        "ai_reader.parsers.opencode._resolve_db_paths",
        lambda base_dir=None, override=None: [str(fake_opencode_db)],
    )
    out = opencode.search("opencode")
    assert len(out) == 1
    assert out[0].uuid == "test-oc-1"
    assert opencode.search("zzz") == []
    assert opencode.search("") == []


def test_resolve_db_paths_dedup(fake_opencode_db: Path, tmp_path: Path) -> None:
    """The override path is deduped against the same realpath (via symlink)."""
    link = tmp_path / "linked.db"
    try:
        link.symlink_to(fake_opencode_db)
    except OSError:
        pytest.skip("symlinks unavailable on this fs")
    paths = _resolve_db_paths(override=str(link))
    realpaths = {Path(p).resolve() for p in paths}
    # The override and the symlink both point at the same file.
    assert Path(link).resolve() in realpaths
    # The dedup pass collapses them — the symlink target realpath
    # appears at most once across ``override`` and the native lookup.
    occurrences = sum(
        1 for p in paths if Path(p).resolve() == Path(link).resolve()
    )
    assert occurrences == 1


def test_epoch_ms_to_datetime() -> None:
    dt = _epoch_ms_to_datetime(1_716_000_000_000)
    assert dt.year == 2024
    assert dt.tzinfo is not None


def test_row_to_session(fake_opencode_db: Path) -> None:
    conn = sqlite3.connect(str(fake_opencode_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, title, time_created, time_updated, parent_id "
        "FROM session WHERE id = ?",
        ("test-oc-1",),
    ).fetchone()
    session = _row_to_session(row, str(fake_opencode_db))
    assert session.uuid == "test-oc-1"
    assert session.title == "First OpenCode session"
    assert session.path == str(fake_opencode_db)
    assert session.parent_uuid is None
    conn.close()


def test_row_to_session_untitled() -> None:
    """A session with a NULL title falls back to ``Untitled``."""
    import sqlite3
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER);"
    )
    conn.execute(
        "INSERT INTO session VALUES (?, NULL, NULL, 1700000000000, 1700000000000)",
        ("u",),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM session WHERE id = 'u'").fetchone()
    session = _row_to_session(row, str(path))
    assert session.title == "Untitled"
    conn.close()
    path.unlink()


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


def test_read_messages_basic(fake_opencode_db: Path) -> None:
    """The minimal fixture has parts: user text + assistant text."""
    msgs = opencode.read_messages("test-oc-1", override=str(fake_opencode_db))
    assert isinstance(msgs, list)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].text == "Hello"
    assert msgs[1].role == "assistant"
    assert msgs[1].text == "Hi there"


def test_read_messages_message_without_parts_is_graceful(
    fake_opencode_db: Path,
) -> None:
    """test-oc-2's message has no part rows → empty text, no crash."""
    msgs = opencode.read_messages("test-oc-2", override=str(fake_opencode_db))
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert msgs[0].text == ""


def test_read_messages_partless_db_falls_back_to_metadata(tmp_path: Path) -> None:
    """An old DB with no ``part`` table at all still parses without crashing."""
    db = tmp_path / "nodb.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session VALUES ('s', NULL, 't', 1, 2)")
    conn.execute(
        "INSERT INTO message VALUES ('m', 's', 1, 1, ?)",
        (json.dumps({"role": "assistant", "content": "legacy text"}),),
    )
    conn.commit()
    conn.close()
    msgs = opencode.read_messages("s", override=str(db))
    assert len(msgs) == 1
    # Fallback path reads content from message.data when no parts exist.
    assert msgs[0].text == "legacy text"


def test_read_messages_preserves_tool_calls(fake_opencode_db_with_tools: Path) -> None:
    msgs = opencode.read_messages("oc-tools-1", override=str(fake_opencode_db_with_tools))
    assert len(msgs) == 2
    user = msgs[0]
    assert user.role == "user"
    assert user.text == "run tests"
    assistant = msgs[1]
    assert assistant.role == "assistant"
    # text = reasoning + text parts, in order; step-* skipped.
    assert "thinking..." in assistant.text
    assert "okay" in assistant.text
    # step-start/step-finish leak no text
    assert "snapshot" not in assistant.text
    assert "tokens" not in assistant.text
    # Two real tool parts + file/patch metadata entries.
    assert len(assistant.tool_use) == 4
    assert assistant.tool_use[0]["name"] == "shell"
    assert "pytest" in assistant.tool_use[0]["input"]
    assert assistant.tool_use[1]["name"] == "write"
    assert assistant.tool_use[2]["name"] == "file"
    file_input = json.loads(assistant.tool_use[2]["input"])
    assert file_input["mime"] == "image/png"
    assert file_input["filename"] == "manifest.png"
    assert file_input["url"] == {"omitted": "data-url"}
    assert "iVBOR" not in assistant.tool_use[2]["input"]
    assert assistant.tool_use[3]["name"] == "patch"
    patch_input = json.loads(assistant.tool_use[3]["input"])
    assert patch_input["hash"] == "abc123"
    assert patch_input["files"] == [{"path": "src/app.py", "added": 3, "removed": 1}]
    # Only the completed tool produced an output → one tool_result.
    assert len(assistant.tool_result) == 1
    assert assistant.tool_result[0]["content"] == "5 passed"


def test_tool_use_entries_carry_per_part_timestamp(
    fake_opencode_db_with_tools: Path,
) -> None:
    """Each ``tool_use`` entry exposes the originating part's timestamp.

    The fixture seeds 4 tool-like parts (shell, write, file, patch) at
    ``t0+3``/``t0+4``/``t0+5``/``t0+6`` respectively — the timestamp on
    each entry must match its part, not the message-level ts.
    """
    msgs = opencode.read_messages(
        "oc-tools-1", override=str(fake_opencode_db_with_tools)
    )
    assistant = msgs[1]
    t0_ms = 1_716_000_200_000
    expected = {
        "shell": t0_ms + 3,
        "write": t0_ms + 4,
        "file": t0_ms + 5,
        "patch": t0_ms + 6,
    }
    from datetime import datetime, timezone
    for tool in assistant.tool_use:
        assert "timestamp" in tool, tool
        ts = tool["timestamp"]
        assert isinstance(ts, datetime)
        assert ts.tzinfo is not None
        assert ts.utcoffset() == timezone.utc.utcoffset(ts)
        assert ts == datetime.fromtimestamp(
            expected[tool["name"]] / 1000.0, tz=timezone.utc
        )


def test_message_timestamp_is_earliest_part(
    fake_opencode_db_with_tools: Path,
) -> None:
    """``Message.timestamp`` reflects the earliest part time, not the latest.

    Without parts the message falls back to its own ``mtime``; with
    parts, the first part's ``time_created`` wins so consumers can rely
    on "first thing happened" semantics.
    """
    from datetime import datetime, timezone
    msgs = opencode.read_messages(
        "oc-tools-1", override=str(fake_opencode_db_with_tools)
    )
    assistant = msgs[1]
    ts = assistant.timestamp
    assert isinstance(ts, datetime)
    assert ts.tzinfo is not None
    # a1-p0 (step-start) is at t0+0; assistant mtime is at t0; earliest
    # wins → t0.
    t0_ms = 1_716_000_200_000
    assert ts == datetime.fromtimestamp(t0_ms / 1000.0, tz=timezone.utc)


def test_read_messages_missing_raises(fake_opencode_db: Path) -> None:
    with pytest.raises(FileNotFoundError):
        opencode.read_messages("nope", override=str(fake_opencode_db))


def test_read_messages_invalid_uuid(fake_opencode_db: Path) -> None:
    with pytest.raises(ValueError):
        opencode.read_messages("../escape", override=str(fake_opencode_db))


def test_open_db_locked_falls_back_to_copy(
    fake_opencode_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently-locked DB is copied to /tmp and opened from there.

    Exercises opencode.py:162-168 (``shutil.copy2`` fallback). Deterministic:
    the read-only ``sqlite3.connect`` is forced to raise 'database is
    locked' on every retry, so the copy branch runs without relying on
    real cross-process lock contention. (Audit 2026-06-21 gap.)
    """
    import glob
    import sqlite3 as _sqlite3

    from ai_reader.parsers import opencode as oc

    real_connect = _sqlite3.connect

    def fake_connect(target, *args, **kwargs):
        if isinstance(target, str) and "mode=ro" in target:
            raise _sqlite3.OperationalError("database is locked")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(oc.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(oc.time, "sleep", lambda *_a, **_k: None)

    conn = oc._open_db(str(fake_opencode_db))
    assert conn is not None
    try:
        # The fallback connection reads the copied schema.
        count = conn.execute("SELECT count(*) FROM session").fetchone()[0]
        assert count >= 0
    finally:
        conn.close()

    assert glob.glob("/tmp/ai_reader_opencode_*.db"), (
        "expected a fallback copy under /tmp"
    )
