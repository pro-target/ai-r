"""Antigravity session parser.

Antigravity stores its "brain" (per-session scratchpad) in two
locations that we know about, both with the same internal layout::

    ~/.gemini/antigravity/brain/<session-uuid>/...
    ~/.gemini/antigravity-cli/brain/<session-uuid>/...

A brain directory contains:

* ``.system_generated/logs/overview.txt``  — JSONL of meta events.
  The first ``USER_EXPLICIT``/``USER_INPUT`` record whose content
  contains ``<USER_REQUEST>...</USER_REQUEST>`` becomes the title.
* ``.system_generated/logs/transcript.json`` or
  ``.system_generated/logs/transcript_full.jsonl``  — full transcript
  (newer variants).
* ``walkthrough.md`` / ``task.md`` / ``task.md.resolved`` /
  ``implementation_plan.md``  — markdown plan with a ``# Title``
  heading.

The directory is considered a session if any of these files exist;
missing files degrade gracefully (we just report zero messages).

The base directory can be overridden by ``base_dir`` (which then names
a single brain directory) or by setting ``$AI_R_HOME/.gemini``
and ``$AI_R_HOME/.gemini-cli``.  When unset we look at
``~/.gemini/antigravity/brain`` and ``~/.gemini/antigravity-cli/brain``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ._common import _is_valid_uuid, _normalise_title, _parse_iso_timestamp
from .models import AgentName, Message, Session


_TITLE_MAX_LEN = 100
_USER_REQUEST_RE = re.compile(
    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL
)
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_MARKDOWN_CANDIDATES: tuple[str, ...] = (
    "implementation_plan.md",
    "walkthrough.md",
    "task.md.resolved",
    "task.md",
)
_TRANSCRIPT_CANDIDATES: tuple[str, ...] = (
    ".system_generated/logs/transcript.json",
    ".system_generated/logs/transcript_full.jsonl",
    ".system_generated/logs/transcript.jsonl",
)
_OVERVIEW = ".system_generated/logs/overview.txt"

_TOOL_PART_TYPES: frozenset[str] = frozenset({
    "MODEL_TOOL_CALL",
    "TOOL_CALL",
    "tool_call",
    "toolCall",
    "tool_use",
    "TOOL_USE",
})


def _resolve_brain_roots(
    base_dir: Optional[str] = None,
) -> List[Path]:
    """Return the list of brain roots to scan, in priority order.

    When ``base_dir`` is supplied, it is treated as a single brain
    directory and returned as-is.  Otherwise we look at the two known
    installations under ``$AI_R_HOME/.gemini`` (if set) or
    ``~/.gemini``.
    """
    if base_dir:
        return [Path(base_dir).expanduser()]

    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        root = Path(env_home).expanduser() / ".gemini"
    else:
        root = Path("~/.gemini").expanduser()

    roots = [
        root / "antigravity-cli" / "brain",
        root / "antigravity" / "brain",
    ]
    return [r for r in roots if r.is_dir()]


def _extract_title_from_overview(overview_path: Path) -> tuple[str, int, Optional[str]]:
    """Return ``(title, message_count, latest_timestamp_iso)`` from overview.txt."""
    title = ""
    count = 0
    latest_ts: Optional[str] = None
    try:
        with overview_path.open("r", encoding="utf-8") as fh:
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
                count += 1
                ts = record.get("timestamp")
                if isinstance(ts, str):
                    latest_ts = ts
                if not title and record.get("source") == "USER_EXPLICIT" \
                        and record.get("type") == "USER_INPUT":
                    raw_content = record.get("content", "")
                    if isinstance(raw_content, str) and raw_content:
                        match = _USER_REQUEST_RE.search(raw_content)
                        if match:
                            title = match.group(1)
                        else:
                            title = raw_content
    except OSError:
        return "", 0, None
    return title, count, latest_ts


def _extract_title_from_markdown(brain: Path) -> str:
    """Return the first ``# Heading`` found in any of the markdown plans."""
    for name in _MARKDOWN_CANDIDATES:
        path = brain / name
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = fh.read(_TITLE_MAX_LEN * 4)
        except OSError:
            continue
        match = _HEADING_RE.search(data)
        if match:
            return match.group(1)
    return ""


