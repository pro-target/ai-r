"""The ``audit_brief`` preset — a token-lean session digest for auditors.

Answers "what happened in this session, verbatim where it matters" in ONE call,
inside a hard character budget.  An auditor reconstructing a session today has
to chain ``read_session`` + ``query`` + ``plan`` + ``find_file_edits`` +
``session_stats`` by hand and then trim the output; this preset bakes exactly
that chain with a deterministic selection algorithm and a token budget — the
project preset rule (one call = a fixed chain of base verbs with an algorithm
inside, never a second engine).

The baked chain (existing projections only, no new event taxonomy):

1. **User turns, VERBATIM** — ``query(type="user_turn", session=…)``.  The
   user's words are the auditor's ground truth (rule: "auditor collects user
   turns"), so they are emitted in full and are **NEVER truncated** by the
   budget.
2. **Plans / decisions** — the ``plan`` projection (atoms + the final body)
   plus ``plan_feedback`` («plan quote → user comment» verdict pairs).
3. **Tool-call footprint** — the SAME ``query`` scan's ``tool_call`` rows,
   folded by ``aggregate(group_by="tool_kind")`` (counts, not dumps) + the
   notable errors (rows whose correlated result carried ``is_error``).
4. **File footprint** — the edit/write rows' ``file`` refs from that same
   scan, folded to distinct files with edit counts.  (The ``find_file_edits``
   core has no session facet and scans the whole corpus by ``path`` substring,
   so the session-scoped footprint reuses the event stream's existing file
   refs instead — same classifier, zero new scanning code.)
5. **Token breakdown** — :func:`ai_r.tokens.session_tokens` +
   :func:`ai_r.tokens.component_tokens`, the SAME per-session SSOT
   ``session_stats(with_tokens)`` and ``read_session(with_tokens)`` read.

Deterministic budget algorithm (two-step: build full → tighten in a FIXED
ladder until the serialized digest fits ``budget_chars``):

1. drop tool-call error details (counts stay);
2. drop the per-file edit list (counts stay);
3. drop plan bodies + feedback quote/comment texts (references stay — bodies
   on-demand via ``get_body``).

If the digest still exceeds the budget after the full ladder, the user turns
alone (plus the summary counts) are over it: they stay VERBATIM and the
response carries an honest ``over_budget: true`` + a ``note`` pointing at the
full projections — never a silently clipped ground truth.

Honesty rules (house-wide): absence is honest (``tokens.source`` ``null``
without a signal, empty ``plans`` for agents with no plan signal), matching
runs on RAW text while emitted text is redacted by default (F2.1), and the
budget is measured on the ACTUAL serialized JSON — not a guess.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai_r.events import (
    aggregate as _aggregate,
    plan as _plan,
    plan_feedback as _plan_feedback,
    query as _query,
)
from ai_r.parsers import PARSERS, Session, iso, target_agents
from ai_r.redact import merge_redaction_counts, redact_text
from ai_r.resume import resume_command
from ai_r.tokens import component_tokens, session_tokens

__all__ = ["DEFAULT_BUDGET_CHARS", "audit_brief"]


# --- knobs (deterministic, all caps are ai-r-authored bounds) ----------------
# The hard character budget the serialized digest must fit (0 = unlimited).
# ~15k chars ≈ ~4k tokens — a digest an auditor can afford per session.
DEFAULT_BUDGET_CHARS: int = 15_000

# Level-0 inline caps — applied BEFORE the budget ladder, so a single huge
# plan body cannot eat the whole budget on its own.  Cut with the house
# ``…[truncated]`` marker; the full body stays on-demand via ``get_body``.
_PLAN_BODY_CHARS_CAP = 4_000
_FEEDBACK_CHARS_CAP = 500

# Detail-list caps (the "notable" selection is deterministic: first N in
# chronological / count order; the *_count fields always carry the full total).
_ERROR_RECORDS_CAP = 10
_FILE_RECORDS_CAP = 20

_TRUNCATION_MARKER = "…[truncated]"


def _measure(payload: Any) -> int:
    """The digest's budget currency: chars of its serialized JSON."""
    return len(json.dumps(payload, ensure_ascii=False, default=str))


def _ref_value(refs: Any, key: str) -> Any:
    for r in refs or ():
        if isinstance(r, dict) and key in r:
            return r[key]
    return None


def _cap(text: Optional[str], cap: int) -> Optional[str]:
    if isinstance(text, str) and len(text) > cap:
        return text[:cap] + _TRUNCATION_MARKER
    return text


def _resolve_session(
    session: str, agent: Optional[str]
) -> Tuple[Session, Any]:
    """Resolve ``session`` to its :class:`Session` + owning parser.

    Same agent-optional semantics as ``read_session``: the id is looked up
    across every parser (scoped when ``agent`` is given).  A miss raises
    :class:`FileNotFoundError` — the MCP wrapper maps it to ``not_found``.
    """
    for agent_name in target_agents(agent):
        parser = PARSERS[agent_name]
        try:
            if parser.session_exists(session):
                return parser.read_session(session), parser
        except (ValueError, OSError):
            continue
    scope = agent or "any supported agent"
    raise FileNotFoundError(f"session {session!r} not found under {scope}")


