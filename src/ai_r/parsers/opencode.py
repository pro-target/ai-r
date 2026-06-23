"""OpenCode session parser.

OpenCode stores everything in a single SQLite database.  The current
installations we know about:

* Native: ``~/.local/share/opencode/opencode.db``.
* Snap VSCode: ``~/snap/code/<revision>/.local/share/opencode/opencode.db``.
* Snap OpenCode: ``~/snap/opencode/<revision>/.local/share/opencode/opencode.db``.

All three are searched (deduplicated by ``realpath``) and queried
transparently.  An override path can be supplied via
``$OPENCODE_DB`` or the ``base_dir`` argument (the latter must point
to a directory containing ``opencode.db``).

Schema (relevant columns only)::

    CREATE TABLE session (
        id              TEXT PRIMARY KEY,
        parent_id       TEXT,
        title           TEXT,
        time_created    INTEGER,    -- ms epoch
        time_updated    INTEGER,    -- ms epoch
        ... (other fields ignored)
    );
    CREATE TABLE message (
        id              TEXT PRIMARY KEY,
        session_id      TEXT NOT NULL REFERENCES session(id),
        time_created    INTEGER NOT NULL,
        time_updated    INTEGER NOT NULL,
        data            TEXT NOT NULL  -- JSON metadata: role/time/agent/model
    );
    CREATE TABLE part (
        id              TEXT PRIMARY KEY,
        message_id      TEXT NOT NULL REFERENCES message(id),
        session_id      TEXT NOT NULL,
        time_created    INTEGER NOT NULL,
        time_updated    INTEGER NOT NULL,
        data            TEXT NOT NULL  -- JSON: {type, text|tool|state|...}
    );

``message.data`` carries only metadata (``role``, ``time``,
``agent``, ``model``); the actual message **text and tool calls live
in the ``part`` table**, one row per part, linked to the message by
``part.message_id`` and ordered by ``part.time_created`` (ties broken
by ``part.id``).

Observed ``part.data`` shapes (``data.type``):

* ``text``        — ``{"type":"text","text":"..."}``
* ``reasoning``   — ``{"type":"reasoning","text":"...", "time":{...}}``
* ``tool``        — single combined call+result:
  ``{"type":"tool","tool":"<name>","callID":"...","state":{
     "status":"completed|error|running",
     "input":{...},          # tool-call arguments
     "output":"..."          # tool-result content (absent on error)
  }}``
* ``step-start``  — ``{"type":"step-start","snapshot":"..."}`` (boundary)
* ``step-finish`` — ``{"type":"step-finish","tokens":{...}}`` (boundary)
* ``file``        — ``{"type":"file","mime":...,"url":"data:..."}`` (metadata kept)
* ``patch``       — ``{"type":"patch","hash":...,"files":[...]}`` (metadata kept)

A ``tool`` part is BOTH the call (``tool``+``state.input``) and the
result (``state.output``) in one row; the parser emits a
``tool_use`` entry for the call and a ``tool_result`` entry for the
output.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .models import AgentName, Message, Session


_PartTuple = Tuple[dict, Optional[int]]


_TITLE_MAX_LEN = 100
_DEFAULT_DB = "~/.local/share/opencode/opencode.db"


class _TemporaryCopyConnection(sqlite3.Connection):
    """SQLite connection that removes its temporary DB copy on close."""

    _ai_r_temp_path: str | None = None

    def close(self) -> None:
        temp_path = self._ai_r_temp_path
        try:
            super().close()
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                self._ai_r_temp_path = None


def _expand(path: str) -> str:
    return str(Path(path).expanduser())


def _resolve_db_paths(
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[str]:
    """Return all OpenCode DB paths that exist, in priority order.

    Priority:

    1. ``override`` (if given and a regular file).
    2. ``$OPENCODE_DB`` env var.
    3. ``base_dir/opencode.db`` (if ``base_dir`` is supplied).
    4. Native ``~/.local/share/opencode/opencode.db``.
    5. All ``~/snap/code/*/.local/share/opencode/opencode.db``.
    6. All ``~/snap/opencode/*/.local/share/opencode/opencode.db``.
    """
    candidates: List[str] = []

    def _add(path: str) -> None:
        if path and os.path.isfile(path):
            candidates.append(path)

    if override:
        _add(override)
    env_override = os.environ.get("OPENCODE_DB")
    if env_override:
        _add(env_override)
    if base_dir:
        _add(os.path.join(base_dir, "opencode.db"))

    _add(_expand(_DEFAULT_DB))

    for pattern in (
        "~/snap/code/*/.local/share/opencode/opencode.db",
        "~/snap/opencode/*/.local/share/opencode/opencode.db",
    ):
        for p in sorted(glob.glob(_expand(pattern))):
            _add(p)

    # Dedupe by realpath so the "current" symlink and the real revision
    # don't both appear.
    seen: set[str] = set()
    deduped: List[str] = []
    for p in candidates:
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        deduped.append(p)
    return deduped


def _open_db(db_path: str) -> Optional[sqlite3.Connection]:
    """Open an OpenCode DB read-only, retrying on lock, falling back to copy."""
    for backoff in (0.0, 0.5, 1.0, 2.0):
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000")
            return conn
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc):
                break
            if backoff:
                time.sleep(backoff)
        except Exception:
            break

    try:
        h = hashlib.sha1(db_path.encode()).hexdigest()[:16]
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"ai_r_opencode_{h}_",
            suffix=".db",
        )
        os.close(fd)
        shutil.copy2(db_path, tmp_path)
        os.chmod(tmp_path, 0o600)
        conn = sqlite3.connect(
            tmp_path,
            timeout=30.0,
            factory=_TemporaryCopyConnection,
        )
        conn._ai_r_temp_path = tmp_path
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn
    except Exception:
        if "tmp_path" in locals():
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
        return None


def _iter_dbs(
    base_dir: Optional[str], override: Optional[str]
) -> Iterable[Tuple[str, sqlite3.Connection]]:
    """Yield ``(path, conn)`` for every readable OpenCode DB.

    The caller is responsible for closing the connection.  Connections
    that fail to open are silently skipped (we surface the error in
    :func:`list_sessions` only if no DB at all could be opened).
    """
    for path in _resolve_db_paths(base_dir, override):
        conn = _open_db(path)
        if conn is not None:
            yield path, conn


def _epoch_ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _row_to_session(row: sqlite3.Row, db_path: str) -> Session:
    sid, title, time_created, time_updated, parent_id = (
        row["id"],
        row["title"],
        row["time_created"],
        row["time_updated"],
        row["parent_id"],
    )
    date = _epoch_ms_to_datetime(time_updated or time_created or 0)
    clean_title = (title or "").strip() or "Untitled"
    return Session(
        uuid=sid,
        agent=AgentName.OPENCODE,
        title=clean_title[:_TITLE_MAX_LEN],
        date=date,
        path=db_path,
        message_count=0,  # filled by the caller with a per-row count
        parent_uuid=parent_id,
        extra={
            "time_created": time_created,
            "time_updated": time_updated,
        },
    )


_SELECT_SESSION = (
    "SELECT id, title, time_created, time_updated, parent_id "
    "FROM session "
    "WHERE id = ?"
)
_SELECT_ALL_SESSIONS = (
    "SELECT id, title, time_created, time_updated, parent_id "
    "FROM session "
    "ORDER BY time_updated DESC"
)
_SELECT_MESSAGE_COUNT = "SELECT COUNT(*) FROM message WHERE session_id = ?"


def list_sessions(
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[Session]:
    """Return every OpenCode session, deduplicated across all DBs.

    The same ``session.id`` may exist in more than one DB (e.g. when
    the native and snap installations are kept in sync).  We only
    surface the first occurrence, which comes from the highest-priority
    DB in :func:`_resolve_db_paths`.
    """
    sessions: List[Session] = []
    seen_ids: set[str] = set()
    for db_path, conn in _iter_dbs(base_dir, override):
        try:
            conn.row_factory = sqlite3.Row
            list_cursor = conn.cursor()
            count_cursor = conn.cursor()
            # Materialise the SELECT first: nesting ``execute`` on the
            # same cursor would invalidate the iteration when we look
            # up the per-session message count below.
            rows = list(list_cursor.execute(_SELECT_ALL_SESSIONS))
            for row in rows:
                sid = row["id"]
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                count = count_cursor.execute(
                    _SELECT_MESSAGE_COUNT, (sid,)
                ).fetchone()[0]
                session = _row_to_session(row, db_path)
                session = Session(
                    uuid=session.uuid,
                    agent=session.agent,
                    title=session.title,
                    date=session.date,
                    path=session.path,
                    message_count=int(count),
                    parent_uuid=session.parent_uuid,
                    extra=session.extra,
                )
                sessions.append(session)
        except sqlite3.Error:
            continue
        finally:
            conn.close()

    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _read_session_by_uuid(
    uuid: str,
    base_dir: Optional[str],
    override: Optional[str],
) -> Session:
    if not uuid or not isinstance(uuid, str):
        raise ValueError(f"Invalid OpenCode session uuid: {uuid!r}")
    if any(c.isspace() for c in uuid) or "/" in uuid or "\\" in uuid:
        raise ValueError(f"Invalid OpenCode session uuid: {uuid!r}")

    for db_path, conn in _iter_dbs(base_dir, override):
        try:
            conn.row_factory = sqlite3.Row
            session_cursor = conn.cursor()
            count_cursor = conn.cursor()
            row = session_cursor.execute(_SELECT_SESSION, (uuid,)).fetchone()
            if row is None:
                continue
            count = count_cursor.execute(
                _SELECT_MESSAGE_COUNT, (uuid,)
            ).fetchone()[0]
            session = _row_to_session(row, db_path)
            return Session(
                uuid=session.uuid,
                agent=session.agent,
                title=session.title,
                date=session.date,
                path=session.path,
                message_count=int(count),
                parent_uuid=session.parent_uuid,
                extra=session.extra,
            )
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    raise FileNotFoundError(f"OpenCode session {uuid!r} not found")


def read_session(
    uuid: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> Session:
    """Read a single OpenCode session by ``uuid``.

    Raises:
        FileNotFoundError: no DB contains a session with this id.
        ValueError: ``uuid`` is malformed.
    """
    return _read_session_by_uuid(uuid, base_dir, override)



# Order: message row first (so messages keep DB order), then parts by
# time_created (ties broken by id).  LEFT JOIN so a message with no
# parts still yields a row (with NULL part columns) — robust against
# older DBs / edge cases.
_SELECT_MESSAGES_WITH_PARTS = (
    "SELECT m.id AS mid, m.time_created AS mtime, m.data AS mdata, "
    "p.id AS pid, p.data AS pdata, p.time_created AS ptime "
    "FROM message m "
    "LEFT JOIN part p ON p.message_id = m.id "
    "WHERE m.session_id = ? "
    "ORDER BY m.time_created, m.id, p.time_created, p.id"
)
_SELECT_MESSAGES_ONLY = (
    "SELECT id AS mid, time_created AS mtime, data AS mdata, "
    "NULL AS pid, NULL AS pdata, NULL AS ptime "
    "FROM message WHERE session_id = ? "
    "ORDER BY time_created, id"
)


def _json_or_none(blob: object) -> Optional[dict]:
    if not isinstance(blob, str) or not blob.strip():
        return None
    try:
        rec = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    return rec if isinstance(rec, dict) else None


def _stringify(value: object) -> str:
    """Best-effort serialise a tool input/output value to a string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


