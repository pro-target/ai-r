"""Plan atom + task grouping + ``get_body`` (the plan preset core).

Groups the ``plan_event`` records emitted by :mod:`ai_r.events.model` into
tasks, tags each ``draft`` / ``final`` / ``completed_major``, and resolves
on-demand bodies/steps (kept OFF the bare :class:`Event`, honouring
"no body inlined").

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

import re
from collections import OrderedDict as _OrderedDict
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Optional,
    OrderedDict as OrderedDictType,
    Sequence,
    Tuple,
)

from ai_r.parsers import PARSERS, target_agents
from ai_r.redact import merge_redaction_counts, redact_value

from ai_r.events._common import _plan_ref_value
from ai_r.events.model import (
    _PlanResponse,
    _PlanSignal,
    _anchor_quote_to_section,
    _normalize_task_key,
    _plan_responses_for_session,
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
        version: 1-based revision number within the task group, in
            chronological ``(ts, seq)`` order (F3.4 v2) — drafts are
            ``v1…vN-1``, the final is ``vN``; numbering restarts per task.
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
    version: int = 1
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
        # F3.4 v2: 1-based revision numbering per task group, chronological —
        # drafts are v1…vN-1, the final is vN (restarts for every task).
        for version, ev in enumerate(ordered, start=1):
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
                version=version,
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
        "version": p.version,
        "path": p.path,
        "steps": [dict(s) for s in p.steps] if p.steps else None,
        "status": p.status,
        "refs": [dict(r) for r in p.refs],
        "sha256": p.sha256,
    }


def _session_plan_context(
    session_id: str, agent_hint: Optional[str]
) -> Tuple[List[_PlanSignal], List[_PlanResponse]]:
    """Read one session ONCE and derive its plan signals + user responses.

    The signal list is index-aligned with the session's ``plan_event`` ids
    (both are emitted in the same order — the invariant
    :func:`_resolve_plan_signal` already relies on); the response list index
    is the stable ``pf<N>`` ordinal.  Unknown/unreadable session → two empty
    lists (an audit tool prefers a partial view to a crash).
    """
    for agent_name in target_agents(agent_hint):
        parser = PARSERS[agent_name]
        for sess in parser.list_sessions():
            if sess.uuid != session_id:
                continue
            try:
                messages = parser.read_messages(sess.uuid)
            except (FileNotFoundError, ValueError, OSError):
                return [], []
            agent_lc = agent_name.value.lower()
            signals = _plan_signals_for_session(
                messages,
                agent=agent_lc,
                session_path=getattr(sess, "path", "") or "",
            )
            responses = _plan_responses_for_session(messages, agent=agent_lc)
            return signals, responses
    return [], []


class _PlanContextCache:
    """Per-call cache: one message read per session, shared by all lookups."""

    def __init__(self) -> None:
        self._ctx: Dict[str, Tuple[List[_PlanSignal], List[_PlanResponse]]] = {}

    def get(
        self, session_id: str, agent_hint: Optional[str]
    ) -> Tuple[List[_PlanSignal], List[_PlanResponse]]:
        if session_id not in self._ctx:
            self._ctx[session_id] = _session_plan_context(
                session_id, agent_hint
            )
        return self._ctx[session_id]


def _plan_ids_by_session(
    events: Sequence[dict[str, Any]],
) -> "OrderedDictType[str, List[str]]":
    """Group the queried plan_event ids per session, preserving order."""
    grouped: "OrderedDictType[str, List[str]]" = _OrderedDict()
    for ev in events:
        sid = ev.get("session_id") or ""
        grouped.setdefault(sid, []).append(ev.get("id", ""))
    return grouped


def _approved_edited_body(
    sig: Optional[_PlanSignal], responses: Sequence[_PlanResponse]
) -> Optional[str]:
    """The user-edited approved text answering ``sig``'s call, if any.

    The approval tool_result is the AUTHORITATIVE final text (the plan file
    on disk can diverge from what the user actually approved — audited on
    real vaults), so it overrides the signal body when present.
    """
    if sig is None or not sig.tool_use_id:
        return None
    for resp in responses:
        if (
            resp.verdict == "approved"
            and resp.tool_use_id == sig.tool_use_id
            and resp.edited_body
        ):
            return resp.edited_body
    return None


def plan(
    session: Optional[str] = None,
    *,
    kind: Optional[str] = None,
    group: str = "task",
    agent: Optional[str] = None,
    bodies: str = "final",
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
        bodies: ``"final"`` (default, F3.4) inlines the full text of each
            ``final`` plan as ``body`` + ``body_source`` — the one plan a
            consumer almost always needs, per the measured default schema;
            ``"none"`` restores the historical reference-only shape.
            Draft/major bodies are NEVER inlined — use :func:`get_body`.

    Returns:
        A list of normalized plan dicts (see :func:`_plan_to_dict`), in
        timeline order.  Every atom carries ``version`` — its 1-based
        chronological revision number within the task group (F3.4 v2:
        drafts are ``v1…vN-1``, the final is ``vN``).  Steps/status are
        carried for Codex plans.  With ``bodies="final"`` the ``final``
        atom carries ``body`` (honest ``None`` when the signal has no
        text, e.g. a steps-only Codex plan) and ``body_source`` —
        ``"approval_edited_by_user"`` when the authoritative user-edited
        approval text overrides the signal body, else ``"plan_signal"``.
    """
    if group != "task":
        raise ValueError(f"group must be 'task', got {group!r}")
    if kind is not None and kind not in ("draft", "final", "completed_major"):
        raise ValueError(
            f"kind must be draft|final|completed_major or null, got {kind!r}"
        )
    if bodies not in ("final", "none"):
        raise ValueError(f"bodies must be 'final' or 'none', got {bodies!r}")
    # ``redact=False``: internal call — the public wrappers apply the single
    # emission-time redaction pass on their own final output (F2.1).
    events = query(type="plan_event", session=session, agent=agent, redact=False)
    plans = _assign_plan_kinds(events)
    ids_by_session = _plan_ids_by_session(events)
    agent_by_session = {
        ev.get("session_id") or "": ev.get("agent") for ev in events
    }
    cache = _PlanContextCache()
    # Enrich Codex plans with their steps/status (resolved on demand from the
    # source, one message read per session) and — F3.4 — inline the final
    # plan's body.
    enriched: List[dict[str, Any]] = []
    for p in plans:
        if kind is not None and p.kind != kind:
            continue
        d = _plan_to_dict(p)
        signals, responses = cache.get(
            p.session_id, agent_by_session.get(p.session_id)
        )
        session_ids = ids_by_session.get(p.session_id, [])
        try:
            ordinal = session_ids.index(p.id)
        except ValueError:
            ordinal = -1
        sig = (
            signals[ordinal] if 0 <= ordinal < len(signals) else None
        )
        if sig is not None:
            if sig.steps:
                d["steps"] = [dict(s) for s in sig.steps]
            if sig.status:
                d["status"] = sig.status
        if bodies == "final" and p.kind == "final":
            edited = _approved_edited_body(sig, responses)
            if edited is not None:
                d["body"] = edited
                d["body_source"] = "approval_edited_by_user"
            else:
                d["body"] = sig.body if sig is not None else None
                d["body_source"] = (
                    "plan_signal" if d["body"] is not None else None
                )
        enriched.append(d)
    return enriched