# --- budget-ladder droppers (fixed order; each returns True when it removed
# something, so the emitted ``dropped`` list never over-claims) ---------------


def _drop_tool_error_details(resp: Dict[str, Any]) -> bool:
    tools = resp.get("tools") or {}
    if tools.get("errors"):
        tools["errors"] = None
        tools["errors_dropped"] = True
        return True
    return False


def _drop_file_details(resp: Dict[str, Any]) -> bool:
    files = resp.get("files") or {}
    if files.get("edited"):
        files["edited"] = None
        files["edited_dropped"] = True
        return True
    return False


def _drop_plan_bodies(resp: Dict[str, Any]) -> bool:
    plans = resp.get("plans") or {}
    dropped = False
    for atom in plans.get("tasks") or ():
        if atom.get("body") is not None:
            atom["body"] = None
            atom["body_dropped"] = True
            dropped = True
    for pair in plans.get("feedback") or ():
        if pair.get("quote") is not None or pair.get("comment") is not None:
            pair["quote"] = None
            pair["comment"] = None
            dropped = True
    if dropped:
        plans["detail_dropped"] = True
    return dropped


# The FIXED tightening ladder: tool details first, then the per-file list,
# then the plan/feedback (assistant-authored) quotes.  User turns are not on
# the ladder — they are never cut.
_LADDER: Tuple[Tuple[str, Callable[[Dict[str, Any]], bool]], ...] = (
    ("tool_error_details", _drop_tool_error_details),
    ("file_details", _drop_file_details),
    ("plan_bodies", _drop_plan_bodies),
)


# --- the preset ---------------------------------------------------------------


