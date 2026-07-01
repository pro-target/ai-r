"""Event model + query core — the unified session-event surface (Phase 1).

This module layers a single, agent-neutral *event stream* over the
per-agent parsers.  Every parser already returns
:class:`ai_r.parsers.models.Message` objects; :func:`iter_events`
normalises those (plus their embedded ``tool_use`` calls) into a flat
sequence of :class:`Event` records so downstream verbs never have to
know how any individual agent stores turns or tool calls.

Design (see ``_docs/knowledge/extraction-core.md``):

* **Event** is the atom: ``id, session_id, agent, ts, type, text?,
  refs[], source, sha256``.  ``type`` is one of ``user_turn``,
  ``assistant_turn``, ``tool_call(<sub>)`` or ``plan_event`` (the last
  is a Phase-2 placeholder — no producer emits it yet).
* **query(facets)** is the workhorse filter over that stream.  The
  killer facet is ``relative_to`` + ``direction`` + ``n``: a general
  timeline walk in either direction, of which the historical
  :func:`ai_r.find_file_edits.previous_user_intent` is the ``prev`` /
  ``n=1`` special case.
* ``text`` + ``sort=relevance`` re-uses the *exact* BM25 scorer that
  backs ``search_sessions`` (:mod:`ai_r.ranking`) — no algorithm is
  duplicated here.
* **intent** / **reaction** presets are thin wrappers over ``query``.

Everything is additive: existing tools/tests are untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict as _OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    Iterable,
    List,
    Optional,
    OrderedDict as OrderedDictType,
    Sequence,
    Tuple,
)

from ai_r.find_file_edits import (
    edit_path_from_input,
    parse_iso_bound,
    previous_user_intent,
    to_utc_aware,
)
from ai_r.parsers import PARSERS, Message, iso, target_agents
from ai_r.ranking import bm25_scores as _bm25_scores, tokenize as _tokenize

__all__ = [
    "Event",
    "TOOL_SUBTYPE",
    "classify_tool",
    "iter_events",
    "query",
    "intent",
    "reaction",
    "Plan",
    "plan",
    "get_body",
    "aggregate",
    "diff",
    "detect_current",
]


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


# --- plan signals (INTERNAL, agent-specific normalization) ----------------
# Different agents record a "plan" through different signals.  This layer
# maps each parser's raw signal to a single normalized ``plan_event`` so the
# consumer never sees ``ExitPlanMode`` / ``update_plan`` /
# ``implementation_plan.md`` — only a unified plan.  The table below is a
# deliberate implementation detail; nothing outside this module keys off it.
#
# The recognised per-agent signals (``agent_signal`` tag in refs):
#
# * claude  — ``ExitPlanMode`` tool_use (input carries ``plan`` text) OR a
#             ``Write`` tool_use whose path matches ``plans/*.md`` (input
#             carries the full body).
# * codex   — ``update_plan`` tool_use (input carries ``steps[]`` with
#             ``status``); rewritten each call, so the LAST call in a task
#             group is the final plan.
# * antigravity — ``implementation_plan.md`` in the session's brain dir (a
#             file, not a message tool_use — emitted once per session).
# * opencode / pi — no plan signal → nothing emitted.

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_CLAUDE_PLAN_WRITE_RE = re.compile(r"plans/[^/]*\.md$", re.IGNORECASE)
# The ``plans/<slug>.md`` portion of a Claude plan-file path — the stable
# per-task slug, extracted so two different absolute paths that share the same
# plan slug still group as one task.
_CLAUDE_PLAN_SLUG_RE = re.compile(r"(plans/[^/]*\.md)$", re.IGNORECASE)


def _claude_plan_slug(path: Optional[str]) -> Optional[str]:
    """Return the ``plans/<slug>.md`` slug of a Claude plan path, else ``None``."""
    if not isinstance(path, str) or not path:
        return None
    match = _CLAUDE_PLAN_SLUG_RE.search(path)
    if match:
        return match.group(1).lower()
    return None


@dataclass(frozen=True)
class _PlanSignal:
    """One detected plan signal within a session (internal, pre-normalization).

    Carries everything the normalized :class:`Plan` needs plus the body that
    is *not* inlined into the :class:`Event` (fetched on demand by
    :func:`get_body`).

    ``task_key`` is the STABLE task-grouping key (see
    :func:`_plan_signals_for_session`).  It is the plan-file path/slug when
    the agent has one (Claude ``plans/<slug>.md``, Antigravity
    ``implementation_plan.md``) so title drift within one iteration chain
    never splits a single task; it falls back to the normalized title only
    when no file path is available (Codex ``update_plan``, or a Claude
    ``ExitPlanMode`` that precedes any plan-file Write).
    """

    title: str
    agent_signal: str
    path: Optional[str] = None
    body: Optional[str] = None
    steps: Optional[Tuple[dict, ...]] = None
    status: Optional[str] = None
    message_index: int = -1
    task_key: str = ""


def _title_from_markdown_body(body: str) -> Optional[str]:
    """Return the first ``# `` heading of a markdown plan body, if any."""
    if not isinstance(body, str) or not body:
        return None
    match = _HEADING_RE.search(body)
    if match:
        return match.group(1).strip()
    return None


def _plan_signal_from_tool(
    tool: dict, *, agent: str, message_index: int
) -> Optional[_PlanSignal]:
    """Detect a plan signal in one assistant ``tool_use`` entry.

    Covers the Claude (``ExitPlanMode`` / ``Write plans/*.md``) and Codex
    (``update_plan``) message-level signals.  Antigravity's file-based
    signal is handled separately in :func:`_antigravity_plan_signal`.
    Returns ``None`` when the tool is not a plan signal.
    """
    name = tool.get("name", "")
    if not isinstance(name, str) or not name:
        return None
    payload = _coerce_tool_input(tool.get("input", ""))

    if agent == "claude":
        if name == "ExitPlanMode":
            body = ""
            if isinstance(payload, dict):
                raw = payload.get("plan")
                if isinstance(raw, str):
                    body = raw
            title = _title_from_markdown_body(body) or "Plan"
            return _PlanSignal(
                title=title,
                agent_signal="claude:ExitPlanMode",
                body=body or None,
                message_index=message_index,
            )
        if name in ("Write", "write", "write_file", "create_file"):
            fpath = _path_from_payload(payload)
            if fpath and _CLAUDE_PLAN_WRITE_RE.search(fpath):
                body = ""
                if isinstance(payload, dict):
                    raw = payload.get("content")
                    if isinstance(raw, str):
                        body = raw
                title = _title_from_markdown_body(body) or fpath.rsplit("/", 1)[-1]
                return _PlanSignal(
                    title=title,
                    agent_signal="claude:Write(plans/*.md)",
                    path=fpath,
                    body=body or None,
                    message_index=message_index,
                )
        return None

    if agent == "codex":
        if name == "update_plan":
            steps: Tuple[dict, ...] = ()
            status: Optional[str] = None
            title = "Plan"
            if isinstance(payload, dict):
                raw_steps = payload.get("steps")
                if isinstance(raw_steps, list):
                    steps = tuple(s for s in raw_steps if isinstance(s, dict))
                raw_title = payload.get("name") or payload.get("explanation")
                if isinstance(raw_title, str) and raw_title.strip():
                    title = raw_title.strip()
                elif steps:
                    # Fall back to the first step's text as a stable title so
                    # task grouping has something agent-neutral to key on.
                    first = steps[0]
                    step_text = first.get("step") or first.get("text") or ""
                    if isinstance(step_text, str) and step_text.strip():
                        title = step_text.strip()
                # Overall status: last non-completed step, else "completed".
                status = _codex_plan_status(steps)
            return _PlanSignal(
                title=title,
                agent_signal="codex:update_plan",
                steps=steps or None,
                status=status,
                message_index=message_index,
            )
        return None

    return None