_BINARY_PART_KEYS = {"base64", "blob", "bytes", "content", "contents", "data"}


def _compact_part_metadata(value: object, key: str = "") -> object:
    """Return part metadata with inline binary/blob payloads removed."""
    key_l = key.lower()
    if isinstance(value, str):
        if value.startswith("data:"):
            return {"omitted": "data-url"}
        if key_l in _BINARY_PART_KEYS:
            return {"omitted": "binary"}
        return value
    if isinstance(value, dict):
        compact: dict = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                continue
            compact[child_key] = _compact_part_metadata(child_value, child_key)
        return compact
    if isinstance(value, list):
        return [_compact_part_metadata(item, key) for item in value]
    return value


def _part_metadata_input(part: dict) -> str:
    metadata = {
        key: _compact_part_metadata(value, key)
        for key, value in part.items()
        if key != "type"
    }
    return _stringify(metadata)


def _role_from_message_data(message_data: Optional[dict]) -> Optional[str]:
    """Map ``message.data.role`` → our role, or ``None`` if unusable.

    Real OpenCode messages carry ``role`` in ``{"user","assistant"}``.
    We also tolerate ``"tool"`` for forward-compat; unknown roles are
    rejected (returns ``None`` → message skipped).
    """
    if message_data is None:
        return None
    raw = message_data.get("role")
    if not isinstance(raw, str):
        return None
    role = raw.lower()
    if role in ("user", "assistant", "tool"):
        return role
    return None


