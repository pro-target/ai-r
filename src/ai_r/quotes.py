"""The ``quotes`` preset (F5.2) — user quotes of prior in-session content.

Answers "where did the user quote something the agent said, and what did they
say about it?" in one call.  When a user selects a chunk of a prior message
and comments on it (the "attach selection as context" / quote-reply flow), the
quoted text is embedded VERBATIM in their turn — no agent records it as a
structured field (verified across 1120 Claude sessions: zero ``selectedText``/
``quotedText`` markers), so it is recovered by matching the user's turn against
the text that came before it.  A *quote* is the longest verbatim (normalized)
run shared between a user turn and a preceding assistant turn; the rest of the
user turn is their *comment*.

This is a preset over the existing core, NOT a second engine (project preset
rule):

1. **Step 1 — candidates** come from ``query`` scans (``type="user_turn"`` for
   the commenter, ``type="assistant_turn"`` for the source) — session
   iteration, agent/session/date/noise/project_dir facets and event ids are
   all the query core's.
2. **Deterministic matching** — the reviewed content length uses
   :func:`ai_r.events.model._normalize_rendered_text` (markdown/whitespace
   normalization, reused from the plan-feedback anchorer) and
   :func:`difflib.SequenceMatcher` finds the longest verbatim run.  Zero LLM,
   zero guessing: a run below :data:`MIN_QUOTE_CHARS` is not a quote (honest
   ``null``); text pasted from OUTSIDE the session matches nothing and is
   never fabricated into a quote.
3. **Token budget** — emitted ``quote``/``comment`` strings are char-capped,
   ``limit`` bounds the record count, full context stays on-demand via the
   event ids.

This is the cross-agent, chat-wide generalization of ``plan(feedback)``: that
verb surfaces «plan quote → user comment» pairs, but ONLY for Claude's
interactive plan-approval flow.  ``quotes`` surfaces «any prior message quote →
user comment» pairs for every agent, since it operates on the normalized event
stream + full message text, not on any client-specific quoting markup.

Honesty rules (same as the rest of the package): all agents are equal — any
parser whose user turns carry text participates; a quote whose source is not in
the session (external paste) yields no record, never a guessed one.  v1 sources
are assistant *prose* (the common case — a user quoting what the agent said);
quoting a tool's raw output is a documented future extension.  The emitted
``quote``/``comment`` are taken from the NORMALIZED text (markdown stripped),
so they are readable but not byte-identical to the raw turn — the raw bodies
stay reachable via the event ids.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ai_r.events.model import _normalize_rendered_text
from ai_r.events.query import query as _query
from ai_r.parsers import PARSERS, Message, target_agents
from ai_r.redact import merge_redaction_counts, redact_text

__all__ = [
    "MIN_QUOTE_CHARS",
    "SOURCE_KINDS",
    "find_verbatim_quote",
    "quotes",
]


# --- knobs (deterministic, calibrated) ---------------------------------------
# The shortest verbatim run that counts as a quote.  Below this, a shared run
# is boilerplate (a path, a common phrase) rather than a deliberate quotation.
# Calibrated 2026-07-09 on a real session where a user quoted a 281-char span
# of the agent's summary and commented on it — the one true quote surfaced,
# with no boilerplate false positives at 40.
MIN_QUOTE_CHARS: int = 40

# How many prior assistant turns before a user turn the match scan covers.  A
# quote references content the user recently saw; a bounded window keeps the
# pairwise diff cost linear in the number of user turns.
_LOOKBACK_ASSISTANT_TURNS: int = 60

# The source-kind vocabulary — what the quoted text was taken from.  v1:
# assistant prose only (a user quoting the agent).  The ``source_kind`` filter
# validates against this set.
SOURCE_KINDS: frozenset[str] = frozenset({"assistant"})

# Emitted-string caps (chars) — the preset's token budget.
_QUOTE_CHARS_CAP = 500
_COMMENT_CHARS_CAP = 500

_DEFAULT_LIMIT = 50


# --- verbatim matching (exposed for tests) -----------------------------------


def find_verbatim_quote(
    user_text: str, source_text: str, min_chars: int = MIN_QUOTE_CHARS
) -> Optional[Tuple[int, str, str]]:
    """The longest verbatim run shared between ``user_text`` and ``source_text``.

    Both sides are normalized (markdown/emphasis stripped, whitespace collapsed
    — :func:`_normalize_rendered_text`) before matching, so a quote survives
    light re-rendering.  Returns ``(size, quote, comment)`` where ``quote`` is
    the shared run and ``comment`` is the user's normalized text with the quote
    elided, or ``None`` when the longest run is below ``min_chars`` (not a
    quote).  Pure :func:`difflib.SequenceMatcher` — deterministic, no guessing.
    """
    nu = _normalize_rendered_text(user_text or "")
    ns = _normalize_rendered_text(source_text or "")
    if len(nu) < min_chars or len(ns) < min_chars:
        return None
    matcher = difflib.SequenceMatcher(None, nu, ns, autojunk=False)
    match = matcher.find_longest_match(0, len(nu), 0, len(ns))
    if match.size < min_chars:
        return None
    quote = nu[match.a:match.a + match.size]
    comment = (nu[:match.a] + " … " + nu[match.a + match.size:]).strip()
    return match.size, quote, comment


# --- helpers ------------------------------------------------------------------


def _seq_of(event_id: Optional[str]) -> int:
    try:
        return int(str(event_id).rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return -1


def _text_at(messages: Sequence[Any], idx: int) -> str:
    if 0 <= idx < len(messages):
        t = getattr(messages[idx], "text", "") or ""
        if isinstance(t, str):
            return t
    return ""


# --- the preset ---------------------------------------------------------------


def quotes(
    *,
    agent: Optional[str] = None,
    session: Optional[Any] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    source_kind: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    noise: str = "include",
    project_dir: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Find «user quote → user comment» pairs — chat-wide, cross-agent (F5.2).

    The baked chain (see module docstring): ``query`` scans for user turns +
    assistant turns → per user turn, the longest verbatim (normalized) run
    shared with a preceding assistant turn → the quote, its source event, and
    the surrounding comment.

    Args:
        agent: Optional agent filter (``claude``/``codex``/...); ``None`` = all.
        session: Optional session scope — a uuid or a list of uuids (same
            semantics/validation as the ``query`` facet).
        since / until: ISO-8601 bounds (inclusive) on the user turn's ts.
        source_kind: Optional filter — currently only ``"assistant"``
            (:data:`SOURCE_KINDS`).  Unknown values fail loud.
        limit: Max records returned (``0`` = no cap, default ``50``).
            ``count``/``by_source_kind`` reflect the FULL matched set.
        noise / project_dir: Session-level filters forwarded to ``query``.
        redact: ``True`` (default) masks secrets in the emitted
            ``session_title``/``quote``/``comment`` and adds a ``redactions``
            type→count dict when anything was masked; ``False`` returns raw.

    Returns:
        A dict::

            {
              "quotes": [
                {
                  "id": "<session>:<seq>",       # the user_turn event id
                  "agent", "session_id", "session_title", "ts",
                  "message_index": int,
                  "source_id": "<session>:<seq>",  # the quoted assistant turn
                  "source_message_index": int,
                  "source_kind": "assistant",
                  "quote_chars": int,
                  "quote": "<capped>",
                  "comment": "<capped>",           # user turn minus the quote
                }, ...
              ],
              "count": N,
              "by_source_kind": {"assistant": N},
              "truncated": bool,
              "redactions": {...},        # only when something was masked
              "diagnostics": {...}        # only when count == 0
            }

        Records are ordered chronologically (ts ascending, undated last).

    Raises:
        ValueError: on invalid arguments (unknown ``source_kind``/``agent``/
            ``noise``, malformed ``session``/``since``/``until``, negative
            ``limit``, non-bool ``redact``).
    """
    if source_kind is not None and source_kind not in SOURCE_KINDS:
        raise ValueError(
            f"source_kind must be one of {sorted(SOURCE_KINDS)}, "
            f"got {source_kind!r}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(
            f"limit must be a non-negative integer, got {limit!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")

    # --- Step 1: candidates (user turns) + sources (assistant turns) ------
    scanned_sessions: dict[str, Any] = {}
    common: dict[str, Any] = dict(
        agent=agent, session=session, since=since, until=until, limit=0,
        noise=noise, project_dir=project_dir, redact=False,
    )
    user_events = _query(
        type="user_turn", scanned_sessions_out=scanned_sessions, **common
    )
    # since/until bound the USER turn only; sources need the whole session, so
    # the assistant scan drops those two bounds.
    asst_common = dict(common)
    asst_common.pop("since"), asst_common.pop("until")
    asst_events = _query(type="assistant_turn", **asst_common)

    title_by_uuid: dict[str, Optional[str]] = {}
    for sessions in scanned_sessions.values():
        for sess in sessions or ():
            title_by_uuid[sess.uuid] = getattr(sess, "title", None)

    parser_by_agent = {
        name.value.lower(): PARSERS[name] for name in target_agents(None)
    }

    # Group both streams by session; assistant turns sorted by seq for the
    # "preceding" scan.
    users_by_session: dict[str, List[dict]] = {}
    agent_by_session: dict[str, str] = {}
    for ev in user_events:
        sid = ev.get("session_id") or ""
        if sid:
            users_by_session.setdefault(sid, []).append(ev)
            agent_by_session[sid] = ev.get("agent") or ""
    asst_by_session: dict[str, List[dict]] = {}
    for ev in asst_events:
        sid = ev.get("session_id") or ""
        if sid:
            asst_by_session.setdefault(sid, []).append(ev)
    for lst in asst_by_session.values():
        lst.sort(key=lambda e: _seq_of(e.get("id")))

    records: List[dict[str, Any]] = []
    for sid, user_list in users_by_session.items():
        parser = parser_by_agent.get(agent_by_session.get(sid, ""))
        if parser is None:  # pragma: no cover — agents come from the scan
            continue
        try:
            messages: Sequence[Message] = parser.read_messages(sid)
        except (FileNotFoundError, ValueError, OSError):
            continue
        sources = asst_by_session.get(sid, [])
        for uev in user_list:
            u_seq = _seq_of(uev.get("id"))
            u_idx = uev.get("message_index", -1)
            if not isinstance(u_idx, int):
                continue
            user_text = _text_at(messages, u_idx)
            if len(user_text) < MIN_QUOTE_CHARS:
                continue
            # Preceding assistant turns, nearest first, within the window.
            prior = [a for a in sources if _seq_of(a.get("id")) < u_seq]
            best: Optional[Tuple[int, str, str, dict]] = None
            for aev in reversed(prior[-_LOOKBACK_ASSISTANT_TURNS:]):
                a_idx = aev.get("message_index", -1)
                if not isinstance(a_idx, int):
                    continue
                found = find_verbatim_quote(user_text, _text_at(messages, a_idx))
                if found is None:
                    continue
                size, quote, comment = found
                if best is None or size > best[0]:
                    best = (size, quote, comment, aev)
            if best is None:
                continue
            size, quote, comment, aev = best
            records.append({
                "id": uev.get("id"),
                "agent": uev.get("agent"),
                "session_id": sid,
                "session_title": title_by_uuid.get(sid),
                "ts": uev.get("ts"),
                "message_index": u_idx,
                "source_id": aev.get("id"),
                "source_message_index": aev.get("message_index"),
                "source_kind": "assistant",
                "quote_chars": size,
                # Placeholders — redacted + capped at emission, only for the
                # records that survive the limit slice.
                "quote": None,
                "comment": None,
                "_raw_quote": quote,
                "_raw_comment": comment,
            })

    # Chronological order (ts ascending, undated last) — deterministic.
    records.sort(key=lambda r: (r["ts"] is None, r["ts"] or "", r["id"] or ""))

    total = len(records)
    by_source_kind: Dict[str, int] = {}
    for r in records:
        k = r["source_kind"]
        by_source_kind[k] = by_source_kind.get(k, 0) + 1

    truncated = False
    if limit and total > limit:
        records = records[:limit]
        truncated = True

    # Emission-time redaction + cap (F2.1): redact the FULL string first, THEN
    # cap — a secret sliced by the cap edge can never leak partially.
    redactions: dict[str, int] = {}
    for r in records:
        for field, raw, cap in (
            ("quote", r.pop("_raw_quote"), _QUOTE_CHARS_CAP),
            ("comment", r.pop("_raw_comment"), _COMMENT_CHARS_CAP),
        ):
            emit = raw
            if redact:
                emit, counts = redact_text(raw)
                if counts:
                    merge_redaction_counts(redactions, counts)
            if len(emit) > cap:
                emit = emit[:cap] + "…"
            r[field] = emit
        if redact:
            new_val, counts = redact_text(r.get("session_title"))
            if counts:
                r["session_title"] = new_val
                merge_redaction_counts(redactions, counts)

    response: dict[str, Any] = {
        "quotes": records,
        "count": total,
        "by_source_kind": by_source_kind,
        "truncated": truncated,
    }
    if redactions:
        response["redactions"] = redactions
    if total == 0:
        from ai_r.diagnostics import empty_result_diagnostics

        response["diagnostics"] = empty_result_diagnostics(
            agent=agent,
            since=since,
            until=until,
            filters={
                "session": session,
                "source_kind": source_kind,
                "noise": None if noise == "include" else noise,
                "project_dir": project_dir,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return response
