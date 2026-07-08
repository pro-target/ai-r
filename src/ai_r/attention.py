"""The ``attention`` preset (F4.4) — rushed-approval pacing audit.

Answers "did the user approve a decision gate faster than they could have
read it?" in one call.  A *gate* is an interactive decision point the agent
put to the user — a plan presented for approval (Claude ``ExitPlanMode``)
or a structured question (``AskUserQuestion``).  The gate becomes a
**rushed-approval** signal when the time between the gate and the user's
answer is too small for the content to have been read: the required reading
speed (``content_chars / gap_seconds``) exceeds a multiple of an average
human reading rate, or the answer was near-instant on non-trivial content.

This is a preset over the existing core, NOT a second engine (project
preset rule):

1. **Step 1 — candidates** come from ONE :func:`ai_r.events.query` scan
   (``type="tool_call"``) filtered to the gate tool names
   (:data:`GATE_TOOLS`) — session iteration, agent/session/date/noise/
   project_dir facets and event ids are all the query core's, nothing is
   re-implemented here.
2. **Deterministic pacing** — for each gate the user's answer is located at
   the *message* level (the answer is a ``tool_result`` correlated by
   ``tool_use_id``, NOT a normalized ``user_turn`` event, so the event
   stream alone cannot time it); the gap is ``answer.ts − gate.ts`` and the
   reviewed content length is measured from the gate's own payload (the
   plan body / the question text).  The severity is a pure arithmetic
   verdict against :data:`AVG_CPS` / :data:`RED_RATIO` / :data:`AMBER_RATIO`
   / :data:`FLOOR_SEC` — zero LLM, zero guessing: no answer found → no
   signal (honest ``null``), unmeasurable content → floor-only.
3. **Token budget** — the emitted answer preview is char-capped, ``limit``
   bounds the record count, and full context stays on-demand (the record's
   ``id`` is a query event id: walk neighbours via ``query(relative_to=...)``
   or read the session).

Why only plan + question gates (calibrated on real Claude JSONL,
2026-07-08): an edit/delete "approval" is NOT a distinct human-timed event
— when the tool is auto-approved (``permissionMode`` allows it) its
``tool_result`` lands at machine speed (tens of ms), indistinguishable from
a genuine human click, so timing it would flag every auto-approved write as
rushed.  Only the plan-approval and question gates carry a real
human-decision timestamp (the ``ExitPlanMode`` / ``AskUserQuestion``
result), so those are the honest v1 gates; an edit/delete gate that
consults ``permissionMode`` is a possible future refinement.

Honesty rules (same as the rest of the package): all agents are equal —
any parser that surfaces these gate tool calls participates (today the
interactive plan/question flow is Claude's; other agents honestly
contribute nothing); a gate whose answer or timestamps are missing yields
no signal, never a guessed one; reviewed-content length is a best-effort
measure and, when it cannot be determined, only the absolute floor applies
(documented, not hidden).

Caveats (documented trade-offs, mirror the incidents "mention vs
execution" note):

* **Streaming/authorship not credited** — the gate ts is the message's
  completion instant; a user who read while the answer streamed (or who
  authored the very plan they approve) can look faster than they were.  The
  high default ratio + the floor keep this from firing on ordinary careful
  approvals, but a genuinely-read fast approval of a long self-authored
  plan can still surface — that is a signal to review, not a proof.
* **AFK inflates the gap** — a user who stepped away looks *slower*, so the
  bias is toward under-flagging (safe): a missed rushed approval, never a
  fabricated one.
* **One transcript clock** — gaps assume the gate and answer timestamps
  come from the same monotonic clock; a negative gap (clock skew) is
  dropped, never coerced.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ai_r.events.query import query as _query
from ai_r.parsers import PARSERS, Message, target_agents
from ai_r.redact import merge_redaction_counts, redact_text

__all__ = [
    "AVG_CPS",
    "AMBER_RATIO",
    "FLOOR_SEC",
    "GATE_KINDS",
    "GATE_TOOLS",
    "MIN_CHARS",
    "RED_RATIO",
    "SEVERITY_MODES",
    "attention",
    "classify_pacing",
    "match_decision",
]


# --- gate vocabulary ---------------------------------------------------------
# The interactive decision-gate tool names → their gate kind.  Only tools
# whose answer carries a genuine human-decision timestamp qualify (see the
# module docstring on why edit/delete is excluded).
GATE_TOOLS: Dict[str, str] = {
    "ExitPlanMode": "plan",
    "AskUserQuestion": "question",
}

# The gate-kind vocabulary — the values of :data:`GATE_TOOLS`; the ``gate``
# filter validates against this set.
GATE_KINDS: frozenset[str] = frozenset(GATE_TOOLS.values())

# ``severity`` filter vocabulary: ``flagged`` = red+amber (default),
# ``red`` = red only, ``all`` = every measured gate incl. the clean ones
# (severity ``null``).
SEVERITY_MODES: frozenset[str] = frozenset({"flagged", "red", "all"})


# --- pacing thresholds (deterministic dictionary, calibrated) ----------------
# The reading-speed model.  Constants (not per-call knobs) so calibration
# lives with the tests, mirroring the danger/regret/risk dictionaries.  The
# effective values are echoed back in the response ``params`` for report
# transparency.
#
# AVG_CPS: an average adult *silent* reading rate in characters/second.
# ~238 wpm (Brysbaert 2019 meta-analysis) × ~6 chars/word / 60 ≈ 24 c/s;
# rounded to 25.  RED at ≥4× this rate (≈2× a trained skim — implausible to
# have read); AMBER at ≥2× (skim-only, shallow).  FLOOR_SEC: any non-trivial
# gate answered faster than this is red regardless of content length (an
# effectively instant ok).  MIN_CHARS: below this the gate is trivial and
# never flagged (a one-line question answered in a second is fine).
AVG_CPS: float = 25.0
RED_RATIO: float = 4.0
AMBER_RATIO: float = 2.0
FLOOR_SEC: float = 2.0
MIN_CHARS: int = 200

# Emitted-fragment cap (chars) — the preset's token budget.  Full context
# stays on-demand via the event id.
_ANSWER_CHARS_CAP = 240

_DEFAULT_LIMIT = 50

# Tool names whose write carries the plan body the user reviews (Claude
# writes the plan to ``plans/<slug>.md`` before ``ExitPlanMode``).
_PLAN_FILE_TOOLS = frozenset({"Write"})
_PLAN_PATH_KEYS = ("file_path", "path", "notebook_path")


# --- decision dictionary (exposed for tests) ---------------------------------
# Coarse bilingual (ru+en) markers labelling the user's answer.  Labels, not
# raw text, are what the record's ``reaction.kind`` carries.  Order = match
# priority; ``approved``/``rejected`` win over the generic ``answered``.
_DECISION_MARKERS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("rejected", re.compile(
        r"reject|keep planning|stay in plan|отклон|не одобр|доработ", re.I)),
    ("approved", re.compile(
        r"\bapprove|has approved|одобр|принял|поехали|начина", re.I)),
    ("answered", re.compile(
        r"have been answered|your answer|выбрал|ответ", re.I)),
)


def match_decision(text: str) -> str:
    """Coarse label for a gate answer — approved / rejected / answered / other.

    A deterministic dictionary hit on the answer text, never inferred;
    nothing recognisable → ``"other"``.
    """
    if not isinstance(text, str) or not text:
        return "other"
    for label, rx in _DECISION_MARKERS:
        if rx.search(text):
            return label
    return "other"


# --- pacing verdict (exposed for tests) --------------------------------------


def classify_pacing(
    gap_sec: float, content_chars: Optional[int]
) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """The pacing verdict for one gate: ``(severity, required_cps, ratio)``.

    ``severity`` is ``"red"`` / ``"amber"`` / ``None`` (fast-but-plausible).
    ``required_cps`` is the reading speed the gap would demand
    (``content_chars / gap_sec``) and ``ratio`` its multiple of
    :data:`AVG_CPS` — both ``None`` when content is unmeasurable or the gap
    is non-positive.  The absolute floor (:data:`FLOOR_SEC`) fires on a
    near-instant answer to non-trivial content even when the exact length is
    unknown.  Pure arithmetic — no guessing.
    """
    required: Optional[float] = None
    ratio: Optional[float] = None
    if content_chars is not None and gap_sec > 0:
        required = content_chars / gap_sec
        ratio = required / AVG_CPS
    red = False
    if gap_sec < FLOOR_SEC and (content_chars is None or content_chars >= MIN_CHARS):
        red = True
    if ratio is not None and ratio >= RED_RATIO:
        red = True
    if red:
        return "red", required, ratio
    if ratio is not None and ratio >= AMBER_RATIO:
        return "amber", required, ratio
    return None, required, ratio


# --- helpers -----------------------------------------------------------------


def _parse_input(entry: dict) -> Any:
    """JSON-decode a tool_use entry's ``input`` (a string in the parsed msg)."""
    raw = entry.get("input", "")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _gate_entries(msg: Any) -> List[dict]:
    """The message's gate tool_use entries (:data:`GATE_TOOLS`), stream order.

    Mirrors the event-construction order of
    :func:`ai_r.events.model._messages_to_events` (dict entries with a
    non-empty string name), so the k-th entry here corresponds to the k-th
    gate ``tool_call`` event of the same ``message_index``.
    """
    out: List[dict] = []
    for tool in getattr(msg, "tool_use", ()) or ():
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "")
        if isinstance(name, str) and name in GATE_TOOLS:
            out.append(tool)
    return out