def _build_message(
    message_data: Optional[dict],
    parts: List[_PartTuple],
    timestamp: Optional[datetime] = None,
) -> Optional[Message]:
    """Assemble a :class:`Message` from metadata + ordered ``(part, ptime)`` tuples.

    * ``text``      = concatenation of ``text`` parts + ``reasoning``
                      parts (reasoning inlined unmarked; kept in-order
                      so the dialogue reads naturally — matches how the
                      Codex/Claude parsers fold thinking into text).
    * ``tool_use``  = one entry per ``tool`` part
                      (``{name: tool, input: state.input, timestamp: ...}``).
                      The per-entry ``timestamp`` is the originating
                      part's ``time_created`` (UTC-aware); ``None`` when
                      the row lacked a part-time column.
    * ``tool_result`` = one entry per ``tool`` part that has a
                      ``state.output`` (``{content: state.output}``).
                      Error/running tools without output are omitted
                      from results.
    * ``file``/``patch`` = metadata-only ``tool_use`` entries. Inline
                      data URLs / binary-looking payload fields are
                      redacted, so manifests and patch summaries remain
                      visible without blobs.
    * ``step-start``/``step-finish`` — boundary markers: skipped
                      (no text leak, no crash).
    """
    role = _role_from_message_data(message_data)
    if role is None:
        return None

    text_chunks: List[str] = []
    tool_use: List[dict] = []
    tool_result: List[dict] = []

    # Fallback: legacy DBs that stored content inside message.data.
    if message_data is not None and not parts:
        content = message_data.get("content")
        if isinstance(content, str) and content:
            text_chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type", "")
                t = part.get("text", "")
                if ptype in ("text", "input_text", "output_text", "") and isinstance(t, str) and t:
                    text_chunks.append(t)

    for part, ptime in parts:
        ptype = part.get("type", "")
        ts_for_entry: Optional[datetime] = (
            _epoch_ms_to_datetime(ptime) if isinstance(ptime, int) else None
        )
        if ptype in ("text", "reasoning"):
            t = part.get("text", "")
            if isinstance(t, str) and t:
                text_chunks.append(t)
        elif ptype == "tool":
            name = part.get("tool") or part.get("toolName") or part.get("name") or ""
            state_raw = part.get("state")
            state: dict = state_raw if isinstance(state_raw, dict) else {}
            inp = state.get("input")
            tool_use.append(
                {"name": name, "input": _stringify(inp), "timestamp": ts_for_entry}
            )
            output = state.get("output")
            if output is not None:
                tool_result.append({"content": _stringify(output)})
        elif ptype in ("file", "patch"):
            tool_use.append(
                {"name": ptype, "input": _part_metadata_input(part), "timestamp": ts_for_entry}
            )
        # step-start / step-finish / unknown → skip

    return Message(
        role=role,
        text="\n".join(text_chunks),
        tool_use=tuple(tool_use),
        tool_result=tuple(tool_result),
        timestamp=timestamp,
    )