def plan_feedback(
    session: Optional[str] = None,
    *,
    agent: Optional[str] = None,
    rounds: str = "all",
) -> List[dict[str, Any]]:
    """All «plan quote → user comment» pairs for a session's plan iterations.

    F3.4: extracts every pair from the user's plan responses (rejections and
    stay-in-plan-mode comments), chronological.  Each pair carries:

    * ``plan_id`` — the ``plan_event`` id of the revision the response
      answered (``None`` when the transcript has no call-id correlation);
    * ``plan_version`` — the 1-based revision number (``v1…vN``) of that
      revision within its task group (v2; ``None`` without correlation);
    * ``verdict`` — ``rejected`` | ``stay_in_plan_mode``;
    * ``round`` — 1-based feedback-round number within the session, one
      round per user response that produced pairs (v2);
    * ``quote`` — the plan excerpt the user selected (``None`` for a
      free-text comment with no selection);
    * ``comment`` — the user's words, verbatim;
    * ``section`` — the heading of the plan section the quote anchors to
      (v2).  The quote is selected from the RENDERED plan, so both sides
      are compared through the same markup-stripping normalization; a miss
      or an ambiguous (multi-section) match is an honest ``None``, never a
      nearest guess;
    * ``ref`` — a ``"<session_id>:pf<N>"`` id; :func:`get_body` on it
      returns the FULL raw response the pair came from (reference-by-default:
      the raw blob is on-demand);
    * ``session_id`` / ``agent`` / ``ts``.

    ``rounds="all"`` (default) returns every round; ``"last"`` keeps only
    each session's final feedback round (v2).

    Only agents with an interactive plan-approval flow have the signal
    (today: Claude — both ``ExitPlanMode`` and a rejected plan-file
    ``Write`` carry it); other agents honestly contribute nothing.
    Technical failures (permission-stream errors) and bare no-comment
    rejections are filtered out.
    """
    if rounds not in ("all", "last"):
        raise ValueError(f"rounds must be 'all' or 'last', got {rounds!r}")
    events = query(type="plan_event", session=session, agent=agent, redact=False)
    ids_by_session = _plan_ids_by_session(events)
    agent_by_session = {
        ev.get("session_id") or "": ev.get("agent") for ev in events
    }
    # v2: revision numbers ride on the pairs — one grouping pass gives the
    # per-task v1…vN map (same numbering the plan atoms carry).
    version_by_id = {p.id: p.version for p in _assign_plan_kinds(events)}
    cache = _PlanContextCache()
    rows: List[dict[str, Any]] = []
    for sid, plan_ids in ids_by_session.items():
        signals, responses = cache.get(sid, agent_by_session.get(sid))
        call_to_plan_id: Dict[str, str] = {}
        sig_by_call: Dict[str, _PlanSignal] = {}
        for i, sig in enumerate(signals):
            if sig.tool_use_id and i < len(plan_ids):
                call_to_plan_id[sig.tool_use_id] = plan_ids[i]
                sig_by_call[sig.tool_use_id] = sig
        session_rows: List[dict[str, Any]] = []
        round_no = 0
        for ordinal, resp in enumerate(responses):
            if resp.verdict == "approved":
                continue
            if not resp.pairs:
                continue
            round_no += 1  # one round per pair-bearing user response
            ref = f"{sid}:pf{ordinal}"
            plan_id = call_to_plan_id.get(resp.tool_use_id)
            answered = sig_by_call.get(resp.tool_use_id)
            answered_body = answered.body if answered is not None else None
            for quote, comment in resp.pairs:
                session_rows.append({
                    "session_id": sid,
                    "agent": agent_by_session.get(sid),
                    "plan_id": plan_id,
                    "plan_version": version_by_id.get(plan_id) if plan_id is not None else None,
                    "verdict": resp.verdict,
                    "round": round_no,
                    "quote": quote,
                    "comment": comment,
                    "section": _anchor_quote_to_section(quote, answered_body),
                    "ref": ref,
                    "ts": resp.ts,
                })
        if rounds == "last" and round_no:
            session_rows = [
                r for r in session_rows if r["round"] == round_no
            ]
        rows.extend(session_rows)
    return rows


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