def _ref_value(refs: Sequence[dict], key: str) -> Optional[Any]:
    for r in refs or ():
        if isinstance(r, dict) and key in r:
            return r[key]
    return None


def _find_answer(
    messages: Sequence[Any], gate_idx: int, tool_use_id: Optional[str]
) -> Optional[Tuple[int, Any]]:
    """Locate the user's answer to the gate at ``gate_idx``.

    The answer is a following user-role message carrying a ``tool_result``
    correlated by ``tool_use_id`` (the plan-approval / question-answer
    result).  When the gate id is unknown, the first following user message
    with any ``tool_result`` is used.  Returns ``(index, message)`` or
    ``None`` — a gate still awaiting its answer (session tail) yields no
    signal, never a guessed one.
    """
    for j in range(gate_idx + 1, len(messages)):
        msg = messages[j]
        if getattr(msg, "role", None) != "user":
            continue
        results = getattr(msg, "tool_result", ()) or ()
        if not results:
            continue
        if tool_use_id is None:
            return j, msg
        for res in results:
            if isinstance(res, dict) and res.get("tool_use_id") == tool_use_id:
                return j, msg
    return None


def _answer_text(msg: Any) -> str:
    """Best-effort readable text of an answer message (result content ∪ text)."""
    parts: List[str] = []
    txt = getattr(msg, "text", "") or ""
    if isinstance(txt, str) and txt:
        parts.append(txt)
    for res in getattr(msg, "tool_result", ()) or ():
        if isinstance(res, dict):
            c = res.get("content", "")
            if isinstance(c, str) and c:
                parts.append(c)
    return "\n".join(parts)