def _extract_messages_from_db(db_path: str, uuid: str) -> List[Message]:
    """Read all messages for ``uuid`` from an OpenCode SQLite DB.

    Joins ``message`` with ``part`` (text/tool/reasoning bodies live in
    ``part``).  Falls back to a metadata-only scan if the ``part`` table
    is absent (older DBs).  Never raises on missing/malformed parts —
    those messages degrade to empty text.
    """
    messages: List[Message] = []
    conn = _open_db(db_path)
    if conn is None:
        return messages
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='part'"
            )
            has_parts = cursor.fetchone() is not None
        except sqlite3.Error:
            has_parts = False

        sql = _SELECT_MESSAGES_WITH_PARTS if has_parts else _SELECT_MESSAGES_ONLY
        rows = cursor.execute(sql, (uuid,)).fetchall()
    except sqlite3.Error:
        conn.close()
        return messages
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    # Group consecutive rows by mid (ORDER BY keeps a message's rows
    # contiguous).  Each row carries one part (or NULL pdata for a
    # part-less message under LEFT JOIN).
    current_mid: Optional[str] = None
    current_data: Optional[dict] = None
    current_parts: List[_PartTuple] = []
    current_mtime: Optional[int] = None
    current_min_ptime: Optional[int] = None

    def flush() -> None:
        nonlocal current_mid, current_data, current_parts
        nonlocal current_mtime, current_min_ptime
        if current_mid is None:
            return
        ts: Optional[datetime] = None
        if current_min_ptime is not None:
            ts = _epoch_ms_to_datetime(current_min_ptime)
        elif current_mtime is not None:
            ts = _epoch_ms_to_datetime(current_mtime)
        msg = _build_message(current_data, current_parts, timestamp=ts)
        if msg is not None:
            messages.append(msg)
        current_mid = None
        current_data = None
        current_parts = []
        current_mtime = None
        current_min_ptime = None

    for row in rows:
        mid = row["mid"]
        if mid != current_mid:
            flush()
            current_mid = mid
            current_data = _json_or_none(row["mdata"])
            current_parts = []
            current_mtime = row["mtime"]
            current_min_ptime = None
        pdata_blob = row["pdata"] if "pdata" in row.keys() else None
        part = _json_or_none(pdata_blob)
        ptime = row["ptime"] if "ptime" in row.keys() else None
        if isinstance(ptime, int) and (
            current_min_ptime is None or ptime < current_min_ptime
        ):
            current_min_ptime = ptime
        if part is not None:
            current_parts.append((part, ptime if isinstance(ptime, int) else None))
    flush()
    return messages


def read_messages(
    uuid: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[Message]:
    """Return the full message list for an OpenCode session.

    Reuses :func:`read_session` for path resolution (which also validates
    the uuid).  Reads ``message`` rows joined to their ``part`` rows —
    real message text/tools live in the ``part`` table, while
    ``message.data`` only carries metadata (role/time/agent/model).
    Falls back to metadata-only when the ``part`` table is absent.

    Raises:
        FileNotFoundError: no DB contains a session with this id.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir, override)
    return _extract_messages_from_db(session.path, session.uuid)


def search(
    query: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[Session]:
    """Case-insensitive substring search across OpenCode session titles."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [
        session
        for session in list_sessions(base_dir, override)
        if needle in session.title.lower()
    ]


def session_exists(
    uuid: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> bool:
    if not uuid or not isinstance(uuid, str):
        return False
    if any(c.isspace() for c in uuid) or "/" in uuid or "\\" in uuid:
        return False
    for db_path, conn in _iter_dbs(base_dir, override):
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            row = cursor.execute(_SELECT_SESSION, (uuid,)).fetchone()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        if row is not None:
            return True
    return False
