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
import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from ai_r.find_file_edits import edit_path_from_input
from ai_r.security import coerce_tool_input as _coerce_tool_input_shared

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


# --- Wrapper resolution: tool_kind + tool_resolved (F3.1) ------------------
# Some tool names are WRAPPERS that hide the real actor: a subagent spawn
# (Claude ``Task``/``Agent``, Codex ``spawn_agent``, OpenCode ``task``), a
# skill invocation (Claude ``Skill``/``SlashCommand``, OpenCode ``skill``) or
# an MCP tool (Claude-style ``mcp__<server>__<tool>``).  ``resolve_tool``
# classifies every call into a ``tool_kind`` and, for wrappers whose input
# carries the real name, surfaces it as ``tool_resolved``.  Signals are
# per-agent honest: when a wrapper's input has no recognisable name key the
# resolved name is ``None`` — never guessed.
#
# ``tool_kind`` is a SUPERSET of the ``classify_tool`` subtypes: the base
# categories stay as-is and wrappers/network calls get their own kinds.  The
# event ``type`` (``tool_call(<sub>)``) is untouched for backward-compat —
# a Task call is still ``tool_call(other)``; its kind lives in the refs.

# Subagent-spawn wrapper names (lowercase) — Claude ``Task`` (legacy) /
# ``Agent`` (current), OpenCode ``task``, Codex ``spawn_agent``.
_TASK_NAMES = frozenset({"task", "agent", "spawn_agent"})
# Skill/slash-command wrapper names — Claude ``Skill``/``SlashCommand``,
# OpenCode ``skill``.
_SKILL_NAMES = frozenset({"skill", "slashcommand"})
# Network-touching tool names (the F4.3 web-audit signal) — Claude
# ``WebFetch``/``WebSearch``, OpenCode ``webfetch``, Codex ``web_search``
# (surfaced from ``web_search_call`` rollout records by the codex parser),
# Gemini/Antigravity ``web_fetch``/``google_web_search`` (verified against
# the vendored gemini-cli reference).  Name-based only; Pi records no web
# tool — honest absence.
_WEB_NAMES = frozenset({
    "webfetch", "web_fetch", "websearch", "web_search", "google_web_search",
})

# Input keys that carry the real name under each wrapper, by preference.
# Task: Claude/OpenCode ``subagent_type``, Codex ``agent_type``.
_TASK_RESOLVE_KEYS = ("subagent_type", "agent_type", "subagent")
# Skill: Claude ``skill``, OpenCode ``name``, SlashCommand ``command``.
_SKILL_RESOLVE_KEYS = ("skill", "name", "command")

# Claude-style MCP tool name: ``mcp__<server>__<tool>``.  The first ``__``
# after the prefix splits server from tool (non-greedy: a server name never
# contains ``__``, a tool name may).  Codex/OpenCode/Pi record MCP calls
# under bare/underscore-joined names with no reliable server delimiter —
# no signal, so no mcp detection there (honest fallthrough).
_MCP_NAME_RE = re.compile(r"^mcp__(.+?)__(.+)$")

# The complete ``tool_kind`` vocabulary — base subtypes + wrapper kinds +
# ``web``.  Exported so consumers (and the ``query`` facet validator) can
# enumerate/validate kinds without re-deriving the mapping.
TOOL_KIND: frozenset[str] = frozenset(
    {"edit", "write", "read", "bash", "task", "skill", "mcp", "web", "other"}
)


