"""Pi coding agent session parser.

Source layout::

    ~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<session-id>.jsonl

Older Pi versions briefly stored ``*.jsonl`` files directly under
``~/.pi/agent``; because discovery is recursive, those legacy files are
also accepted when the parser root points at ``~/.pi/agent``.

Each line is a JSON object with one of these relevant ``type`` values:

* ``"session"``      — header with canonical ``id``, ``timestamp``,
  ``cwd`` and optional ``parentSession``.
* ``"session_info"`` — optional human-readable session name; the latest
  non-empty name becomes the title.
* ``"message"``      — payload under ``message``.  ``user`` and
  ``assistant`` records count as conversation messages; ``toolResult``
  and custom roles are skipped for summary counts.

The base directory can be overridden by ``base_dir`` or by setting
``$AI_R_HOME/.pi/agent/sessions``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ._common import _is_valid_uuid, _normalise_title, iter_jsonl_records
from .models import AgentName, Message, Session


_TITLE_MAX_LEN = 100


def _resolve_base_dir(base_dir: Optional[str]) -> Path:
    if base_dir:
        return Path(base_dir).expanduser()
    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        return Path(env_home).expanduser() / ".pi" / "agent" / "sessions"
    return Path("~/.pi/agent/sessions").expanduser()


def source_roots(base_dir: Optional[str] = None) -> List[str]:
    """Candidate source root(s) for Pi sessions (may not exist).

    Used by :mod:`ai_r.diagnostics` to explain empty results.
    """
    return [str(_resolve_base_dir(base_dir))]


def _parse_iso_timestamp(raw: object) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, always returning a tz-aware datetime.

    Pi emits ISO-Z strings (``...Z``), but defensively accept bare
   /truncated forms too. A naive result would mix with tz-aware entries
    in ``list_sessions`` and break the sort, so every result is pinned
    to UTC.
    """
    if not isinstance(raw, str) or not raw:
        return None
    for candidate in (raw, raw[:23]):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _parse_epoch_millis(raw: object) -> Optional[datetime]:
    if not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _extract_text(content: object, *, include_thinking: bool = False) -> str:
    """Return text from Pi message content.

    Pi content is a string or an array of typed blocks.  Real Pi sessions
    emit ``text`` (user/assistant/toolResult), ``thinking`` (assistant
    reasoning), and ``toolCall`` blocks.  For normal dialogue summaries we
    include only ``text`` blocks; ``thinking`` is included only when
    ``include_thinking`` is set, and ``toolCall`` is always skipped.

    A couple of Codex/OpenAI-style block types (``input_text``,
    ``output_text``) are also accepted for forward-compatibility, though
    upstream Pi does not currently emit them.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    chunks: List[str] = []
    accepted = {"text", "input_text", "output_text", ""}
    if include_thinking:
        accepted.add("thinking")
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if part_type not in accepted:
            continue
        text = part.get("text") or part.get("thinking")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


def _entry_timestamp(entry: dict[str, Any]) -> Optional[datetime]:
    message = entry.get("message")
    if isinstance(message, dict):
        ts = _parse_epoch_millis(message.get("timestamp"))
        if ts is not None:
            return ts
    return _parse_iso_timestamp(entry.get("timestamp"))


def _scan_file(jsonl_path: Path) -> Optional[Session]:
    uuid: Optional[str] = None
    cwd: Optional[str] = None
    parent_session: Optional[str] = None
    header_timestamp: Optional[datetime] = None
    last_timestamp: Optional[datetime] = None
    first_user_text: Optional[str] = None
    session_name: Optional[str] = None
    message_count = 0

    for entry in iter_jsonl_records(jsonl_path):
        rec_type = entry.get("type")
        if rec_type == "session":
            if uuid is None and isinstance(entry.get("id"), str):
                uuid = entry["id"]
            if isinstance(entry.get("cwd"), str):
                cwd = entry["cwd"]
            if isinstance(entry.get("parentSession"), str):
                parent_session = entry["parentSession"]
            ts = _parse_iso_timestamp(entry.get("timestamp"))
            if ts is not None:
                header_timestamp = ts
                last_timestamp = ts
            continue

        ts = _entry_timestamp(entry)
        if ts is not None:
            last_timestamp = ts

        if rec_type == "session_info":
            name = entry.get("name")
            if isinstance(name, str):
                session_name = name.strip() or None
            continue

        if rec_type != "message":
            continue
        message = entry.get("message") or {}
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        message_count += 1
        if role == "user" and first_user_text is None:
            text = _extract_text(message.get("content", "")).strip()
            if text and not text.lstrip().startswith("<"):
                first_user_text = text.splitlines()[0].strip()

    if uuid is None:
        return None

    if session_name:
        title = _normalise_title(session_name)
    elif first_user_text:
        title = _normalise_title(first_user_text)
    elif cwd:
        title = _normalise_title(cwd)
    else:
        title = "Untitled"

    timestamp = last_timestamp or header_timestamp
    if timestamp is None:
        try:
            timestamp = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    extra: dict[str, Any] = {}
    if cwd:
        extra["cwd"] = cwd
    if parent_session:
        # Kept in ``extra`` for backward compatibility; the first-class
        # fields below are the canonical surface.
        extra["parent_session"] = parent_session

    return Session(
        uuid=uuid,
        agent=AgentName.PI,
        title=title,
        date=timestamp,
        path=str(jsonl_path),
        message_count=message_count,
        parent_uuid=parent_session,
        kind="subagent" if parent_session else "agent",
        # The session-header ``cwd`` is the project directory.  Pi has no
        # launch-surface signal (header carries only type/version/id/
        # timestamp/cwd) → launch_surface stays None, never fabricated.
        project_dir=cwd,
        extra=extra,
    )


def _discover_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    files = [p for p in root.glob("**/*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def list_sessions(base_dir: Optional[str] = None) -> List[Session]:
    root = _resolve_base_dir(base_dir)
    sessions: List[Session] = []
    seen_uuids: set[str] = set()
    for path in _discover_files(root):
        session = _scan_file(path)
        if session is None or session.uuid in seen_uuids:
            continue
        seen_uuids.add(session.uuid)
        sessions.append(session)
    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _find_session_file(uuid: str, base_dir: Optional[str]) -> Tuple[Path, Session]:
    if not _is_valid_uuid(uuid):
        raise ValueError(f"Invalid Pi session uuid: {uuid!r}")
    root = _resolve_base_dir(base_dir)
    for path in _discover_files(root):
        session = _scan_file(path)
        if session is not None and session.uuid == uuid:
            return path, session
    raise FileNotFoundError(f"Pi session {uuid!r} not found under {root}")


def read_session(uuid: str, base_dir: Optional[str] = None) -> Session:
    _, session = _find_session_file(uuid, base_dir)
    return session



# NOTE (interactive question→answer pairs): the Pi session format has NO
# native interactive-question tool — its only ``toolCall`` names observed
# in real sessions are ordinary tools (bash/read/edit/write/...), with no
# AskUserQuestion / request_user_input / question equivalent.  Pi therefore
# never populates :attr:`Message.qa`.  Recheck if Pi adds such a tool.
def _pi_extract_message(
    message: dict, timestamp: Optional[datetime] = None
) -> Optional[Message]:
    """Convert a Pi ``message`` payload into a :class:`Message`.

    Returns ``None`` for roles we do not surface (``toolResult`` records
    with no usable content are still emitted as ``tool`` messages so the
    audit trail is complete).  ``toolCall`` blocks become ``tool_use``
    entries; ``thinking`` blocks are skipped from ``text``.
    """
    role = message.get("role")
    content = message.get("content", "")
    text_chunks: List[str] = []
    tool_use: List[dict] = []
    if isinstance(content, str):
        text_chunks.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type in ("text", "input_text", "output_text", ""):
                text = part.get("text", "")
                if isinstance(text, str) and text:
                    text_chunks.append(text)
            elif part_type == "toolCall":
                name = part.get("name", "")
                args = part.get("arguments", part.get("input", ""))
                if isinstance(args, str):
                    input_str = args
                else:
                    try:
                        input_str = json.dumps(args, ensure_ascii=False)
                    except (TypeError, ValueError):
                        input_str = str(args)
                tool_use.append({"name": name, "input": input_str})
            # ``thinking`` blocks are intentionally skipped here.
    if role in ("user", "assistant"):
        return Message(
            role=role,
            text="\n".join(text_chunks),
            tool_use=tuple(tool_use),
            timestamp=timestamp,
        )
    if role == "toolResult":
        result_text = "\n".join(text_chunks)
        return Message(
            role="tool",
            text="",
            # Pi ``toolResult`` records carry no error flag, so ``is_error``
            # is best-effort and defaults False (absent signal).
            tool_result=({"content": result_text, "is_error": False},),
            timestamp=timestamp,
        )
    return None


def _extract_messages_from_jsonl(path: Path) -> List[Message]:
    """Read a Pi JSONL session into structured :class:`Message` objects.

    Only ``message`` records with role ``user``, ``assistant`` or
    ``toolResult`` are surfaced; other record types (``session``,
    ``session_info``, ``model_change``) are skipped.  Lines that fail to
    parse as JSON are silently skipped; an :class:`OSError` returns
    whatever was collected so far.
    """
    messages: List[Message] = []
    for record in iter_jsonl_records(path):
        if record.get("type") != "message":
            continue
        message = record.get("message") or {}
        if not isinstance(message, dict):
            continue
        ts = _entry_timestamp(record)
        parsed = _pi_extract_message(message, timestamp=ts)
        if parsed is not None:
            messages.append(parsed)
    return messages


def read_messages(
    uuid: str, base_dir: Optional[str] = None
) -> List[Message]:
    """Return the full message list for a Pi session.

    Reuses :func:`read_session` for path resolution.  ``toolCall``
    blocks on assistant messages and ``toolResult`` records are
    preserved on the returned :class:`Message` objects.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir)
    return _extract_messages_from_jsonl(Path(session.path))


def search(query: str, base_dir: Optional[str] = None) -> List[Session]:
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [session for session in list_sessions(base_dir) if needle in session.title.lower()]


def session_exists(uuid: str, base_dir: Optional[str] = None) -> bool:
    if not _is_valid_uuid(uuid):
        return False
    try:
        _find_session_file(uuid, base_dir)
    except (FileNotFoundError, ValueError):
        return False
    return True
