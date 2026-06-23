"""Claude Code session parser.

Source layout::

    ~/.claude/projects/<project-slug>/<session-uuid>.jsonl

Each line is a JSON object with the following relevant keys:

* ``type``         — ``"user"``, ``"assistant"``, ``"custom-title"``,
  ``"ai-title"`` or other event types (``"queue-operation"``, …).
* ``timestamp``    — ISO 8601 string (last record wins for the date).
* ``message``      — for ``user``/``assistant`` records only.  Either a
  string or a list of parts, where each part has ``type`` (``"text"``,
  ``"tool_use"``, ``"tool_result"``) and a ``text`` / ``content`` field.
* ``aiTitle``      — for ``"ai-title"`` records, optional auto-generated
  title.
* ``customTitle``  — for ``"custom-title"`` records, optional
  user-supplied title (highest priority).

Title resolution order used by :func:`extract_title` and
:func:`_scan_file` is:

1. ``custom-title`` event value.
2. ``ai-title`` event value.
3. First user message text (first line, stripped, max 100 chars).
4. ``chat-HHMM`` derived from the JSONL file mtime, falling back to
   ``"Untitled"``.

The base directory can be overridden for tests by passing ``base_dir``
explicitly to the module-level functions.  When unset, the directory
is read from the ``AI_R_HOME`` environment variable (used as
``$AI_R_HOME/.claude/projects``), falling back to
``~/.claude/projects``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .models import AgentName, Message, Session


_TITLE_MAX_LEN = 100


def _resolve_base_dir(base_dir: Optional[str]) -> Path:
    """Return the Claude projects directory.

    Lookup order:

    1. Explicit ``base_dir`` argument.
    2. ``$AI_R_HOME/.claude/projects``.
    3. ``~/.claude/projects``.
    """
    if base_dir:
        return Path(base_dir).expanduser()
    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        return Path(env_home).expanduser() / ".claude" / "projects"
    return Path("~/.claude/projects").expanduser()


def _parse_iso_timestamp(raw: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp, tolerating a trailing ``Z``."""
    if not raw:
        return None
    try:
        cleaned = raw[:23]
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _extract_text_from_user_message(message: dict) -> str:
    """Return the first plain-text part of a user message, or empty string."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text", "")
            if not isinstance(text, str):
                continue
            text = text.strip()
            if not text or text.startswith("<"):
                continue
            return text
    return ""


def _normalise_title(raw: str) -> str:
    """Collapse newlines and truncate to ``_TITLE_MAX_LEN`` chars."""
    if not raw:
        return "Untitled"
    return raw.replace("\n", " ").replace("\r", " ").strip()[:_TITLE_MAX_LEN]


def _scan_titles_from_jsonl(
    jsonl_path: Path,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(custom_title, ai_title)`` from a Claude JSONL file."""
    custom_title: Optional[str] = None
    ai_title: Optional[str] = None
    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                rec_type = record.get("type")
                if rec_type == "custom-title" and custom_title is None:
                    raw = record.get("customTitle", "")
                    if isinstance(raw, str) and raw.strip():
                        custom_title = raw.strip()
                elif rec_type == "ai-title" and ai_title is None:
                    raw = record.get("aiTitle", "")
                    if isinstance(raw, str) and raw.strip():
                        ai_title = raw.strip()
    except OSError:
        pass
    return custom_title, ai_title


def _first_user_text_from_messages(messages: List[Message]) -> Optional[str]:
    """Return the first non-empty user message text, or ``None``."""
    for msg in messages:
        if msg.role == "user" and msg.text.strip():
            return msg.text
    return None


def _resolve_title(
    custom_title: Optional[str],
    ai_title: Optional[str],
    first_user_text: Optional[str],
    jsonl_path: Optional[Path],
) -> Optional[str]:
    """Pick a session title from the available signals.

    Returns ``None`` when no signal yields a usable title.
    """
    if custom_title:
        return _normalise_title(custom_title)
    if ai_title:
        return _normalise_title(ai_title)
    if first_user_text:
        return _normalise_title(first_user_text)
    if jsonl_path is not None:
        try:
            ts = datetime.fromtimestamp(jsonl_path.stat().st_mtime)
            return _normalise_title(f"chat-{ts.strftime('%H%M')}")
        except OSError:
            pass
    return None