def _codex_plan_status(steps: Sequence[dict]) -> Optional[str]:
    """Roll a Codex ``update_plan`` ``steps[]`` up to one overall status."""
    if not steps:
        return None
    statuses = [
        s.get("status") for s in steps if isinstance(s.get("status"), str)
    ]
    if not statuses:
        return None
    if all(st == "completed" for st in statuses):
        return "completed"
    if any(st == "in_progress" for st in statuses):
        return "in_progress"
    return "pending"


def _antigravity_plan_signal(session_path: str) -> Optional[_PlanSignal]:
    """Detect Antigravity's ``implementation_plan.md`` plan in a brain dir.

    Reuses the antigravity parser's knowledge that the plan lives as a
    markdown file inside the session's brain directory (``session.path``).
    Emitted once per session (file-based, not message-based).  Returns
    ``None`` when the file is absent/unreadable.
    """
    from pathlib import Path

    if not session_path:
        return None
    plan_file = Path(session_path) / "implementation_plan.md"
    if not plan_file.is_file():
        return None
    try:
        body = plan_file.read_text(encoding="utf-8")
    except OSError:
        return None
    title = _title_from_markdown_body(body) or "implementation_plan.md"
    return _PlanSignal(
        title=title,
        agent_signal="antigravity:implementation_plan.md",
        path=str(plan_file),
        body=body or None,
        message_index=-1,
    )


def _plan_signals_for_session(
    messages: Sequence[Any],
    *,
    agent: str,
    session_path: str,
) -> List[_PlanSignal]:
    """Return the ordered plan signals detected in one session.

    Message-level signals (Claude / Codex) are collected in message order;
    Antigravity's file-based signal is appended once.  Order matters for
    ``get_body`` (matched by seq) and for task grouping (last = final).

    Each returned signal carries a stable ``task_key`` (see
    :class:`_PlanSignal`).  For Claude the key is the ``plans/<slug>.md``
    slug — Write signals carry it directly; an ``ExitPlanMode`` (which has no
    path) *inherits the slug of the nearest preceding plan-file Write in the
    same session*, so a title that drifts within one plan-file iteration
    chain no longer splits a single task.  When no slug precedes an
    ``ExitPlanMode`` the key falls back to the normalized title.  For
    Antigravity the key is the plan-file path; for Codex (no file) it is the
    normalized title (contiguous ``update_plan`` runs group naturally).
    """
    from dataclasses import replace

    signals: List[_PlanSignal] = []
    if agent in ("claude", "codex"):
        last_slug: Optional[str] = None  # nearest preceding Claude plan slug
        for idx, msg in enumerate(messages):
            if getattr(msg, "role", None) != "assistant":
                continue
            for tool in getattr(msg, "tool_use", ()) or ():
                if not isinstance(tool, dict):
                    continue
                sig = _plan_signal_from_tool(
                    tool, agent=agent, message_index=idx
                )
                if sig is None:
                    continue
                if agent == "claude":
                    slug = _claude_plan_slug(sig.path)
                    if slug is not None:
                        # A plan-file Write: it defines the current slug and
                        # every following ExitPlanMode inherits it.
                        last_slug = slug
                        task_key = slug
                    elif last_slug is not None:
                        # ExitPlanMode after a Write: inherit its slug.
                        task_key = last_slug
                    else:
                        # No slug seen yet — fall back to normalized title.
                        task_key = _normalize_task_key(sig.title)
                    sig = replace(sig, task_key=task_key)
                else:  # codex — no file, key by normalized title
                    sig = replace(sig, task_key=_normalize_task_key(sig.title))
                signals.append(sig)
    elif agent == "antigravity":
        sig = _antigravity_plan_signal(session_path)
        if sig is not None:
            # File-based: the plan-file path IS the task key.
            signals.append(replace(sig, task_key=(sig.path or _normalize_task_key(sig.title))))
    # opencode / pi: no plan signal.
    return signals


def _messages_to_events(
    messages: Sequence[Any],
    *,
    session_id: str,
    agent: str,
    session_ts: Optional[datetime],
    session_path: str = "",
) -> List[Event]:
    """Normalize one session's messages into an ordered Event list.

    A ``user`` message → one ``user_turn``.  An ``assistant`` message →
    one ``assistant_turn`` (when it has text) followed by one
    ``tool_call(<sub>)`` per ``tool_use`` entry.  ``tool`` role records
    carry no turn text of their own and are skipped (their results are
    not first-class events in Phase 1).
    """
    events: List[Event] = []
    seq = 0
    session_iso = iso(session_ts) if session_ts is not None else None

    # Detect plan signals once; index the message-level ones by their
    # triggering message so each ``plan_event`` is emitted inline (right
    # after the tool_call that produced it), keeping the stream chronological.
    plan_signals = _plan_signals_for_session(
        messages, agent=agent, session_path=session_path
    )
    signals_by_msg: dict[int, List[_PlanSignal]] = {}
    file_signals: List[_PlanSignal] = []
    for sig in plan_signals:
        if sig.message_index >= 0:
            signals_by_msg.setdefault(sig.message_index, []).append(sig)
        else:
            file_signals.append(sig)

    def _plan_refs(sig: _PlanSignal) -> List[dict]:
        refs: List[dict] = [{"title": sig.title}, {"agent_signal": sig.agent_signal}]
        if sig.path:
            refs.append({"path": sig.path})
        # ``task_key`` is the stable grouping key (plan-file slug when the
        # agent has one, normalized title otherwise) — carried in refs so
        # ``_assign_plan_kinds`` groups without re-deriving it.
        if sig.task_key:
            refs.append({"task_key": sig.task_key})
        return refs

    for idx, msg in enumerate(messages):
        role = getattr(msg, "role", None)
        text = getattr(msg, "text", "") or ""
        msg_ts = to_utc_aware(getattr(msg, "timestamp", None))
        ts_iso = iso(msg_ts) if msg_ts is not None else session_iso

        if role == "user":
            if isinstance(text, str) and text.strip():
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=ts_iso,
                    event_type="user_turn", text=text, refs=(),
                    message_index=idx,
                ))
                seq += 1
            continue

        if role == "assistant":
            if isinstance(text, str) and text.strip():
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=ts_iso,
                    event_type="assistant_turn", text=text, refs=(),
                    message_index=idx,
                ))
                seq += 1
            for tool in getattr(msg, "tool_use", ()) or ():
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name", "")
                if not isinstance(name, str) or not name:
                    continue
                sub = classify_tool(name)
                tool_ts = to_utc_aware(tool.get("timestamp"))
                tool_iso = iso(tool_ts) if tool_ts is not None else ts_iso
                refs: List[dict] = [{"tool": name}]
                fpath = _path_from_payload(_coerce_tool_input(tool.get("input", "")))
                if fpath:
                    refs.append({"file": fpath})
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=tool_iso,
                    event_type=f"tool_call({sub})", text=name, refs=refs,
                    message_index=idx,
                ))
                seq += 1
            # Emit any plan_event(s) triggered by this assistant message,
            # right after its tool_call(s) so the stream stays chronological.
            for sig in signals_by_msg.get(idx, ()):
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=ts_iso,
                    event_type="plan_event",
                    text=sig.title,
                    refs=_plan_refs(sig),
                    message_index=idx,
                ))
                seq += 1
            continue
        # ``tool`` role and anything else: not a first-class Phase-1 event.

    # File-based plan signals (Antigravity's ``implementation_plan.md``) have
    # no hosting message — append them once at the end of the stream.
    for sig in file_signals:
        events.append(_mk_event(
            session_id=session_id, agent=agent, seq=seq, ts=session_iso,
            event_type="plan_event",
            text=sig.title,
            refs=_plan_refs(sig),
            message_index=sig.message_index,
        ))
        seq += 1
    return events