# Default char cap on a returned ``get_body`` body/text.  Generous — normal
# plan bodies and turn texts are far smaller, so ordinary fetches are never
# cut; this only bounds a pathological/adversarial multi-MB body.  A value <= 0
# disables the cap.
_BODY_CHARS_CAP = 500_000


def _cap_body(text: object, max_chars: int) -> tuple[object, bool]:
    """Return ``(text, truncated)`` bounding a body/text to ``max_chars``.

    Only ``str`` values are capped; a value <= 0 disables the cap.  An
    over-cap string is sliced with a trailing ``…[truncated]`` marker.
    """
    if max_chars and max_chars > 0 and isinstance(text, str) and len(text) > max_chars:
        return text[:max_chars] + "…[truncated]", True
    return text, False


# A plan-feedback ref minted by :func:`plan_feedback`:
# ``"<session_id>:pf<ordinal>"`` — the ordinal indexes the session's plan
# responses (message order, deterministic).  Distinct from event ids, whose
# trailing segment is purely numeric.
_FEEDBACK_ID_RE = re.compile(r"^(?P<sid>.+):pf(?P<ord>\d+)$")


def _feedback_body(
    session_id: str,
    ordinal: int,
    *,
    max_chars: int,
    redact: bool,
) -> dict[str, Any]:
    """Resolve a ``"<session>:pf<N>"`` ref to the FULL raw plan response.

    The default ``plan_feedback`` rows carry only the extracted pairs; this
    is the on-demand escape hatch to the verbatim response blob (all pairs,
    boilerplate and surrounding words included).
    """
    ref = f"{session_id}:pf{ordinal}"
    stream = list(iter_events(session=session_id))
    if not stream:
        return {"error": "not_found", "id": ref}
    plan_ids = [e.id for e in stream if e.type == "plan_event"]
    signals, responses = _session_plan_context(session_id, stream[0].agent)
    if not (0 <= ordinal < len(responses)):
        return {"error": "not_found", "id": ref}
    resp = responses[ordinal]
    plan_id: Optional[str] = None
    for i, sig in enumerate(signals):
        if sig.tool_use_id == resp.tool_use_id and i < len(plan_ids):
            plan_id = plan_ids[i]
            break
    text, truncated = _cap_body(resp.raw, max_chars)
    result: dict[str, Any] = {
        "id": ref,
        "type": "plan_feedback",
        "verdict": resp.verdict,
        "plan_id": plan_id,
        "text": text,
        "pairs": [
            {"quote": quote, "comment": comment}
            for quote, comment in resp.pairs
        ],
        "ts": resp.ts,
    }
    if truncated:
        result["body_truncated"] = True
    return _redact_body_fields(result, redact)