def _first_str_value(payload: object, keys: Sequence[str]) -> Optional[str]:
    """Return the first non-empty string under ``keys`` in a dict payload."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def resolve_tool(name: str, payload: object = None) -> Tuple[str, Optional[str]]:
    """Classify a tool call: ``(tool_kind, tool_resolved)``.

    Args:
        name: The raw tool name as recorded by the agent.
        payload: The (already-parsed) tool input, when available — the
            wrapper's real name lives inside it (``subagent_type`` /
            ``agent_type`` for spawns, ``skill``/``name``/``command`` for
            skills).  Non-dict payloads are ignored.

    Returns:
        ``tool_kind`` is always one of :data:`TOOL_KIND`.  ``tool_resolved``
        is the real name under the wrapper — the subagent type, the skill
        name (a ``/command arg`` string is reduced to its bare command
        token), or ``"<server>:<tool>"`` for an MCP call — and ``None``
        whenever there is nothing to resolve (non-wrapper tools, or a
        wrapper whose input carries no recognisable name key).
    """
    key = (name or "").strip()
    low = key.lower()
    mcp_match = _MCP_NAME_RE.match(key)
    if mcp_match:
        return "mcp", f"{mcp_match.group(1)}:{mcp_match.group(2)}"
    if low in _TASK_NAMES:
        return "task", _first_str_value(payload, _TASK_RESOLVE_KEYS)
    if low in _SKILL_NAMES:
        resolved = _first_str_value(payload, _SKILL_RESOLVE_KEYS)
        if resolved:
            # SlashCommand carries ``"/commit -m msg"`` — keep the bare
            # command token so the resolved name is a stable identifier.
            resolved = resolved.split()[0].lstrip("/") or None
        return "skill", resolved
    if low in _WEB_NAMES:
        return "web", None
    return classify_tool(key), None


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
            A ``tool_call`` event additionally carries
            ``{"tool_kind": kind}`` (always, one of :data:`TOOL_KIND`) and
            ``{"tool_resolved": name}`` (only when a Skill/Task/MCP
            wrapper's input carried the real name — see
            :func:`resolve_tool`), plus ``{"is_error": bool}`` when its
            result was correlated (by ``tool_use_id``); the ``is_error``
            ref is absent when the outcome is unknown (agent exposes no
            per-result error signal).
        source: Provenance tag, ``"parser:<agent>"``.
        sha256: Content hash over ``(type, text, refs)`` for dedup /
            change-detection.  Deterministic across runs.
        message_index: Index of the hosting :class:`Message` in the
            parser's message list (kept for backward-compat with the
            record shape ``find_file_edits`` emits).
        model: The model that produced the hosting message, inherited
            from :attr:`~ai_r.parsers.models.Message.model` — an
            ``assistant_turn`` / ``tool_call`` / ``plan_event`` carries
            the model of the assistant message behind it.  ``None``
            where the format records no signal (user turns, Antigravity)
            — absence is honest, never fabricated.  Provenance metadata
            like ``agent``/``ts``: NOT part of the ``sha256`` content
            hash.
        body: The FULL serialized tool-call input (the command text for a
            shell call, the JSON body otherwise) — the searchable payload
            behind a ``tool_call`` whose :attr:`text` holds only the raw
            tool NAME.  Lets the ``text`` facet match a pattern that lives
            inside a multi-line command body (e.g. an ``rm`` buried in a
            ``for … do rm …; done`` loop) which the name-only :attr:`text`
            can never surface.  ``None`` for non-tool events and when the
            input is empty.  Match-only: NOT emitted in the query row (the
            body is surfaced on demand via ``plan``/``find_tool_calls``)
            and — like :attr:`model` — NOT part of the ``sha256`` hash.
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
    model: Optional[str] = None
    body: Optional[str] = None


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
    model: Optional[str] = None,
    body: Optional[str] = None,
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
        model=model,
        body=body or None,
    )


def _coerce_tool_input(raw: object) -> object:
    """Best-effort JSON-decode of a tool input (dicts pass through).

    Thin alias for :func:`ai_r.security.coerce_tool_input`, the single
    size-guarded coerce shared with ``find_tool_calls`` — a string above the
    1 MB cap is returned verbatim instead of being decoded (memory-exhaustion
    guard for tens-of-MB codex ``function_call.arguments`` blobs).
    """
    return _coerce_tool_input_shared(raw)


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