def iter_events(
    agent: Optional[str] = None,
    *,
    session: Optional[str] = None,
) -> Iterable[Event]:
    """Yield the normalized Event stream across sessions, cross-agent.

    Args:
        agent: Optional agent filter (``claude``/``codex``/...); ``None``
            = every agent.
        session: Optional session-uuid filter; restrict the scan to a
            single session (cheap fast-path for ``relative_to`` walks).

    Yields:
        :class:`Event` records in per-session, chronological (parse)
        order.  Sessions that fail to read are skipped (an audit tool
        prefers a partial view to a crash), mirroring ``find_file_edits``.
    """
    for agent_name in target_agents(agent):
        parser = PARSERS[agent_name]
        agent_lc = agent_name.value.lower()
        for sess in parser.list_sessions():
            if session is not None and sess.uuid != session:
                continue
            try:
                messages = parser.read_messages(sess.uuid)
            except (FileNotFoundError, ValueError, OSError):
                continue
            session_ts = to_utc_aware(sess.date)
            yield from _messages_to_events(
                messages,
                session_id=sess.uuid,
                agent=agent_lc,
                session_ts=session_ts,
                session_path=getattr(sess, "path", "") or "",
            )


# --- query facets ----------------------------------------------------------


def _type_matches(event_type: str, wanted: str) -> bool:
    """Match an event ``type`` against a ``type`` facet value.

    * ``"tool_call"`` matches every ``tool_call(<sub>)`` event.
    * ``"tool_call(edit)"`` matches only that subtype.
    * ``"user_turn"`` / ``"assistant_turn"`` / ``"plan_event"`` match exactly.
    """
    if event_type == wanted:
        return True
    if wanted == "tool_call" and event_type.startswith("tool_call("):
        return True
    return False


def _event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "session_id": event.session_id,
        "agent": event.agent,
        "ts": event.ts,
        "type": event.type,
        "text": event.text,
        "refs": [dict(r) for r in event.refs],
        "source": event.source,
        "sha256": event.sha256,
        "message_index": event.message_index,
    }


def _attach_intents(event_dicts: List[dict[str, Any]]) -> None:
    """Attach a top-level ``intent`` to each event dict, in place.

    The intent of an event is the request behind it: the previous user turn's
    text, resolved by the SAME :func:`previous_user_intent` walk-back the
    legacy tools (``find_file_edits`` / ``find_tool_calls`` / ``session_diff``)
    use — over the FULL raw message list, indexed by the event's stored
    ``message_index``.  Reusing that exact helper is what makes the enriched
    ``query`` output byte-identical to the legacy ``intent`` attribution.

    Sessions are read once and cached across the batch so enrichment is
    O(sessions) not O(events).  An event whose session/message cannot be
    resolved gets ``intent=None`` (the same default the legacy tools emit when
    no preceding user turn exists).
    """
    msgs_cache: dict[str, Sequence[Any]] = {}

    def _messages_for(session_id: str, agent: str) -> Optional[Sequence[Any]]:
        if session_id in msgs_cache:
            return msgs_cache[session_id]
        for agent_name in target_agents(agent or None):
            parser = PARSERS[agent_name]
            for sess in parser.list_sessions():
                if sess.uuid != session_id:
                    continue
                messages: list[Message] = []
                try:
                    messages = parser.read_messages(sess.uuid)
                except (FileNotFoundError, ValueError, OSError):
                    messages = []
                msgs_cache[session_id] = messages
                return messages
        msgs_cache[session_id] = ()
        return ()

    for ev in event_dicts:
        session_id = ev.get("session_id") or ""
        agent = ev.get("agent") or ""
        idx = ev.get("message_index", -1)
        messages = _messages_for(session_id, agent)
        if messages and isinstance(idx, int) and 0 <= idx < len(messages):
            ev["intent"] = previous_user_intent(messages, idx)
        else:
            ev["intent"] = None


def _walk_relative(
    events: Sequence[Event],
    anchor_id: str,
    direction: str,
    n_all: bool,
    n: int,
    *,
    step_type: str = "user_turn",
) -> List[Event]:
    """Timeline walk from ``anchor_id`` in ``direction`` collecting turns.

    Generalises :func:`ai_r.find_file_edits.previous_user_intent`:
    ``direction="prev"`` walks backwards (the historical ``intent``
    behaviour), ``"next"`` walks forwards (its mirror).  Collects up to
    ``n`` events of ``step_type`` (default ``user_turn``); ``n_all``
    collects every match in that direction.  Returns them in timeline
    order (ascending index), regardless of walk direction.
    """
    pos = next(
        (i for i, ev in enumerate(events) if ev.id == anchor_id), None
    )
    if pos is None:
        return []
    step = -1 if direction == "prev" else 1
    out: List[Event] = []
    j = pos + step
    while 0 <= j < len(events):
        ev = events[j]
        if _type_matches(ev.type, step_type):
            out.append(ev)
            if not n_all and len(out) >= n:
                break
        j += step
    out.sort(key=lambda e: int(e.id.rsplit(":", 1)[-1]))
    return out