def extract_title(
    messages: List[Message], jsonl_path: Optional[Path] = None
) -> str:
    """Resolve a Claude session title from jsonl events and message content.

    Priority:

    1. ``custom-title`` event with a non-empty string value (only when
       ``jsonl_path`` is provided).
    2. ``ai-title`` event with a non-empty string value (only when
       ``jsonl_path`` is provided).
    3. First user message in ``messages`` — first line, stripped, max
       100 characters.
    4. ``chat-HHMM`` derived from the ``jsonl_path`` mtime, falling
       back to ``"Untitled"``.
    """
    if jsonl_path is not None:
        custom_title, ai_title = _scan_titles_from_jsonl(jsonl_path)
    else:
        custom_title, ai_title = None, None
    first_user_text = _first_user_text_from_messages(messages)
    title = _resolve_title(custom_title, ai_title, first_user_text, jsonl_path)
    return title if title is not None else "Untitled"


def _scan_file(jsonl_path: Path) -> Optional[Session]:
    """Build a :class:`Session` from one Claude JSONL file.

    Returns ``None`` if the file yields no usable title/timestamp.
    """
    custom_title: Optional[str] = None
    ai_title: Optional[str] = None
    first_user_text: Optional[str] = None
    last_timestamp: Optional[datetime] = None
    message_count = 0

    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                ts = _parse_iso_timestamp(record.get("timestamp", ""))
                if ts is not None:
                    last_timestamp = ts

                rec_type = record.get("type")
                if rec_type == "custom-title" and custom_title is None:
                    raw = record.get("customTitle", "")
                    if isinstance(raw, str) and raw.strip():
                        custom_title = raw.strip()
                elif rec_type == "ai-title" and ai_title is None:
                    raw = record.get("aiTitle", "")
                    if isinstance(raw, str) and raw.strip():
                        ai_title = raw.strip()
                elif rec_type == "user":
                    message_count += 1
                    text = _extract_text_from_user_message(
                        record.get("message", {}) or {}
                    )
                    if (
                        text
                        and not text.startswith("<")
                        and first_user_text is None
                    ):
                        first_user_text = text
                elif rec_type == "assistant":
                    message_count += 1
    except OSError:
        return None

    title = _resolve_title(custom_title, ai_title, first_user_text, jsonl_path)
    if title is None:
        return None

    if last_timestamp is None:
        try:
            last_timestamp = datetime.fromtimestamp(
                jsonl_path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return None

    project_slug = jsonl_path.parent.name
    return Session(
        uuid=jsonl_path.stem,
        agent=AgentName.CLAUDE,
        title=title,
        date=last_timestamp,
        path=str(jsonl_path),
        message_count=message_count,
        extra={"project_slug": project_slug},
    )


def list_sessions(base_dir: Optional[str] = None) -> List[Session]:
    """Return every Claude session visible under ``base_dir``.

    Sessions are sorted by date (most recent first).  Files that fail
    to parse are silently skipped — Claude JSONL records are noisy
    and one bad line should not break enumeration.
    """
    root = _resolve_base_dir(base_dir)
    if not root.is_dir():
        return []

    sessions: List[Session] = []
    for jsonl_path in root.glob("*/*.jsonl"):
        if not jsonl_path.is_file():
            continue
        session = _scan_file(jsonl_path)
        if session is not None:
            sessions.append(session)

    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _find_session_file(uuid: str, base_dir: Optional[str]) -> Path:
    """Locate the JSONL for ``uuid`` and validate the identifier.

    Raises:
        ValueError: ``uuid`` contains path separators or whitespace.
        FileNotFoundError: no file with this name exists under
            ``base_dir`` (or it is not a regular file).
    """
    if not uuid or "/" in uuid or "\\" in uuid or ".." in uuid:
        raise ValueError(f"Invalid Claude session uuid: {uuid!r}")
    if uuid != uuid.strip() or any(c.isspace() for c in uuid):
        raise ValueError(f"Invalid Claude session uuid: {uuid!r}")

    root = _resolve_base_dir(base_dir)
    for jsonl_path in root.glob(f"*/{uuid}.jsonl"):
        if jsonl_path.is_file():
            return jsonl_path

    raise FileNotFoundError(
        f"Claude session {uuid!r} not found under {root}"
    )


def read_session(uuid: str, base_dir: Optional[str] = None) -> Session:
    """Read and return a single Claude session by ``uuid``.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    path = _find_session_file(uuid, base_dir)
    session = _scan_file(path)
    if session is None:
        raise FileNotFoundError(
            f"Claude session {uuid!r} at {path} yielded no parseable data"
        )
    return session


def _parse_jsonl_line(line: str) -> Optional[Message]:
    """Parse one Claude JSONL line into a :class:`Message`, or skip it.

    Returns ``None`` for blank lines, malformed JSON, non-dict records,
    and records whose ``type`` is not ``"user"`` or ``"assistant"``.
    Assistant records yield ``text`` (from ``text`` blocks) and
    ``tool_use`` entries (from ``tool_use`` blocks).  User records yield
    ``text`` plus ``tool_result`` entries for any ``tool_result`` blocks
    they carry (Claude embeds tool results in user-role records).
    """
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    rec_type = record.get("type")
    if rec_type not in ("user", "assistant"):
        return None
    payload = record.get("message") or {}
    ts = _parse_iso_timestamp(record.get("timestamp", ""))
    if not isinstance(payload, dict):
        return None
    content = payload.get("content", "")
    text_chunks: List[str] = []
    tool_use: List[dict] = []
    tool_result: List[dict] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text", "")
                if isinstance(text, str) and text:
                    text_chunks.append(text)
            elif part_type == "tool_use":
                name = part.get("name", "")
                raw_input = part.get("input", "")
                if isinstance(raw_input, str):
                    input_str = raw_input
                else:
                    try:
                        input_str = json.dumps(
                            raw_input, ensure_ascii=False
                        )
                    except (TypeError, ValueError):
                        input_str = str(raw_input)
                tool_use.append({"name": name, "input": input_str})
            elif part_type == "tool_result":
                result_content = part.get("content", "")
                if isinstance(result_content, list):
                    pieces: List[str] = []
                    for piece in result_content:
                        if isinstance(piece, dict):
                            t = piece.get("text", "")
                            if isinstance(t, str) and t:
                                pieces.append(t)
                    result_str = "\n".join(pieces)
                elif isinstance(result_content, str):
                    result_str = result_content
                else:
                    result_str = ""
                tool_result.append({"content": result_str})
    elif isinstance(content, str):
        text_chunks.append(content)
    return Message(
        role=rec_type,
        text="\n".join(text_chunks),
        tool_use=tuple(tool_use),
        tool_result=tuple(tool_result),
        timestamp=ts,
    )


def _extract_messages_from_jsonl(path: Path) -> List[Message]:
    """Read a Claude JSONL file into structured :class:`Message` objects.

    Lines that are not valid JSON or not ``user``/``assistant`` records
    are silently skipped.  An :class:`OSError` reading the file returns
    whatever was collected so far.
    """
    messages: List[Message] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                msg = _parse_jsonl_line(line)
                if msg is not None:
                    messages.append(msg)
    except OSError:
        return messages
    return messages


def read_messages(
    uuid: str, base_dir: Optional[str] = None
) -> List[Message]:
    """Return the full message list for a Claude session.

    Reuses :func:`read_session` for path resolution.  Tool calls and
    tool results are preserved on the returned :class:`Message` objects.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir)
    return _extract_messages_from_jsonl(Path(session.path))