def _scan_brain(brain: Path) -> Optional[Session]:
    """Build a :class:`Session` from one brain directory."""
    if not brain.is_dir():
        return None

    title = ""
    count = 0
    latest_ts: Optional[str] = None

    overview = brain / _OVERVIEW
    if overview.is_file():
        title, count, latest_ts = _extract_title_from_overview(overview)

    if not title:
        title = _extract_title_from_markdown(brain)

    # Newer variants use transcript.json / transcript_full.jsonl instead
    # of overview.txt.  Try them as a fallback for both title and count.
    if not title or count == 0:
        for candidate in _TRANSCRIPT_CANDIDATES:
            tpath = brain / candidate
            if not tpath.is_file():
                continue
            try:
                with tpath.open("r", encoding="utf-8") as fh:
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
                        count += 1
                        ts = record.get("timestamp")
                        if isinstance(ts, str):
                            latest_ts = ts
                        if not title and record.get("source") == "USER_EXPLICIT" \
                                and record.get("type") == "USER_INPUT":
                            raw_content = record.get("content", "")
                            if isinstance(raw_content, str) and raw_content:
                                match = _USER_REQUEST_RE.search(raw_content)
                                if match:
                                    title = match.group(1)
                                else:
                                    title = raw_content
            except OSError:
                continue
            break  # first existing transcript wins

    if not title:
        # No titleable content and no transcript — treat the brain as
        # empty rather than fabricating a record.
        if count == 0:
            return None
        title = "Untitled"

    timestamp = _parse_iso_timestamp(latest_ts or "") if latest_ts else None
    if timestamp is None:
        try:
            timestamp = datetime.fromtimestamp(
                brain.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return None

    return Session(
        uuid=brain.name,
        agent=AgentName.ANTIGRAVITY,
        title=_normalise_title(title),
        date=timestamp,
        path=str(brain),
        message_count=count,
    )


def list_sessions(base_dir: Optional[str] = None) -> List[Session]:
    """Return every Antigravity brain directory under the search roots."""
    sessions: List[Session] = []
    for root in _resolve_brain_roots(base_dir):
        for brain in sorted(root.iterdir(), key=lambda p: p.name):
            if not brain.is_dir():
                continue
            session = _scan_brain(brain)
            if session is not None:
                sessions.append(session)
    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _find_brain(uuid: str, base_dir: Optional[str]) -> Path:
    if not _is_valid_uuid(uuid):
        raise ValueError(f"Invalid Antigravity session uuid: {uuid!r}")
    for root in _resolve_brain_roots(base_dir):
        candidate = root / uuid
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"Antigravity session {uuid!r} not found")


def read_session(uuid: str, base_dir: Optional[str] = None) -> Session:
    """Read a single Antigravity session by ``uuid`` (brain directory name)."""
    brain = _find_brain(uuid, base_dir)
    session = _scan_brain(brain)
    if session is None:
        raise FileNotFoundError(
            f"Antigravity session {uuid!r} at {brain} yielded no parseable data"
        )
    return session



