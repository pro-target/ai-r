"""Event stream construction — plan-signal detection + ``iter_events``.

Normalises each parser's :class:`~ai_r.parsers.models.Message` objects (plus
their embedded ``tool_use`` calls) into a flat, chronological sequence of
:class:`~ai_r.events._common.Event` records, and detects the agent-specific
*plan signals* that become ``plan_event`` records.

The plan-signal machinery (``_PlanSignal`` + ``_plan_signals_for_session`` +
``_normalize_task_key``) lives here because both this module (which emits the
``plan_event`` inline in the stream) and :mod:`ai_r.events.plan` (which
re-derives a signal for ``get_body`` / step enrichment) need it — keeping it in
one place below ``iter_events`` avoids an intra-package cycle.

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from ai_r.find_file_edits import to_utc_aware
from ai_r.parsers import PARSERS, Message, iso, target_agents
from ai_r.parsers._common import project_dir_matches
from ai_r.parsers._noise import noise_allows, validate_noise

from ai_r.events._common import (
    Event,
    _coerce_tool_input,
    _mk_event,
    _path_from_payload,
    classify_tool,
    resolve_tool,
)


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
                # Codex ``update_plan`` carries the step array under the
                # ``plan`` key (verified across the vault); ``steps`` is kept
                # only as a defensive fallback for any other shape.
                raw_steps = payload.get("plan")
                if not isinstance(raw_steps, list):
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
    carry no turn text of their own and are skipped as standalone events,
    but their ``tool_result`` entries are correlated back to the owning
    ``tool_call`` (by ``tool_use_id``) so the call event carries an
    ``is_error`` ref — the success/error outcome is thus visible on the
    existing ``tool_call`` events WITHOUT introducing a new event type
    (so ``type`` filters and event counts are unchanged).
    """
    events: List[Event] = []
    seq = 0
    session_iso = iso(session_ts) if session_ts is not None else None

    # Correlate tool_result outcomes back to their calls by ``tool_use_id``.
    # Only ids that appear on a result are recorded; a call whose id is not
    # in this map simply carries no outcome (unknown / no error signal —
    # e.g. Codex/Pi/Antigravity, which expose no per-result flag).
    error_by_tool_id: dict[str, bool] = {}
    for _m in messages:
        for _tr in getattr(_m, "tool_result", ()) or ():
            if not isinstance(_tr, dict):
                continue
            _tid = _tr.get("tool_use_id")
            if isinstance(_tid, str) and _tid:
                error_by_tool_id[_tid] = bool(_tr.get("is_error"))

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
                payload = _coerce_tool_input(tool.get("input", ""))
                refs: List[dict] = [{"tool": name}]
                fpath = _path_from_payload(payload)
                if fpath:
                    refs.append({"file": fpath})
                # F3.1: classify the call (wrapper-aware) and surface the
                # real name under a Skill/Task/MCP wrapper.  ``tool_kind``
                # is always present; ``tool_resolved`` only when the input
                # actually carries the real name (honest — never guessed).
                # The event ``type`` keeps the classify_tool subtype for
                # backward-compat (a Task call stays ``tool_call(other)``).
                kind, resolved = resolve_tool(name, payload)
                refs.append({"tool_kind": kind})
                if resolved:
                    refs.append({"tool_resolved": resolved})
                # Surface the call's outcome when a correlated result exists:
                # ``{"is_error": True|False}``.  Absent when no matching
                # result id was seen (outcome unknown / agent has no signal).
                tu_id = tool.get("tool_use_id")
                if isinstance(tu_id, str) and tu_id in error_by_tool_id:
                    refs.append({"is_error": error_by_tool_id[tu_id]})
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
    noise: str = "include",
    project_dir: Optional[str] = None,
    scanned_sessions_out: Optional[dict[str, Any]] = None,
) -> Iterable[Event]:
    """Yield the normalized Event stream across sessions, cross-agent.

    Args:
        agent: Optional agent filter (``claude``/``codex``/...); ``None``
            = every agent.
        session: Optional session-uuid filter; restrict the scan to a
            single session (cheap fast-path for ``relative_to`` walks).
        noise: Session-level noise filter (see
            :mod:`ai_r.parsers._noise`): ``"include"`` (default, no
            filtering), ``"exclude"`` (drop subagent sessions), ``"only"``
            (keep only subagent sessions).  Applied *before* reading
            messages, so excluded sessions cost nothing.
        project_dir: Session-level project filter — keep only sessions
            whose ``Session.project_dir`` equals this path or is a
            descendant of it (path-boundary aware, see
            :func:`ai_r.parsers._common.project_dir_matches`); sessions
            without a ``project_dir`` signal never match.  Like ``noise``,
            applied *before* any message is read.
        scanned_sessions_out: Optional out-parameter — a dict the caller
            owns, filled with ``{agent_label: list_sessions() result}`` as
            each agent is scanned.  Lets the caller reuse the enumeration
            (e.g. for empty-result diagnostics) instead of paying for a
            second full corpus walk.  Complete only once the generator is
            exhausted.

    Yields:
        :class:`Event` records in per-session, chronological (parse)
        order.  Sessions that fail to read are skipped (an audit tool
        prefers a partial view to a crash), mirroring ``find_file_edits``.
    """
    validate_noise(noise)
    for agent_name in target_agents(agent):
        parser = PARSERS[agent_name]
        agent_lc = agent_name.value.lower()
        sessions = parser.list_sessions()
        if scanned_sessions_out is not None:
            scanned_sessions_out[agent_lc] = sessions
        for sess in sessions:
            if session is not None and sess.uuid != session:
                continue
            if not noise_allows(sess, noise):
                continue
            if project_dir is not None and not project_dir_matches(
                getattr(sess, "project_dir", None), project_dir
            ):
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


# ``Message`` is re-exported so downstream modules that build the enrichment
# message cache can import the parser type from here alongside the stream.
__all__ = ["Event", "Message", "iter_events"]