def query(
    *,
    type: Optional[str] = None,
    agent: Optional[str] = None,
    session: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    file: Optional[str] = None,
    tool: Optional[str] = None,
    text: Optional[str] = None,
    sort: str = "date",
    relative_to: Optional[str] = None,
    direction: str = "prev",
    n: Any = 1,
    step_type: str = "user_turn",
    limit: int = 0,
    with_intent: bool = False,
    # --- Phase-2/3 placeholders (accepted, TODO not-yet-implemented) ---
    kind: Optional[str] = None,
    parent: Optional[str] = None,
    group: Optional[str] = None,
) -> List[dict[str, Any]]:
    """Filter/search the normalized Event stream — the Phase-1 workhorse.

    Facets (all optional, all parameters — no hard-wired variants):

    * ``type``  — ``user_turn`` | ``assistant_turn`` | ``tool_call`` |
      ``tool_call(<sub>)`` | ``plan_event``.  Bare ``tool_call`` matches
      every subtype.
    * ``agent`` — restrict to one agent (``claude``/``codex``/...).
    * ``session`` — restrict to one session uuid.
    * ``since`` / ``until`` — ISO-8601 bounds (inclusive) on ``ts``.
    * ``file`` — substring matched against any ``refs[*].file``.
    * ``tool`` — substring (pattern) matched against any ``refs[*].tool``
      (case-insensitive).
    * ``text`` — substring matched against event ``text``
      (case-insensitive).  With ``sort="relevance"`` the survivors are
      BM25-ranked using the **same scorer** as ``search_sessions``.
    * ``sort`` — ``"date"`` (default, ts-ascending) or ``"relevance"``
      (BM25 over ``text``; requires a ``text`` facet, else falls back to
      date order).
    * ``relative_to`` + ``direction`` (``prev``|``next``) + ``n``
      (``1`` | ``"all"``) — the neighbouring-turn walk.  Generalises
      ``previous_user_intent`` (prev/1) to both directions and any count.
      ``step_type`` selects which event type to collect (default
      ``user_turn``).  When ``relative_to`` is set, other filter facets
      are ignored (the anchor + walk fully specify the result).
    * ``with_intent`` — when ``True``, attach a top-level ``intent`` to each
      returned event: the request behind it (previous user turn), resolved by
      the SAME :func:`previous_user_intent` walk-back the legacy tools use.
      Default ``False`` so the base event shape is unchanged.  This is what
      lets ``diff`` / the ``find_file_edits`` preset reproduce the legacy
      ``intent`` field byte-for-byte.

    ``kind`` / ``parent`` / ``group`` are **not yet implemented** (Phase 2/3
    — plan/subagent facets).  They are accepted in the signature for forward
    compatibility, but passing a non-``None`` value raises
    :class:`ValueError` (fail-loud) rather than silently no-op'ing, so an
    external client is never misled into thinking a filter was applied.

    Returns a list of event dicts (see :func:`_event_to_dict`).  Invalid
    arguments raise :class:`ValueError` (the MCP wrapper converts these
    to the error-dict convention).
    """
    if direction not in ("prev", "next"):
        raise ValueError(
            f"direction must be 'prev' or 'next', got {direction!r}"
        )
    sort_lc = (sort or "date").lower()
    if sort_lc not in ("date", "relevance"):
        raise ValueError(
            f"sort must be 'relevance' or 'date', got {sort!r}"
        )
    # Normalize ``n``: accepts 1/all (or any positive int / "all").
    n_all = False
    n_int = 1
    if isinstance(n, str):
        if n.strip().lower() == "all":
            n_all = True
        else:
            try:
                n_int = int(n)
            except ValueError as exc:
                raise ValueError(
                    f"n must be a positive integer or 'all', got {n!r}"
                ) from exc
    elif isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"n must be a positive integer or 'all', got {n!r}")
    else:
        n_int = n
    if not n_all and n_int < 1:
        raise ValueError(f"n must be >= 1 or 'all', got {n!r}")

    # Phase 2/3 facets (kind=subagent + parent tree, group for plan_event) are
    # not implemented yet.  Fail loud rather than silently ignore, so a caller
    # is never misled into thinking the filter took effect.
    if kind is not None or parent is not None or group is not None:
        raise ValueError(
            "kind/parent/group not yet supported (Phase 2/3 stub)"
        )

    # --- relative_to walk: needs a single, contiguous, ordered stream ----
    if relative_to is not None:
        # The anchor id is ``"{session}:{seq}"`` — scope the scan to that
        # session so the walk is over one contiguous timeline.
        anchor_session = relative_to.rsplit(":", 1)[0] if ":" in relative_to else session
        stream = list(iter_events(agent, session=anchor_session or session))
        walked = _walk_relative(
            stream, relative_to, direction, n_all, n_int, step_type=step_type
        )
        out = [_event_to_dict(ev) for ev in walked]
        if with_intent:
            _attach_intents(out)
        return out

    # --- ordinary facet filter ------------------------------------------
    since_dt = parse_iso_bound(since, "since")
    until_dt = parse_iso_bound(until, "until")
    file_needle = file if file else None
    tool_needle = tool.lower() if tool else None
    text_needle = text.lower() if text else None

    survivors: List[Event] = []
    score_texts: List[str] = []
    for ev in iter_events(agent, session=session):
        if type is not None and not _type_matches(ev.type, type):
            continue
        if since_dt is not None or until_dt is not None:
            ev_dt = parse_iso_bound(ev.ts, "ts") if ev.ts else None
            if since_dt is not None and (ev_dt is None or ev_dt < since_dt):
                continue
            if until_dt is not None and (ev_dt is None or ev_dt > until_dt):
                continue
        if file_needle is not None:
            files = [r.get("file", "") for r in ev.refs if "file" in r]
            if not any(file_needle in f for f in files):
                continue
        if tool_needle is not None:
            tools = [r.get("tool", "").lower() for r in ev.refs if "tool" in r]
            if not any(tool_needle in t for t in tools):
                continue
        if text_needle is not None:
            if not ev.text or text_needle not in ev.text.lower():
                continue
        survivors.append(ev)
        score_texts.append((ev.text or "").lower())

    if sort_lc == "relevance" and text_needle and survivors:
        # Re-use the SAME BM25 scorer that backs search_sessions.
        query_tokens = _tokenize(text_needle)
        docs_tokens = [_tokenize(t) for t in score_texts]
        scores = _bm25_scores(query_tokens, docs_tokens)
        order = sorted(
            range(len(survivors)), key=lambda i: scores[i], reverse=True
        )
        survivors = [survivors[i] for i in order]
    else:
        # Date order: ts-ascending, None ts last (stable within session).
        survivors.sort(key=lambda e: (e.ts is None, e.ts or ""))

    if limit and len(survivors) > limit:
        survivors = survivors[:limit]
    out = [_event_to_dict(ev) for ev in survivors]
    if with_intent:
        _attach_intents(out)
    return out


# --- presets (thin wrappers, no duplicated logic) --------------------------


def intent(event: str, n: Any = 1, *, agent: Optional[str] = None) -> List[dict[str, Any]]:
    """Preset: previous user turn(s) before ``event`` — the request behind it.

    Expands to ``query(relative_to=event, direction="prev", n=n)``.  The
    ``n=1`` case reproduces :func:`previous_user_intent`.
    """
    return query(relative_to=event, direction="prev", n=n, agent=agent)


def reaction(event: str, n: Any = 1, *, agent: Optional[str] = None) -> List[dict[str, Any]]:
    """Preset: following user turn(s) after ``event`` — the user's reaction.

    Expands to ``query(relative_to=event, direction="next", n=n)`` — the
    forward mirror of :func:`intent`.
    """
    return query(relative_to=event, direction="next", n=n, agent=agent)


# --- Plan atom + task grouping + get_body ----------------------------------


@dataclass(frozen=True)
class Plan:
    """A normalized plan atom — agent differences hidden.

    Attributes:
        id: The owning ``plan_event`` id (``"{session_id}:{seq}"``).
        session_id: Owning session uuid.
        agent: Lowercase agent name.
        title: The plan title (may drift across revisions of one task).
        task_id: Stable grouping key — the plan-file slug
            (``plans/<slug>.md`` for Claude, plan-file path for Antigravity)
            when the agent has one, else the normalized title (Codex).
            Plans sharing a ``task_id`` are revisions of one task, even when
            their titles drifted.
        kind: ``draft`` | ``final`` | ``completed_major``.  Within a task
            group the latest plan_event is ``final``, earlier ones are
            ``draft``; plans belonging to *earlier* completed task groups
            are ``completed_major``.
        path: Source path when the signal is file-backed (``plans/*.md`` for
            Claude Write, ``implementation_plan.md`` for Antigravity).
        steps: Codex ``update_plan`` steps (with per-step ``status``), else
            ``None``.  Bodies/steps are on-demand — the Plan carries them
            only after they were resolved by :func:`plan` (they are ``None``
            on the bare atom otherwise).
        status: Rolled-up status (Codex), else ``None``.
        refs: The originating event refs (``title``/``agent_signal``/``path``).
        sha256: Content hash of the originating plan_event.
    """

    id: str
    session_id: str
    agent: str
    title: str
    task_id: str
    kind: str
    path: Optional[str] = None
    steps: Optional[Tuple[dict, ...]] = None
    status: Optional[str] = None
    refs: Tuple[dict, ...] = ()
    sha256: str = ""


_WS_RE = re.compile(r"\s+")


def _normalize_task_key(title: Optional[str]) -> str:
    """Normalize a plan title into a stable task-grouping key.

    Grouping is by *task*, NOT by slug/filename: lower-cased, whitespace
    collapsed, surrounding punctuation trimmed.  Two plan revisions with the
    same human title collapse to one task even if their files differ.
    """
    text = (title or "").strip().lower()
    text = _WS_RE.sub(" ", text)
    return text.strip(" #:-—")


def _plan_ref_value(refs: Sequence[dict], key: str) -> Optional[str]:
    for r in refs:
        if key in r and isinstance(r[key], str):
            return r[key]
    return None