def audit_brief(
    session: str,
    *,
    agent: Optional[str] = None,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    redact: bool = True,
) -> dict[str, Any]:
    """One-call, budgeted session digest for auditors (stage-4 preset).

    The baked chain (see module docstring): ONE ``query`` scan over the
    session's events → user turns verbatim + tool/file footprint via
    ``aggregate`` folds; the ``plan``/``plan_feedback`` projections for the
    decision trail; the ``ai_r.tokens`` SSOT for the token breakdown — then a
    deterministic budget ladder tightens the digest until it fits.

    Args:
        session: The session uuid (required).
        agent: Optional agent hint (``claude``/``codex``/…); ``None`` = the
            id is resolved across every parser (same as ``read_session``).
        budget_chars: Hard budget on the serialized digest, in characters
            (default :data:`DEFAULT_BUDGET_CHARS`; ``0`` = unlimited, the
            ladder never runs).  User turns are NEVER truncated by it.
        redact: ``True`` (default) masks secrets in the emitted title / user
            texts / plan bodies / feedback pairs and adds a ``redactions``
            type→count dict when anything was masked; ``False`` returns raw.

    Returns:
        A dict of deterministic sections::

            {
              "session":    {uuid, agent, title, date, project_dir,
                             launch_surface, kind, parent_uuid,
                             message_count, models, resume_command, path},
              "user_turns": [{id, ts, text}, ...],   # verbatim, NEVER cut
              "user_turns_count": N,
              "plans":      {count, tasks: [...], feedback: [...],
                             feedback_count},
              "tools":      {total, by_kind: {kind: n}, errors_count,
                             errors: [{id, ts, tool, tool_kind}, ...]},
              "files":      {count, edited: [{file, edits}, ...]},
              "tokens":     {...session_tokens...},
              "component_tokens": {...} | null,
              "budget":     {budget_chars, used_chars, dropped: [...],
                             over_budget, note?},
              "redactions": {...}    # only when something was masked
            }

    Raises:
        ValueError: invalid arguments (empty ``session``, unknown ``agent``,
            negative/non-int ``budget_chars``, non-bool ``redact``).
        FileNotFoundError: the session id is not found under any parser.
    """
    if not isinstance(session, str) or not session.strip():
        raise ValueError(f"session must be a non-empty uuid string, got {session!r}")
    if (
        not isinstance(budget_chars, int)
        or isinstance(budget_chars, bool)
        or budget_chars < 0
    ):
        raise ValueError(
            f"budget_chars must be a non-negative integer, got {budget_chars!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")

    sess_obj, parser = _resolve_session(session.strip(), agent)
    agent_label = sess_obj.agent.value.lower()
    uuid = sess_obj.uuid

    redactions: dict[str, int] = {}

    def _emit(text: Optional[str]) -> Optional[str]:
        """Emission-time redaction (F2.1) — matching above ran on RAW text."""
        if not redact or not isinstance(text, str) or not text:
            return text
        new_val, counts = redact_text(text)
        if counts and isinstance(new_val, str):
            merge_redaction_counts(redactions, counts)
            return new_val
        return text

    # --- Step 1: ONE query scan over the session's normalized events -------
    events = _query(
        session=[uuid], agent=agent_label, limit=0, redact=False
    )
    user_rows = [ev for ev in events if ev.get("type") == "user_turn"]
    tool_rows = [
        ev for ev in events if str(ev.get("type", "")).startswith("tool_call")
    ]

    # (a) user turns — VERBATIM, chronological (the query scan's date order).
    user_turns = [
        {"id": ev.get("id"), "ts": ev.get("ts"), "text": _emit(ev.get("text"))}
        for ev in user_rows
    ]

    # (b) plans / decisions — the existing plan projections, slimmed.
    plan_atoms = _plan(uuid, agent=agent_label, bodies="final")
    tasks: List[dict[str, Any]] = []
    for atom in plan_atoms:
        entry: dict[str, Any] = {
            "id": atom.get("id"),
            "task_id": atom.get("task_id"),
            "kind": atom.get("kind"),
            "version": atom.get("version"),
            "title": _emit(atom.get("title")),
        }
        if atom.get("kind") == "final":
            entry["body"] = _emit(_cap(atom.get("body"), _PLAN_BODY_CHARS_CAP))
            entry["body_source"] = atom.get("body_source")
        tasks.append(entry)
    feedback_pairs = [
        {
            "plan_id": pair.get("plan_id"),
            "plan_version": pair.get("plan_version"),
            "verdict": pair.get("verdict"),
            "round": pair.get("round"),
            "section": pair.get("section"),
            "quote": _emit(_cap(pair.get("quote"), _FEEDBACK_CHARS_CAP)),
            "comment": _emit(_cap(pair.get("comment"), _FEEDBACK_CHARS_CAP)),
            "ts": pair.get("ts"),
            "ref": pair.get("ref"),
        }
        for pair in _plan_feedback(uuid, agent=agent_label)
    ]

    # (c) tool footprint — aggregate fold by tool_kind + notable errors.
    by_kind_rollup = _aggregate(
        tool_rows, group_by="tool_kind", metrics=("count",)
    )
    by_kind = {
        str(g.get("group")): g.get("count")
        for g in by_kind_rollup.get("groups", ())
    }
    error_rows = [
        ev for ev in tool_rows if _ref_value(ev.get("refs"), "is_error") is True
    ]
    errors = [
        {
            "id": ev.get("id"),
            "ts": ev.get("ts"),
            "tool": _ref_value(ev.get("refs"), "tool"),
            "tool_kind": ev.get("tool_kind"),
        }
        for ev in error_rows[:_ERROR_RECORDS_CAP]
    ]

    # (d) file footprint — the edit/write rows' existing file refs, folded.
    file_counts: dict[str, int] = {}
    for ev in tool_rows:
        if ev.get("tool_kind") not in ("edit", "write"):
            continue
        path = _ref_value(ev.get("refs"), "file")
        if isinstance(path, str) and path:
            file_counts[path] = file_counts.get(path, 0) + 1
    edited = [
        {"file": path, "edits": n}
        for path, n in sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ][:_FILE_RECORDS_CAP]

    # (e) token breakdown — the same per-session SSOT the token surfaces use.
    try:
        messages = parser.read_messages(uuid)
    except (FileNotFoundError, ValueError, OSError):
        messages = []
    tokens_block = session_tokens(sess_obj, messages=messages)
    comp_block = component_tokens(messages, agent=agent_label)

    response: dict[str, Any] = {
        "session": {
            "uuid": uuid,
            "agent": agent_label,
            "title": _emit(sess_obj.title),
            "date": iso(sess_obj.date),
            "project_dir": sess_obj.project_dir,
            "launch_surface": sess_obj.launch_surface,
            "kind": sess_obj.kind,
            "parent_uuid": sess_obj.parent_uuid,
            "message_count": sess_obj.message_count,
            "models": list(sess_obj.models),
            "resume_command": resume_command(sess_obj),
            "path": str(sess_obj.path),
        },
        "user_turns": user_turns,
        "user_turns_count": len(user_turns),
        "plans": {
            "count": len(tasks),
            "tasks": tasks,
            "feedback": feedback_pairs,
            "feedback_count": len(feedback_pairs),
        },
        "tools": {
            "total": len(tool_rows),
            "by_kind": by_kind,
            "errors_count": len(error_rows),
            "errors": errors,
        },
        "files": {"count": len(file_counts), "edited": edited},
        "tokens": tokens_block,
        "component_tokens": comp_block,
    }

    # --- Step 2: the deterministic budget ladder ---------------------------
    dropped: List[str] = []
    over_budget = False
    note: Optional[str] = None
    if budget_chars:
        for label, dropper in _LADDER:
            if _measure(response) <= budget_chars:
                break
            if dropper(response):
                dropped.append(label)
        if _measure(response) > budget_chars:
            over_budget = True
            note = (
                "the remaining digest (user turns verbatim + summary counts) "
                "exceeds budget_chars; user turns are the auditor's ground "
                "truth and are NEVER truncated — emitted whole. Full "
                f"projections: query(type='user_turn', session='{uuid}') / "
                f"read_session('{uuid}')."
            )

    budget_block: dict[str, Any] = {
        "budget_chars": budget_chars,
        "used_chars": _measure(response),
        "dropped": dropped,
        "over_budget": over_budget,
    }
    if note:
        budget_block["note"] = note
    response["budget"] = budget_block
    if redactions:
        response["redactions"] = redactions
    return response