def get_body(
    id: str,
    shallow: bool = False,
    *,
    max_chars: int = _BODY_CHARS_CAP,
    redact: bool = True,
) -> dict[str, Any]:
    """Return the on-demand body for an event / plan / plan-feedback id.

    For a ``plan_event`` the body is the full plan text (Claude
    ``ExitPlanMode`` / ``Write plans/*.md`` markdown, Antigravity
    ``implementation_plan.md``) and/or Codex ``steps``.  For a ``user_turn``
    / ``assistant_turn`` the body is the turn text.  For a plan-feedback ref
    (``"<session>:pf<N>"``, minted by :func:`plan_feedback`) the body is the
    FULL raw user response the pairs were extracted from.  Bodies are
    fetched here rather than inlined into :class:`Event`, so a caller pays
    for them only when needed.

    ``shallow`` (plans only): return just the *final* plan of the id's task,
    dropping the bodies of any earlier ``draft`` revisions — the S6 scenario
    where a subagent gets one plan without the noise of superseded drafts.

    ``max_chars`` bounds the returned ``body``/``text``; when it trips the
    field is sliced with a ``…[truncated]`` marker and ``body_truncated: true``
    is set.  The default (500_000) is generous — ordinary bodies are never
    cut; pass ``0`` to disable.

    ``redact`` (default ``True``) masks secrets in the emitted
    ``text``/``body``/``title``/``steps`` as ``[REDACTED_<TYPE>]`` and adds
    a ``redactions`` type→count dict when any replacement happened (see
    :mod:`ai_r.redact`); ``False`` returns the raw content.

    Returns:
        ``{"id", "type", "title"?, "body"?, "steps"?, "status"?, "path"?,
        "shallow", "body_truncated"?}`` for a plan, or ``{"id", "type",
        "text", "body_truncated"?}`` for a turn.  ``{"error": ...}`` when the
        id cannot be resolved.
    """
    if not id or ":" not in id:
        return {"error": "invalid_argument", "message": f"invalid id {id!r}"}
    feedback_match = _FEEDBACK_ID_RE.match(id)
    if feedback_match:
        return _feedback_body(
            feedback_match.group("sid"),
            int(feedback_match.group("ord")),
            max_chars=max_chars,
            redact=redact,
        )
    session_id = id.rsplit(":", 1)[0]
    stream = list(iter_events(session=session_id))
    event = next((e for e in stream if e.id == id), None)
    if event is None:
        return {"error": "not_found", "id": id}

    if event.type != "plan_event":
        # Turn/tool body: the text already lives on the event.
        text, body_truncated = _cap_body(event.text, max_chars)
        out: dict[str, Any] = {"id": id, "type": event.type, "text": text}
        if body_truncated:
            out["body_truncated"] = True
        return _redact_body_fields(out, redact)

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
            query(type="plan_event", session=session_id, redact=False)
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
    if "body" in result:
        result["body"], body_truncated = _cap_body(result["body"], max_chars)
        if body_truncated:
            result["body_truncated"] = True
    return _redact_body_fields(result, redact)


def _redact_body_fields(result: dict[str, Any], redact: bool) -> dict[str, Any]:
    """Emission-time redaction for a ``get_body`` payload (F2.1), in place.

    Masks ``text``/``body``/``title``/``steps``/``pairs``; attaches per-type
    ``redactions`` counts only when something was actually masked.  No-op
    when ``redact`` is ``False``.
    """
    if not redact:
        return result
    redactions: dict[str, int] = {}
    for field in ("text", "body", "title", "steps", "pairs"):
        if field not in result:
            continue
        new_val, counts = redact_value(result[field])
        if counts:
            result[field] = new_val
            merge_redaction_counts(redactions, counts)
    if redactions:
        result["redactions"] = redactions
    return result