def _question_content_chars(gate_input: Any) -> Optional[int]:
    """Reviewed-text length of an ``AskUserQuestion`` gate (question + options).

    Sums the human-readable strings (question text, option labels +
    descriptions) rather than the raw JSON, so JSON syntax does not inflate
    the reading load.  Falls back to the serialized length when the shape is
    unexpected; ``None`` when nothing is extractable.
    """
    if not isinstance(gate_input, dict):
        return None
    questions = gate_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    total = 0
    for q in questions:
        if not isinstance(q, dict):
            continue
        for key in ("question", "header"):
            v = q.get(key)
            if isinstance(v, str):
                total += len(v)
        opts = q.get("options")
        if isinstance(opts, list):
            for o in opts:
                if isinstance(o, dict):
                    for key in ("label", "description"):
                        v = o.get(key)
                        if isinstance(v, str):
                            total += len(v)
                elif isinstance(o, str):
                    total += len(o)
    return total or None


def _plan_content_chars(
    messages: Sequence[Any], gate_idx: int, gate_input: Any
) -> Optional[int]:
    """Reviewed-text length of a plan gate — the plan body the user read.

    Prefers a ``plan`` string carried in the ``ExitPlanMode`` input; else the
    body of the nearest preceding plan-file ``Write`` (Claude writes the plan
    to ``plans/<slug>.md`` before presenting ``ExitPlanMode``).  ``None`` when
    neither exists — the caller then applies the floor only (a plan of
    unknown size approved instantly is still flagged; approved after a real
    pause is not).
    """
    if isinstance(gate_input, dict):
        plan = gate_input.get("plan")
        if isinstance(plan, str) and plan.strip():
            return len(plan)
    for j in range(gate_idx - 1, -1, -1):
        msg = messages[j]
        if getattr(msg, "role", None) != "assistant":
            continue
        for tool in getattr(msg, "tool_use", ()) or ():
            if not isinstance(tool, dict) or tool.get("name") not in _PLAN_FILE_TOOLS:
                continue
            payload = _parse_input(tool)
            if not isinstance(payload, dict):
                continue
            path = next(
                (payload.get(k) for k in _PLAN_PATH_KEYS
                 if isinstance(payload.get(k), str)),
                "",
            )
            if "plan" in path.lower() and path.lower().endswith(".md"):
                content = payload.get("content")
                if isinstance(content, str):
                    return len(content)
    return None