def _assign_plan_kinds(events: Sequence[dict[str, Any]]) -> List[Plan]:
    """Group plan_events into tasks and assign ``draft``/``final``/major.

    Groups by ``task_key`` (the plan-file slug when the agent has one, else
    the normalized title — see :func:`_plan_signals_for_session`).  Keying on
    the *slug* rather than the title is the fix for real-data title drift: a
    Claude iteration chain writing one ``plans/<slug>.md`` file emits several
    plan_events whose titles drift as they get decorated, but they are one
    task and must not be split.

    Within each group the latest event (by ts, then seq) is ``final`` and the
    rest are ``draft``.  Across groups, every plan of a task whose *final* is
    older than another task's final is ``completed_major`` (a superseded
    prior task, i.e. a DIFFERENT slug), except the single most-recent task
    which keeps ``draft``/``final``.
    """
    # Bucket events by task key, preserving arrival order within a bucket.
    # The stable ``task_key`` ref is the primary key; fall back to a
    # normalized title only for legacy events that predate the ref.
    buckets: "OrderedDictType[str, List[dict[str, Any]]]" = _OrderedDict()
    for ev in events:
        key = _plan_ref_value(ev.get("refs", ()), "task_key")
        if not key:
            title = _plan_ref_value(ev.get("refs", ()), "title") or ev.get("text") or ""
            key = _normalize_task_key(title)
        buckets.setdefault(key, []).append(ev)

    def _seq(ev: dict[str, Any]) -> int:
        try:
            return int(str(ev.get("id", "")).rsplit(":", 1)[-1])
        except (ValueError, TypeError):
            return -1

    def _sort_key(ev: dict[str, Any]) -> Tuple[bool, str, int]:
        ts = ev.get("ts")
        return (ts is None, ts or "", _seq(ev))

    # Determine each task's "final time" (its latest event) to rank tasks.
    task_final_time: dict[str, Tuple[bool, str, int]] = {}
    for key, evs in buckets.items():
        latest = max(evs, key=_sort_key)
        task_final_time[key] = _sort_key(latest)

    # The most-recent task (by its final event) keeps draft/final; every
    # earlier task's plans are completed_major.
    latest_task_key = (
        max(task_final_time, key=lambda k: task_final_time[k])
        if task_final_time else None
    )

    plans: List[Plan] = []
    for key, evs in buckets.items():
        ordered = sorted(evs, key=_sort_key)
        final_ev = ordered[-1] if ordered else None
        is_latest_task = key == latest_task_key
        for ev in ordered:
            if not is_latest_task:
                kind = "completed_major"
            elif ev is final_ev:
                kind = "final"
            else:
                kind = "draft"
            refs = tuple(dict(r) for r in ev.get("refs", ()))
            plans.append(Plan(
                id=ev.get("id", ""),
                session_id=ev.get("session_id", ""),
                agent=ev.get("agent", ""),
                title=_plan_ref_value(ev.get("refs", ()), "title") or ev.get("text") or "",
                task_id=key,
                kind=kind,
                path=_plan_ref_value(ev.get("refs", ()), "path"),
                refs=refs,
                sha256=ev.get("sha256", ""),
            ))
    # Return in timeline order (ts, seq) for a stable, chronological result.
    plans.sort(key=lambda p: (
        p.id.rsplit(":", 1)[0],
        int(p.id.rsplit(":", 1)[-1]) if ":" in p.id else -1,
    ))
    return plans


def _plan_to_dict(p: Plan) -> dict[str, Any]:
    return {
        "id": p.id,
        "session_id": p.session_id,
        "agent": p.agent,
        "title": p.title,
        "task_id": p.task_id,
        "kind": p.kind,
        "path": p.path,
        "steps": [dict(s) for s in p.steps] if p.steps else None,
        "status": p.status,
        "refs": [dict(r) for r in p.refs],
        "sha256": p.sha256,
    }


def plan(
    session: Optional[str] = None,
    *,
    kind: Optional[str] = None,
    group: str = "task",
    agent: Optional[str] = None,
) -> List[dict[str, Any]]:
    """Preset: normalized plan atoms for a session (or across sessions).

    Thin wrapper over ``query(type="plan_event", …)`` that groups the
    plan_events into tasks and tags each with ``draft`` | ``final`` |
    ``completed_major`` (see :func:`_assign_plan_kinds`).  ``group="task"``
    (the only supported grouping today) keys by each plan's ``task_key`` —
    the plan-file slug when the agent has one (Claude ``plans/<slug>.md``,
    Antigravity ``implementation_plan.md`` path), falling back to the
    normalized title only when no plan file exists (Codex ``update_plan``).
    The agent-specific plan signal is already normalized away upstream.

    Args:
        session: Restrict to one session uuid (recommended); ``None`` scans
            every session for the agent(s).
        kind: Optional filter — return only plans of this kind
            (``draft`` / ``final`` / ``completed_major``).
        group: Grouping strategy; only ``"task"`` is implemented.
        agent: Optional agent filter.

    Returns:
        A list of normalized plan dicts (see :func:`_plan_to_dict`), in
        timeline order.  Steps/status are carried for Codex plans; bodies
        are never inlined — use :func:`get_body`.
    """
    if group != "task":
        raise ValueError(f"group must be 'task', got {group!r}")
    if kind is not None and kind not in ("draft", "final", "completed_major"):
        raise ValueError(
            f"kind must be draft|final|completed_major or null, got {kind!r}"
        )
    events = query(type="plan_event", session=session, agent=agent)
    plans = _assign_plan_kinds(events)
    # Enrich Codex plans with their steps/status (resolved on demand from the
    # source, mirroring get_body — kept off the bare Event to honour "no body
    # inlined").
    enriched: List[dict[str, Any]] = []
    for p in plans:
        if kind is not None and p.kind != kind:
            continue
        d = _plan_to_dict(p)
        sig = _resolve_plan_signal(p.id)
        if sig is not None:
            if sig.steps:
                d["steps"] = [dict(s) for s in sig.steps]
            if sig.status:
                d["status"] = sig.status
        enriched.append(d)
    return enriched


def _resolve_plan_signal(event_id: str) -> Optional[_PlanSignal]:
    """Re-derive the plan signal backing a ``plan_event`` id.

    ``get_body`` and ``plan`` deliberately keep bodies/steps off the
    :class:`Event`.  To honour a later ``get_body(id)`` we re-read the owning
    session and re-run plan detection, matching the plan_event by its seq
    position among the session's plan signals.  Deterministic: the same
    session yields the same signal order every time.
    """
    if ":" not in event_id:
        return None
    session_id = event_id.rsplit(":", 1)[0]
    # Re-materialize the session's event stream to map this plan_event id to
    # its ordinal among plan_events, then pick the matching signal.
    stream = list(iter_events(session=session_id))
    plan_ids = [e.id for e in stream if e.type == "plan_event"]
    try:
        ordinal = plan_ids.index(event_id)
    except ValueError:
        return None
    # Rebuild the signal list for the owning session/agent.
    owning = next((e for e in stream if e.id == event_id), None)
    if owning is None:
        return None
    for agent_name in target_agents(owning.agent):
        parser = PARSERS[agent_name]
        for sess in parser.list_sessions():
            if sess.uuid != session_id:
                continue
            try:
                messages = parser.read_messages(sess.uuid)
            except (FileNotFoundError, ValueError, OSError):
                return None
            signals = _plan_signals_for_session(
                messages,
                agent=agent_name.value.lower(),
                session_path=getattr(sess, "path", "") or "",
            )
            if 0 <= ordinal < len(signals):
                return signals[ordinal]
            return None
    return None