def get_session_size(uuid: str, base_dir: Optional[str] = None) -> int:
    """Return the on-disk byte size of the JSONL file backing ``uuid``.

    Useful for incremental readers: a caller that knows the
    ``new_offset`` returned by :func:`read_session_incremental` can poll
    this to decide whether the agent has appended more data.  Returns
    ``0`` if the file's size cannot be determined.
    """
    path = _find_session_file(uuid, base_dir)
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_session_incremental(
    uuid: str,
    from_offset: int = 0,
    base_dir: Optional[str] = None,
) -> Tuple[List[Message], int]:
    """Read Claude-session messages from ``from_offset`` to end of file.

    Opens the JSONL file in binary mode, seeks to ``from_offset``, and
    parses every line that follows.  Returns ``(messages, new_offset)``
    where ``new_offset`` is the byte position immediately after the last
    byte read — pass it back in on the next call to fetch only the
    delta.

    An :class:`OSError` while reading returns whatever messages were
    collected up to the failure point along with the current offset.
    ``FileNotFoundError`` from path resolution still propagates.
    """
    path = _find_session_file(uuid, base_dir)
    messages: List[Message] = []
    new_offset = max(from_offset, 0)
    try:
        with path.open("rb") as fh:
            fh.seek(new_offset)
            for raw_line in fh:
                msg = _parse_jsonl_line(
                    raw_line.decode("utf-8", errors="replace")
                )
                if msg is not None:
                    messages.append(msg)
            new_offset = fh.tell()
    except OSError:
        return messages, new_offset
    return messages, new_offset


def search(query: str, base_dir: Optional[str] = None) -> List[Session]:
    """Case-insensitive substring search across Claude session titles."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [
        session
        for session in list_sessions(base_dir)
        if needle in session.title.lower()
    ]


def session_exists(uuid: str, base_dir: Optional[str] = None) -> bool:
    """Return ``True`` if a Claude session with this uuid is on disk."""
    if not uuid or "/" in uuid or "\\" in uuid or ".." in uuid:
        return False
    try:
        _find_session_file(uuid, base_dir)
    except (FileNotFoundError, ValueError):
        return False
    return True