# NOTE (interactive question→answer pairs): the parsed Antigravity surface
# (overview.txt / transcript*.jsonl) has NO structured interactive-question
# record — observed record ``type`` values are PLANNER_RESPONSE / VIEW_FILE /
# GREP_SEARCH / USER_INPUT / ... with no AskUserQuestion-style call+answer.
# (Antigravity's planning UI may emit such prompts, but only as free-text
# inside model reasoning, not as a parseable question→answer pair.)
# Antigravity therefore never populates :attr:`Message.qa`.  Recheck if a
# structured ask-question record type appears.
def _antigravity_message_from_record(record: dict) -> Optional[Message]:
    """Map an Antigravity transcript record to a :class:`Message`.

    Antigravity transcript records carry ``source`` (``USER_EXPLICIT``,
    ``MODEL``, …) and ``type`` (``USER_INPUT``, ``MODEL_OUTPUT``, …)
    fields rather than an explicit ``role``.  We map ``USER_*`` records
    to ``user`` and ``MODEL_*`` records to ``assistant``.  Text comes
    from the ``content`` field; tool structure is best-effort because
    Antigravity transcripts are heterogeneous.

    Tool calls may appear in three shapes (all normalised to
    ``tool_use`` entries):

    1. ``content`` is a list of parts, one of which has
       ``type`` in :data:`_TOOL_PART_TYPES` — the part carries
       ``name`` + ``args`` (or ``input``/``arguments``).
    2. ``content`` is a single dict whose ``type`` is a tool-part
       type (e.g. raw ``MODEL_TOOL_CALL`` records).
    3. The record-level ``type`` is itself a tool-part type — the
       tool spec lives in ``record["name"]``/``record["args"]``.

    Returns ``None`` when the record cannot be classified.
    """
    source = record.get("source", "")
    rtype = record.get("type", "")
    if isinstance(source, str) and source.startswith("USER"):
        role = "user"
    elif isinstance(source, str) and source.startswith("MODEL"):
        role = "assistant"
    elif isinstance(rtype, str) and rtype.startswith("USER"):
        role = "user"
    elif isinstance(rtype, str) and rtype.startswith("MODEL"):
        role = "assistant"
    else:
        return None
    ts = _parse_iso_timestamp(record.get("timestamp", ""))
    content = record.get("content", "")
    text_chunks: List[str] = []
    tool_use: List[dict] = []
    tool_result: List[dict] = []

    def _record_tool(part: dict) -> None:
        name = part.get("name", "") if isinstance(part, dict) else ""
        if not isinstance(name, str) or not name:
            return
        args = part.get("args", part.get("input", part.get("arguments", "")))
        if isinstance(args, str):
            input_str = args
        else:
            try:
                input_str = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                input_str = str(args)
        tool_use.append({
            "name": name,
            "input": input_str,
            "timestamp": ts,
        })

    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype in _TOOL_PART_TYPES:
                _record_tool(part)
                continue
            t = part.get("text", "")
            if isinstance(t, str) and t:
                text_chunks.append(t)
    elif isinstance(content, dict):
        if content.get("type", "") in _TOOL_PART_TYPES:
            _record_tool(content)
    elif isinstance(content, str):
        if content:
            text_chunks.append(content)

    # Record-level tool spec: ``type`` itself marks a tool call and the
    # spec lives in ``name``/``args`` (or ``input``/``arguments``).
    if isinstance(rtype, str) and rtype in _TOOL_PART_TYPES:
        _record_tool(record)

    text = "\n".join(text_chunks)

    return Message(
        role=role,
        text=text,
        tool_use=tuple(tool_use),
        tool_result=tuple(tool_result),
        timestamp=ts,
    )


def _extract_messages_from_transcript(path: Path) -> List[Message]:
    """Read an Antigravity transcript/overview JSONL into messages."""
    messages: List[Message] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
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
                parsed = _antigravity_message_from_record(record)
                if parsed is not None:
                    messages.append(parsed)
    except OSError:
        return messages
    return messages


def _extract_messages_from_brain(brain: Path) -> List[Message]:
    """Read messages from the first available transcript file in a brain.

    Tries each transcript candidate in order; the first one that exists
    and yields records wins.  Falls back to ``overview.txt``.
    """
    for candidate in _TRANSCRIPT_CANDIDATES:
        tpath = brain / candidate
        if tpath.is_file():
            messages = _extract_messages_from_transcript(tpath)
            if messages:
                return messages
    overview = brain / _OVERVIEW
    if overview.is_file():
        return _extract_messages_from_transcript(overview)
    return []


def read_messages(
    uuid: str, base_dir: Optional[str] = None
) -> List[Message]:
    """Return the full message list for an Antigravity session.

    Reuses :func:`read_session` for path resolution.  Reads the first
    available transcript file (``transcript.json``,
    ``transcript_full.jsonl``, ``transcript.jsonl``) or ``overview.txt``.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir)
    return _extract_messages_from_brain(Path(session.path))


def search(query: str, base_dir: Optional[str] = None) -> List[Session]:
    """Case-insensitive substring search across Antigravity session titles."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [
        session
        for session in list_sessions(base_dir)
        if needle in session.title.lower()
    ]


def session_exists(uuid: str, base_dir: Optional[str] = None) -> bool:
    if not _is_valid_uuid(uuid):
        return False
    try:
        _find_brain(uuid, base_dir)
    except (FileNotFoundError, ValueError):
        return False
    return True