def get_body(id: str, shallow: bool = False) -> dict[str, Any]:
    """Return the on-demand body for an event / plan id.

    For a ``plan_event`` the body is the full plan text (Claude
    ``ExitPlanMode`` / ``Write plans/*.md`` markdown, Antigravity
    ``implementation_plan.md``) and/or Codex ``steps``.  For a ``user_turn``
    / ``assistant_turn`` the body is the turn text.  Bodies are fetched here
    rather than inlined into :class:`Event`, so a caller pays for them only
    when needed.

    ``shallow`` (plans only): return just the *final* plan of the id's task,
    dropping the bodies of any earlier ``draft`` revisions — the S6 scenario
    where a subagent gets one plan without the noise of superseded drafts.

    Returns:
        ``{"id", "type", "title"?, "body"?, "steps"?, "status"?, "path"?,
        "shallow"}`` for a plan, or ``{"id", "type", "text"}`` for a turn.
        ``{"error": ...}`` when the id cannot be resolved.
    """
    if not id or ":" not in id:
        return {"error": "invalid_argument", "message": f"invalid id {id!r}"}
    session_id = id.rsplit(":", 1)[0]
    stream = list(iter_events(session=session_id))
    event = next((e for e in stream if e.id == id), None)
    if event is None:
        return {"error": "not_found", "id": id}

    if event.type != "plan_event":
        # Turn/tool body: the text already lives on the event.
        return {"id": id, "type": event.type, "text": event.text}

    sig = _resolve_plan_signal(id)
    title = _plan_ref_value(event.refs, "title") or event.text or ""
    path = _plan_ref_value(event.refs, "path")
    result: dict[str, Any] = {
        "id": id,
        "type": "plan_event",
        "title": title,
        "path": path,
        "shallow": bool(shallow),
    }
    if sig is not None:
        result["body"] = sig.body
        if sig.steps:
            result["steps"] = [dict(s) for s in sig.steps]
        if sig.status:
            result["status"] = sig.status

    if shallow:
        # S6: collapse to the task's FINAL plan only; drop draft bodies.
        # Resolve the task the id belongs to, find its final plan, and return
        # that plan's body (never the earlier drafts').
        task_plans = _assign_plan_kinds(
            query(type="plan_event", session=session_id)
        )
        my = next((p for p in task_plans if p.id == id), None)
        if my is not None:
            final = next(
                (p for p in task_plans
                 if p.task_id == my.task_id and p.kind in ("final", "completed_major")),
                None,
            )
            # Prefer the group's final: if the requested id is itself a draft,
            # return the final plan's body and mark the drafts as dropped.
            if final is not None and final.id != id:
                final_sig = _resolve_plan_signal(final.id)
                result["id"] = final.id
                result["title"] = final.title
                result["path"] = final.path
                result["body"] = final_sig.body if final_sig else None
                if final_sig and final_sig.steps:
                    result["steps"] = [dict(s) for s in final_sig.steps]
                if final_sig and final_sig.status:
                    result["status"] = final_sig.status
            result["dropped_drafts"] = [
                p.id for p in task_plans
                if p.task_id == my.task_id and p.kind == "draft"
                and p.id != result["id"]
            ]
    return result


# --- Phase-3a verb: aggregate (rollup over query rows) ---------------------
#
# ``aggregate`` is the generic rollup that reproduces what ``session_stats``
# (group_by ∈ agent|dir|date|kind) and ``file_frequency`` (group_by=file, rank
# by edit count) do today, without re-parsing.  It is *pure*: it never touches
# the filesystem — it folds a list of already-materialized row dicts (the
# output of ``query``, ``find_file_edits``, or a session inventory) into
# ``{groups: [...], totals: {...}}``.  All behaviour is parameters:
#
# * ``group_by`` selects the bucket key (a row field name, or a callable
#   ``row -> str``).
# * ``metrics`` selects which numbers each bucket carries.  Each metric name
#   maps to a reducer over the bucket's rows; unknown names raise ValueError.
#
# The metric reducers are deliberately the SAME semantics the legacy tools
# use so Phase 3b can retarget them onto this verb with byte-identical output:
#
# * ``count``    — number of rows in the bucket.
# * ``sessions`` — distinct ``session_uuid`` | ``session_id`` (falls back to
#   ``count`` of rows when a row carries a pre-counted ``sessions`` int, so a
#   session-inventory row = one session).
# * ``edits``    — SUM of each row's ``edits`` int when present, else the
#   number of rows (an edit-record stream = one edit per row).  This is the
#   union of the ``session_stats`` (pre-summed per session) and
#   ``file_frequency`` (one row per edit) conventions.
# * ``intents``  — distinct count of ``intent`` (str) and/or the union of each
#   row's ``intents`` (iterable of str), stripped, empties skipped.
# * ``agents``   — sorted distinct ``agent`` values.
# * ``messages`` — SUM of each row's ``messages`` | ``message_count`` int.
# * ``files``    — distinct count of ``file``.
#
# ``totals`` carries the same metrics folded over the WHOLE row set (never the
# truncated ``groups``), plus ``sessions``/``agents``/``agents_list`` mirrors
# so the shape lines up with both legacy tools' ``totals`` blocks.


def _row_group_key(row: dict[str, Any], group_by: Any) -> str:
    """Resolve a row's bucket label under ``group_by`` (field name or callable)."""
    if callable(group_by):
        return str(group_by(row))
    val = row.get(group_by)
    if val is None or (isinstance(val, str) and not val):
        return "(unknown)"
    return str(val)


def _metric_sessions(rows: Sequence[dict[str, Any]]) -> int:
    seen: set[str] = set()
    counted = 0
    for r in rows:
        uuid = r.get("session_uuid") or r.get("session_id")
        if isinstance(uuid, str) and uuid:
            seen.add(uuid)
        elif isinstance(r.get("sessions"), int):
            counted += int(r["sessions"])
        else:
            counted += 1
    return len(seen) if seen else counted


def _metric_edits(rows: Sequence[dict[str, Any]]) -> int:
    total = 0
    for r in rows:
        val = r.get("edits")
        if isinstance(val, bool):
            total += 1
        elif isinstance(val, int):
            total += val
        else:
            total += 1
    return total


