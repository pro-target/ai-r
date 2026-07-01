"""Plan atom + task grouping + ``get_body`` (the plan preset core).

Groups the ``plan_event`` records emitted by :mod:`ai_r.events.model` into
tasks, tags each ``draft`` / ``final`` / ``completed_major``, and resolves
on-demand bodies/steps (kept OFF the bare :class:`Event`, honouring
"no body inlined").

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

from collections import OrderedDict as _OrderedDict
from dataclasses import dataclass
from typing import (
    Any,
    List,
    Optional,
    OrderedDict as OrderedDictType,
    Sequence,
    Tuple,
)

from ai_r.parsers import PARSERS, target_agents

from ai_r.events._common import _plan_ref_value
from ai_r.events.model import (
    _PlanSignal,
    _normalize_task_key,
    _plan_signals_for_session,
    iter_events,
)
from ai_r.events.query import query


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
