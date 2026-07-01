"""Shared event atoms + helpers (INTERNAL — no intra-package cycles).

Holds the pieces that several verb modules need: the :class:`Event` atom, the
tool-name classification vocabulary, content hashing, tool-input coercion /
path extraction, and the small ``_plan_ref_value`` ref accessor.  Everything
here depends only on stdlib + :mod:`ai_r.find_file_edits`, so it can be
imported by any module in the package without creating a cycle.

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from ai_r.find_file_edits import edit_path_from_input

# --- Tool-name → normalized subtype ---------------------------------------
# ``tool_call`` events carry a normalized subtype so a consumer can filter
# ``type="tool_call(edit)"`` without knowing each agent's tool vocabulary.
# The mapping is deliberately small and agent-neutral; anything unmatched
# falls through to ``"other"`` (still a valid ``tool_call`` event).
_EDIT_NAMES = frozenset({
    "edit", "multiedit", "multi_edit", "notebookedit",
    "str_replace", "patch", "apply_patch", "edit_file", "update_file",
    "file_edit",
})
_WRITE_NAMES = frozenset({
    "write", "write_file", "create_file", "file",
})
_READ_NAMES = frozenset({
    "read", "read_file", "view", "cat", "open",
})
_BASH_NAMES = frozenset({
    "bash", "shell", "exec_command", "local_shell_call", "run_command",
    "run_terminal_cmd", "terminal",
})

# Keys that carry a file path in a (parsed) tool input.  Superset of
# ``find_file_edits.EDIT_PATH_KEYS`` so ``read``-style calls resolve too.
_PATH_KEYS = ("file_path", "notebook_path", "path", "filePath", "abspath")


def classify_tool(name: str) -> str:
    """Return the normalized tool_call subtype for a raw tool ``name``.

    One of ``edit``, ``write``, ``read``, ``bash`` or ``other``.  The
    match is case-insensitive; unknown tools are ``"other"`` (still a
    valid ``tool_call`` event, just uncategorised).
    """
    key = (name or "").strip().lower()
    if key in _EDIT_NAMES:
        return "edit"
    if key in _WRITE_NAMES:
        return "write"
    if key in _READ_NAMES:
        return "read"
    if key in _BASH_NAMES:
        return "bash"
    return "other"


# The complete vocabulary of normalized subtypes :func:`classify_tool` can
# return.  A ``tool_call`` event's ``type`` is always ``tool_call(<sub>)`` for
# one of these; exported so a consumer can enumerate/validate subtypes without
# re-deriving the mapping.
TOOL_SUBTYPE: frozenset[str] = frozenset(
    {"edit", "write", "read", "bash", "other"}
)


@dataclass(frozen=True)
class Event:
    """A single normalized session event — the query atom.

    Attributes:
        id: Stable within-session identity, ``"{session_id}:{seq}"``
            where ``seq`` is the monotonic event index within the
            session's normalized stream.  Unique per session; use with
            ``session_id`` for global identity.
        session_id: Owning session uuid.
        agent: Lowercase agent name (``claude``/``codex``/...).
        ts: ISO-8601 timestamp string, or ``None`` when the source
            record carried none (falls back to the session date at
            construction time when available).
        type: ``user_turn`` | ``assistant_turn`` | ``tool_call(<sub>)``
            | ``plan_event``.  ``<sub>`` is the :func:`classify_tool`
            result (e.g. ``tool_call(edit)``).
        text: Plain-text payload — the turn text, or the raw tool name
            for a tool_call.  ``None`` when empty.
        refs: Structured references pulled from the event — currently
            ``{"file": path}`` and/or ``{"tool": name}`` entries so
            ``file`` / ``tool`` facets can filter without re-parsing.
        source: Provenance tag, ``"parser:<agent>"``.
        sha256: Content hash over ``(type, text, refs)`` for dedup /
            change-detection.  Deterministic across runs.
        message_index: Index of the hosting :class:`Message` in the
            parser's message list (kept for backward-compat with the
            record shape ``find_file_edits`` emits).
    """

    id: str
    session_id: str
    agent: str
    ts: Optional[str]
    type: str
    text: Optional[str] = None
    refs: Tuple[dict, ...] = ()
    source: str = ""
    sha256: str = ""
    message_index: int = -1


def _sha256(event_type: str, text: Optional[str], refs: Sequence[dict]) -> str:
    payload = json.dumps(
        {"type": event_type, "text": text or "", "refs": list(refs)},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mk_event(
    *,
    session_id: str,
    agent: str,
    seq: int,
    ts: Optional[str],
    event_type: str,
    text: Optional[str],
    refs: Sequence[dict],
    message_index: int,
) -> Event:
    refs_tuple = tuple(refs)
    return Event(
        id=f"{session_id}:{seq}",
        session_id=session_id,
        agent=agent,
        ts=ts,
        type=event_type,
        text=text or None,
        refs=refs_tuple,
        source=f"parser:{agent}",
        sha256=_sha256(event_type, text, refs_tuple),
        message_index=message_index,
    )


def _coerce_tool_input(raw: object) -> object:
    """Best-effort JSON-decode of a tool input (dicts pass through)."""
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _path_from_payload(payload: object) -> Optional[str]:
    """Extract a file path from a (parsed) tool input, incl. read-style keys."""
    hit = edit_path_from_input(payload)
    if hit:
        return hit
    if isinstance(payload, dict):
        for key in _PATH_KEYS:
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _plan_ref_value(refs: Sequence[dict], key: str) -> Optional[str]:
    for r in refs:
        if key in r and isinstance(r[key], str):
            return r[key]
    return None