def _collect_intents(rows: Sequence[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        single = r.get("intent")
        if isinstance(single, str) and single.strip():
            out.add(single.strip())
        many = r.get("intents")
        if isinstance(many, (list, tuple, set)):
            for it in many:
                if isinstance(it, str) and it.strip():
                    out.add(it.strip())
    return out


def _collect_agents(rows: Sequence[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        agent = r.get("agent")
        if isinstance(agent, str) and agent:
            out.add(agent)
        many = r.get("agents")
        if isinstance(many, (list, tuple, set)):
            for a in many:
                if isinstance(a, str) and a:
                    out.add(a)
    return out


def _metric_messages(rows: Sequence[dict[str, Any]]) -> int:
    total = 0
    for r in rows:
        val = r.get("messages")
        if val is None:
            val = r.get("message_count")
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            total += val
    return total


def _metric_files(rows: Sequence[dict[str, Any]]) -> int:
    seen: set[str] = set()
    for r in rows:
        f = r.get("file")
        if isinstance(f, str) and f:
            seen.add(f)
    return len(seen)


# Metric name → (reducer, kind).  ``kind`` shapes the emitted value:
# ``"int"`` scalar, ``"list"`` sorted-distinct-list.
_METRICS: "dict[str, tuple[Any, str]]" = {
    "count": (lambda rows: len(rows), "int"),
    "sessions": (_metric_sessions, "int"),
    "edits": (_metric_edits, "int"),
    "intents": (lambda rows: len(_collect_intents(rows)), "int"),
    "agents": (lambda rows: sorted(_collect_agents(rows)), "list"),
    "messages": (_metric_messages, "int"),
    "files": (_metric_files, "int"),
}


# Note text reused VERBATIM from ``session_stats`` (RISK-4) so a
# ``kind_split=True`` aggregate reproduces its degenerate-split note byte-for-byte.
_KIND_SPLIT_NOTE: str = (
    "kind split is degenerate: no subagent sessions were in scope, so a "
    "group_by='kind' result shows only an 'agent' bucket. This is NOT a "
    "verified 'no subagents' — subagent detection is currently "
    "Claude-only; other agents always report kind='agent'."
)


def aggregate(
    rows: Sequence[dict[str, Any]],
    *,
    group_by: Any,
    metrics: Sequence[str] = ("count",),
    rank_by: str = "default",
    kind_split: bool = False,
) -> dict[str, Any]:
    """Roll a list of row dicts up by ``group_by`` — the generic stats verb.

    Reproduces ``session_stats`` (``group_by`` ∈ ``agent``/``dir``/``date``/
    ``kind`` over a session-inventory row stream) and ``file_frequency``
    (``group_by="file"`` over a ``find_file_edits`` record stream) without
    re-parsing — it is a pure fold over already-materialized rows.

    Args:
        rows: The row dicts to fold (``query`` output, ``find_file_edits``
            records, or a session inventory).
        group_by: The bucket key — a row field name (str) or a callable
            ``row -> str``.  Missing/empty values bucket under ``"(unknown)"``.
        metrics: Which numbers each bucket carries.  One or more of
            ``count`` / ``sessions`` / ``edits`` / ``intents`` / ``agents`` /
            ``messages`` / ``files`` (see the module-level table).  Unknown
            names raise :class:`ValueError`.
        rank_by: Group ordering.  ``"default"`` (edits desc, sessions desc,
            count desc, label asc — the ``file_frequency`` order) or
            ``"stats"`` (sessions desc, edits desc, label asc — the
            ``session_stats`` order).  The two differ whenever a
            more-sessions bucket has fewer edits than a fewer-sessions bucket,
            which is why ``session_stats`` needs its own rank to delegate.
        kind_split: When ``True``, add the ``session_stats`` RISK-4 fields —
            ``kind_split_available`` (``True`` iff any row's ``kind`` is
            ``"subagent"``) and, when ``False``, a human-readable ``note``
            (verbatim from ``session_stats``) so a degenerate kind split is
            never misread as a verified "no subagents".

    Returns:
        ``{"group_by": <label>, "groups": [{"group", <metrics...>}],
        "totals": {<metrics...>, "sessions", "agents", "agents_list"}}``,
        plus ``kind_split_available`` / ``note`` when ``kind_split=True``.
        ``totals`` fold over the WHOLE row set (never a truncated group list).

    Raises:
        ValueError: on an unknown metric name or ``rank_by`` value.
    """
    if rank_by not in ("default", "stats"):
        raise ValueError(
            f"rank_by must be 'default' or 'stats', got {rank_by!r}"
        )
    metric_list = list(metrics) if metrics else ["count"]
    for name in metric_list:
        if name not in _METRICS:
            raise ValueError(
                f"unknown metric {name!r}; expected one of {sorted(_METRICS)}"
            )

    buckets: "OrderedDictType[str, List[dict[str, Any]]]" = _OrderedDict()
    for row in rows:
        key = _row_group_key(row, group_by)
        buckets.setdefault(key, []).append(row)

    def _row_metrics(bucket_rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in metric_list:
            reducer, _kind = _METRICS[name]
            out[name] = reducer(bucket_rows)
        return out

    group_rows: List[dict[str, Any]] = []
    for label, bucket_rows in buckets.items():
        entry: dict[str, Any] = {"group": label}
        entry.update(_row_metrics(bucket_rows))
        group_rows.append(entry)

    # Rank.  ``default`` = edits desc, sessions desc, count desc, label asc
    # (the ``file_frequency`` order).  ``stats`` = sessions desc, edits desc,
    # label asc (the ``session_stats`` order) — these disagree whenever a
    # more-sessions bucket has fewer edits.
    def _default_rank(g: dict[str, Any]) -> Tuple[int, int, int, str]:
        edits = g.get("edits", 0) if isinstance(g.get("edits"), int) else 0
        sessions = g.get("sessions", 0) if isinstance(g.get("sessions"), int) else 0
        count = g.get("count", 0) if isinstance(g.get("count"), int) else 0
        return (-edits, -sessions, -count, g["group"])

    def _stats_rank(g: dict[str, Any]) -> Tuple[int, int, str]:
        sessions = g.get("sessions", 0) if isinstance(g.get("sessions"), int) else 0
        edits = g.get("edits", 0) if isinstance(g.get("edits"), int) else 0
        return (-sessions, -edits, g["group"])

    group_rows.sort(key=_stats_rank if rank_by == "stats" else _default_rank)

    totals: dict[str, Any] = {}
    for name in metric_list:
        reducer, _kind = _METRICS[name]
        totals[name] = reducer(rows)
    # Always surface the session/agent totals the legacy ``totals`` blocks
    # carry, even when not requested as a group metric.
    if "sessions" not in totals:
        totals["sessions"] = _metric_sessions(rows)
    agents_all = sorted(_collect_agents(rows))
    totals["agents"] = len(agents_all)
    totals["agents_list"] = agents_all

    label = group_by if isinstance(group_by, str) else "custom"
    result: dict[str, Any] = {
        "group_by": label, "groups": group_rows, "totals": totals,
    }
    if kind_split:
        # RISK-4: honesty flag + degenerate-split note, matching session_stats.
        subagent_seen = any(
            isinstance(r.get("kind"), str) and r["kind"] == "subagent"
            for r in rows
        )
        result["kind_split_available"] = subagent_seen
        if not subagent_seen:
            result["note"] = _KIND_SPLIT_NOTE
    return result


# --- Phase-3a verb: diff (stitch edit rows into a per-file unified diff) ----
#
# ``diff`` reproduces the synthesis of ``session_diff``: given the edit events
# for a session (``query(type="tool_call(edit)", session=…)`` — plus write /
# shell-redirect events), it groups them per file in chronological order and
# renders a stitched, readable hunk-by-hunk diff.  Bodies (``old_string`` /
# ``new_string`` / ``content`` / shell ``cmd``) are NOT inlined on the Event —
# ``diff`` fetches them on demand via :func:`get_body`, so this verb pays for
# the payload only when it stitches.
#
# The per-hunk rendering + caveats are imported verbatim from
# :mod:`ai_r.session_diff` (``_hunk_from_tool`` / ``_render_hunk`` /
# ``_GIT_CAVEAT`` / ``_RISK3_CAVEAT``) so the stitched output is byte-identical
# to the legacy tool and there is a single source of truth for what an edit
# hunk looks like.


def _edit_input_from_event(event_id: str) -> Tuple[str, dict[str, Any]]:
    """Re-resolve ``(tool_name, parsed_input_obj)`` for one edit event id.

    ``diff`` gets its edit rows from ``query`` whose Events carry only the raw
    tool NAME + refs (no body).  To stitch a real hunk we re-read the owning
    session, find the tool_use at the event's ``message_index`` matching the
    referenced file, and shape its input exactly like ``session_diff`` does
    (parse JSON, recover codex shell-redirect ``{cmd, edit}``).  Returns
    ``("", {})`` when the event/tool cannot be resolved.
    """
    from ai_r.find_file_edits import (
        _SHELL_EXEC_TOOLS,
        _extract_shell_command,
        _shell_redirect_targets,
    )

    if ":" not in event_id:
        return "", {}
    session_id = event_id.rsplit(":", 1)[0]
    stream = list(iter_events(session=session_id))
    event = next((e for e in stream if e.id == event_id), None)
    if event is None:
        return "", {}
    target_file = _plan_ref_value(event.refs, "file")
    tool_name = _plan_ref_value(event.refs, "tool") or event.text or ""

    for agent_name in target_agents(event.agent):
        parser = PARSERS[agent_name]
        for sess in parser.list_sessions():
            if sess.uuid != session_id:
                continue
            try:
                messages = parser.read_messages(sess.uuid)
            except (FileNotFoundError, ValueError, OSError):
                return tool_name, {}
            if not (0 <= event.message_index < len(messages)):
                return tool_name, {}
            msg = messages[event.message_index]
            for tool in getattr(msg, "tool_use", ()) or ():
                if not isinstance(tool, dict):
                    continue
                if tool.get("name", "") != tool_name:
                    continue
                if tool.get("name", "") in _SHELL_EXEC_TOOLS:
                    cmd = _extract_shell_command(tool.get("input", ""))
                    for fpath, append in _shell_redirect_targets(cmd):
                        if target_file is None or fpath == target_file:
                            return tool_name, {
                                "cmd": cmd,
                                "edit": "append" if append else "write",
                            }
                    continue
                payload = _coerce_tool_input(tool.get("input", ""))
                if isinstance(payload, dict):
                    # For the plain edit tools the whole parsed input carries
                    # the hunk shape (old_string/new_string/content/edits).
                    if target_file is None or _path_from_payload(payload) == target_file:
                        return tool_name, payload
            return tool_name, {}
    return tool_name, {}


def diff(
    rows: Sequence[dict[str, Any]],
    *,
    per_file: bool = True,
    format: str = "unified",
) -> dict[str, Any]:
    """Stitch edit rows into a per-file chronological diff — the diff verb.

    Reproduces the synthesis of :func:`ai_r.session_diff.session_diff`: given
    the edit events of a session (``query(type="tool_call(edit)",
    session=…)`` — plus ``write`` / shell-redirect events), group them per
    file in chronological order and render a stitched, readable diff.  Bodies
    are fetched on demand via :func:`get_body` (through the event's stored
    ``message_index``), never inlined on the row.

    Args:
        rows: Edit-event dicts (``query`` output).  Each must carry an ``id``
            (``"{session}:{seq}"``) and a ``refs`` list with a ``file`` entry;
            rows without a resolvable file are skipped.
        per_file: Group by file (the only mode today; ``False`` still groups
            per file but is reserved for a future flat mode).
        format: ``"unified"`` (the only rendering today).  Any other value
            raises :class:`ValueError`.

    Returns:
        ``{"files": [{"file", "edits", "diff", "hunks"}], "count": N,
        "caveats": [...]}`` — the same shape + caveats as
        :func:`session_diff`, with an added flat ``hunks`` list per file.

    Raises:
        ValueError: on an unsupported ``format``.
    """
    from ai_r.session_diff import (
        _GIT_CAVEAT,
        _RISK3_CAVEAT,
        _hunk_from_tool,
        _render_hunk,
    )

    if format != "unified":
        raise ValueError(f"format must be 'unified', got {format!r}")

    # Build ordered (file, edit) events from the rows, mirroring the shaping
    # ``session_diff._scan_session`` produces.
    events: List[dict[str, Any]] = []
    for row in rows:
        event_id = row.get("id")
        if not isinstance(event_id, str) or ":" not in event_id:
            continue
        refs = row.get("refs", ()) or ()
        fpath = _plan_ref_value(refs, "file")
        ts = row.get("ts")
        try:
            seq = int(event_id.rsplit(":", 1)[-1])
        except ValueError:
            seq = -1
        tool_name, input_obj = _edit_input_from_event(event_id)
        # For a shell-redirect event the resolved file lives on the {cmd,edit}
        # shape; fall back to the ref file (or the redirect target).
        if fpath is None and isinstance(input_obj, dict) and "cmd" in input_obj:
            fpath = _plan_ref_value(refs, "file")
        if not fpath:
            continue
        events.append({
            "file": fpath,
            "timestamp": ts,
            "seq": seq,
            "intent": row.get("intent"),
            "tool": tool_name or (row.get("text") or ""),
            "hunks": _hunk_from_tool(tool_name, input_obj),
        })

    # Group per file, preserving chronological order within each file.
    by_file: "OrderedDictType[str, List[dict[str, Any]]]" = _OrderedDict()
    for ev in events:
        by_file.setdefault(ev["file"], []).append(ev)

    files: List[dict[str, Any]] = []
    for fpath, evs in by_file.items():
        ordered = sorted(
            evs,
            key=lambda e: (e["timestamp"] is None, e["timestamp"] or "", e["seq"]),
        )
        diff_blocks: List[str] = []
        edits_out: List[dict[str, Any]] = []
        all_hunks: List[dict[str, Any]] = []
        for ev in ordered:
            edits_out.append({
                "timestamp": ev["timestamp"],
                "intent": ev["intent"],
                "tool": ev["tool"],
                "hunks": ev["hunks"],
            })
            all_hunks.extend(ev["hunks"])
            header = f"@@ {ev['timestamp'] or '(no ts)'} {ev['tool']} @@"
            rendered = "\n".join(_render_hunk(h) for h in ev["hunks"])
            diff_blocks.append(f"{header}\n{rendered}")
        files.append({
            "file": fpath,
            "edits": edits_out,
            "diff": "\n".join(diff_blocks),
            "hunks": all_hunks,
        })

    return {
        "files": files,
        "count": len(files),
        "caveats": [_GIT_CAVEAT, _RISK3_CAVEAT],
    }


# --- Phase-3a verb: detect_current (runtime identity, env/fs) ---------------
#
# ``detect_current`` is NOT a session-query — it answers "who am I RIGHT NOW?"
# from the runtime environment (env vars + per-session flag files).  It is a
# thin re-export/composition of the existing ``ai_r.agents.detect_agent`` +
# ``ai_r.session.detect_session_candidates`` cascade (the same logic behind the
# ``ai-r detect-agent`` / ``ai-r detect-session`` CLI subcommands), reshaped
# into a single ``{session_id, agent, candidates, verified, self}`` dict.


def detect_current(agent: Optional[str] = None) -> dict[str, Any]:
    """Return the current runtime identity (session + agent) from env/fs.

    NOT a session-query — this reads the runtime environment (env vars +
    per-session flag files), reusing the exact cascade behind the
    ``ai-r detect-agent`` / ``ai-r detect-session`` CLI subcommands:
    :func:`ai_r.agents.detect_agent` for the agent and
    :func:`ai_r.session.detect_session_candidates` for the session id(s).

    Args:
        agent: Optional hint; accepted for API symmetry with the CLI's
            deprecated ``--agent`` flag.  The cascade scans every agent
            regardless, so this only overrides the reported ``agent`` when the
            session cascade yields no agent context.

    Returns:
        ``{"session_id": str|None, "agent": str|None, "candidates": [...],
        "verified": bool, "self": bool}`` where:

        * ``session_id`` / ``agent`` describe the FIRST (highest-priority)
          candidate — the same one the CLI's default ``list`` mode returns.
        * ``candidates`` is the full cascade (each ``{id, agent, source,
          verified, self, fingerprint}``), so a caller can disambiguate.
        * ``verified`` / ``self`` mirror the first candidate's flags.
    """
    from ai_r.agents import detect_agent as _detect_agent
    from ai_r.parsers import coerce_agent as _coerce_agent
    from ai_r.session import detect_session_candidates as _detect_candidates

    hint: Optional[str] = None
    if agent:
        # Validate the hint the same way the CLI does; an unknown agent is a
        # caller error, surfaced as ValueError (MCP wrapper → error dict).
        hint = _coerce_agent(agent).value.lower()

    candidates = _detect_candidates()
    candidate_dicts: List[dict[str, Any]] = [
        {
            "id": c.session_id,
            "agent": c.agent.value.lower() if c.agent is not None else "",
            "source": c.source,
            "verified": c.verified,
            "self": c.is_self,
            "fingerprint": c.fingerprint if c.fingerprint is not None else "",
        }
        for c in candidates
    ]

    env_agent = _detect_agent()
    env_agent_str = env_agent.value.lower() if env_agent is not None else None

    first = candidates[0] if candidates else None
    session_id = first.session_id if first is not None else None
    # Agent of record: the first candidate's agent, else the env-detected
    # agent, else the caller's hint.
    if first is not None and first.agent is not None:
        agent_str: Optional[str] = first.agent.value.lower()
    elif env_agent_str is not None:
        agent_str = env_agent_str
    else:
        agent_str = hint

    return {
        "session_id": session_id,
        "agent": agent_str,
        "candidates": candidate_dicts,
        "verified": first.verified if first is not None else False,
        "self": first.is_self if first is not None else False,
    }