# --- the preset ---------------------------------------------------------------


def attention(
    *,
    agent: Optional[str] = None,
    session: Optional[Any] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    gate: Optional[str] = None,
    severity: str = "flagged",
    limit: int = _DEFAULT_LIMIT,
    noise: str = "include",
    project_dir: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Find rushed-approval decision gates — pacing audit (F4.4).

    The baked chain (see module docstring): ONE ``query`` scan for gate tool
    calls (``ExitPlanMode``/``AskUserQuestion``) → message-level answer
    correlation (``tool_use_id``) → reviewed-content sizing → the arithmetic
    pacing verdict against the reading-speed dictionary.

    Args:
        agent: Optional agent filter (``claude``/``codex``/...); ``None`` =
            all agents (today only Claude surfaces these interactive gates).
        session: Optional session scope — a single uuid or a list of uuids
            (same semantics/validation as the ``query`` facet).
        since / until: ISO-8601 bounds (inclusive) on the gate timestamp.
        gate: Optional gate-kind filter — ``"plan"`` or ``"question"``
            (:data:`GATE_KINDS`).  Unknown values fail loud.
        severity: Verdict filter — ``"flagged"`` (default: red + amber),
            ``"red"`` (red only), ``"all"`` (every measured gate, including
            fast-but-plausible ones with severity ``null``).
        limit: Max records returned (``0`` = no cap, default ``50``).
            ``count``/``red_count``/``amber_count``/``by_gate`` always
            reflect the FULL matched set.
        noise / project_dir: Session-level filters, forwarded verbatim to
            the ``query`` scan.
        redact: ``True`` (default) masks secrets in the emitted
            ``session_title``/``reaction.preview`` as ``[REDACTED_<TYPE>]``
            and adds a ``redactions`` type→count dict when anything was
            masked; ``False`` returns raw.

    Returns:
        A dict::

            {
              "gates": [
                {
                  "id": "<session>:<seq>",     # query event id (context
                                               # on-demand via relative_to)
                  "agent", "session_id", "session_title", "ts",
                  "message_index": int,
                  "answer_message_index": int,
                  "gate": "plan" | "question",
                  "tool": "ExitPlanMode" | "AskUserQuestion",
                  "content_chars": int | null,   # reviewed length (null =
                                                 # unmeasured → floor-only)
                  "gap_sec": float,              # answer.ts − gate.ts
                  "required_cps": float | null,  # content_chars / gap_sec
                  "ratio": float | null,         # required_cps / AVG_CPS
                  "severity": "red" | "amber" | null,
                  "reaction": {
                    "kind": "approved"|"rejected"|"answered"|"other",
                    "preview": "<capped>"
                  }
                }, ...
              ],
              "count": N,               # full matched set (post filters)
              "red_count": M,
              "amber_count": K,
              "by_gate": {"plan": 2, "question": 1},
              "truncated": bool,        # limit tripped
              "params": {"avg_cps", "red_ratio", "amber_ratio",
                         "floor_sec", "min_chars"},   # effective thresholds
              "redactions": {...},      # only when something was masked
              "diagnostics": {...}      # only when count == 0
            }

        Records are ordered chronologically (ts ascending, undated last).

    Raises:
        ValueError: on invalid arguments (unknown ``gate``/``severity``/
            ``agent``/``noise``, malformed ``session``/``since``/``until``,
            negative ``limit``, non-bool ``redact``).
    """
    if gate is not None and gate not in GATE_KINDS:
        raise ValueError(
            f"gate must be one of {sorted(GATE_KINDS)}, got {gate!r}"
        )
    if severity not in SEVERITY_MODES:
        raise ValueError(
            f"severity must be one of {sorted(SEVERITY_MODES)}, got {severity!r}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(
            f"limit must be a non-negative integer, got {limit!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")

    # --- Step 1: candidates from ONE query scan --------------------------
    scanned_sessions: dict[str, Any] = {}
    events = _query(
        type="tool_call",
        agent=agent,
        session=session,
        since=since,
        until=until,
        limit=0,
        noise=noise,
        project_dir=project_dir,
        scanned_sessions_out=scanned_sessions,
        redact=False,
    )
    # Keep only gate tool calls (name in GATE_TOOLS) — the event's raw tool
    # name lives in refs {"tool": ...}, falling back to the event text.
    events = [
        ev for ev in events
        if (_ref_value(ev.get("refs") or (), "tool") or ev.get("text"))
        in GATE_TOOLS
    ]

    title_by_uuid: dict[str, Optional[str]] = {}
    for sessions in scanned_sessions.values():
        for sess in sessions or ():
            title_by_uuid[sess.uuid] = getattr(sess, "title", None)

    parser_by_agent = {
        name.value.lower(): PARSERS[name] for name in target_agents(None)
    }

    # Group candidate events by session, then message_index (read each
    # session's messages ONCE and pair with its events).
    by_session: dict[str, dict[int, List[dict[str, Any]]]] = {}
    agent_by_session: dict[str, str] = {}
    for ev in events:
        sid = ev.get("session_id") or ""
        idx = ev.get("message_index", -1)
        if not sid or not isinstance(idx, int) or idx < 0:
            continue
        by_session.setdefault(sid, {}).setdefault(idx, []).append(ev)
        agent_by_session[sid] = ev.get("agent") or ""

    records: List[dict[str, Any]] = []
    for sid, by_msg in by_session.items():
        parser = parser_by_agent.get(agent_by_session.get(sid, ""))
        if parser is None:  # pragma: no cover — agents come from the scan
            continue
        try:
            messages: Sequence[Message] = parser.read_messages(sid)
        except (FileNotFoundError, ValueError, OSError):
            continue
        for idx, msg_events in by_msg.items():
            if not (0 <= idx < len(messages)):
                continue
            entries = _gate_entries(messages[idx])
            # Pair the k-th gate event of this message with the k-th gate
            # tool_use entry — same filter, same order (see _gate_entries).
            msg_events.sort(
                key=lambda e: int(str(e.get("id", "")).rsplit(":", 1)[-1] or 0)
            )
            for ev, entry in zip(msg_events, entries):
                kind = GATE_TOOLS.get(entry.get("name", ""))
                if kind is None:  # pragma: no cover — filtered above
                    continue
                if gate is not None and kind != gate:
                    continue
                gate_ts = getattr(messages[idx], "timestamp", None)
                if gate_ts is None:
                    continue  # cannot time — honest skip
                found = _find_answer(
                    messages, idx, entry.get("tool_use_id")
                )
                if found is None:
                    continue  # no answer yet / uncorrelated — no signal
                ans_idx, ans_msg = found
                ans_ts = getattr(ans_msg, "timestamp", None)
                if ans_ts is None:
                    continue
                gap_sec = (ans_ts - gate_ts).total_seconds()
                if gap_sec < 0:
                    continue  # clock skew — never coerced
                gate_input = _parse_input(entry)
                if kind == "plan":
                    content_chars = _plan_content_chars(
                        messages, idx, gate_input
                    )
                else:
                    content_chars = _question_content_chars(gate_input)
                # Trivial gate (short, measurable content) is never flagged.
                if content_chars is not None and content_chars < MIN_CHARS:
                    continue
                sev, required_cps, ratio = classify_pacing(
                    gap_sec, content_chars
                )
                if severity == "flagged" and sev is None:
                    continue
                if severity == "red" and sev != "red":
                    continue
                # Classify the verdict on the LEADING result line only — a
                # plan-approval result echoes the full (possibly huge) plan
                # body, whose prose can carry stray decision words; the
                # system verdict ("User has approved…") is always at the head.
                answer_full = _answer_text(ans_msg)
                records.append({
                    "id": ev.get("id"),
                    "agent": ev.get("agent"),
                    "session_id": sid,
                    "session_title": title_by_uuid.get(sid),
                    "ts": ev.get("ts"),
                    "message_index": idx,
                    "answer_message_index": ans_idx,
                    "gate": kind,
                    "tool": entry.get("name"),
                    "content_chars": content_chars,
                    "gap_sec": round(gap_sec, 3),
                    "required_cps": (
                        round(required_cps, 1) if required_cps is not None
                        else None
                    ),
                    "ratio": round(ratio, 2) if ratio is not None else None,
                    "severity": sev,
                    "reaction": {
                        "kind": match_decision(answer_full[:300]),
                        # Placeholder — emitted (redacted, capped) below, only
                        # for records surviving the limit slice.
                        "preview": None,
                    },
                    "_raw_answer": answer_full,
                })

    # Chronological order (ts ascending, undated last) — deterministic.
    records.sort(key=lambda r: (r["ts"] is None, r["ts"] or "", r["id"] or ""))

    total = len(records)
    red_count = sum(1 for r in records if r["severity"] == "red")
    amber_count = sum(1 for r in records if r["severity"] == "amber")
    by_gate: Dict[str, int] = {}
    for r in records:
        by_gate[r["gate"]] = by_gate.get(r["gate"], 0) + 1

    truncated = False
    if limit and total > limit:
        records = records[:limit]
        truncated = True

    # Emission-time redaction + preview cap (F2.1): only records that
    # survived the limit slice pay for it.  ORDER (same rule as query/
    # incidents/network): redact the FULL answer first, THEN cap — a secret
    # sliced by the cap edge can never leak partially.
    redactions: dict[str, int] = {}
    for r in records:
        raw_answer = r.pop("_raw_answer")
        emit = raw_answer
        if redact:
            emit, counts = redact_text(raw_answer)
            if counts:
                merge_redaction_counts(redactions, counts)
        preview = emit.replace("\n", " ")
        if len(preview) > _ANSWER_CHARS_CAP:
            preview = preview[:_ANSWER_CHARS_CAP] + "…"
        r["reaction"]["preview"] = preview
        if redact:
            new_val, counts = redact_text(r.get("session_title"))
            if counts:
                r["session_title"] = new_val
                merge_redaction_counts(redactions, counts)

    response: dict[str, Any] = {
        "gates": records,
        "count": total,
        "red_count": red_count,
        "amber_count": amber_count,
        "by_gate": by_gate,
        "truncated": truncated,
        "params": {
            "avg_cps": AVG_CPS,
            "red_ratio": RED_RATIO,
            "amber_ratio": AMBER_RATIO,
            "floor_sec": FLOOR_SEC,
            "min_chars": MIN_CHARS,
        },
    }
    if redactions:
        response["redactions"] = redactions
    if total == 0:
        # Zero gates: attach corpus diagnostics (missing source dir vs
        # all-excluding filter vs a genuinely unhurried history).  Lazy
        # import mirrors incidents / network.
        from ai_r.diagnostics import empty_result_diagnostics

        response["diagnostics"] = empty_result_diagnostics(
            agent=agent,
            since=since,
            until=until,
            filters={
                "session": session,
                "gate": gate,
                # defaults are never the cause of emptiness — echo only the
                # non-default values.
                "severity": None if severity == "flagged" else severity,
                "noise": None if noise == "include" else noise,
                "project_dir": project_dir,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return response
