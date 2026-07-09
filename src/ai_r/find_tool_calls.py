"""Cross-agent ``find_tool_calls`` core, shared by the CLI and MCP server.

Mirrors the layout of :mod:`ai_r.find_file_edits`: the pure-Python scan
logic lives here so the CLI handler (:mod:`ai_r.cli.commands.find_tool_calls`)
and the MCP tool (:func:`ai_r.mcp_server.find_tool_calls`) both delegate
to a single implementation.  The MCP tool is a thin wrapper that catches
:class:`ValueError` and converts it to the MCP error-dict convention; the
CLI handler catches the same exception and prints it to stderr.

Unlike :func:`ai_r.find_file_edits.find_file_edits`, this module does
NOT filter on file paths — it surfaces every assistant tool call whose
``name`` matches an exact (case-insensitive) value or a substring
pattern, with the previous user message recorded as ``intent``.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from ai_r.find_file_edits import (
    cap_field as _cap_field,
    parse_iso_bound,
    previous_user_intent,
    to_utc_aware,
)
from ai_r.parsers import (
    PARSERS,
    iso,
    target_agents,
)
from ai_r.events._common import resolve_tool
from ai_r.events.model import normalize_session_filter
from ai_r.redact import merge_redaction_counts, redact_value
from ai_r.security import coerce_tool_input as _coerce_input

__all__ = [
    "find_tool_calls",
]

# --- Per-record field caps (chars) ----------------------------------------
# ``limit`` bounds the record COUNT only, never bytes.  Without these caps a
# single record can inline a multi-hundred-KB ``input`` / uncapped user
# ``intent`` / uncapped ``assistant`` text, so a handful of records blow the
# MCP response past any sane size.  Each field is truncated to its cap and
# marked in the per-record ``truncated_fields`` list when it trips.
_INPUT_CHARS_CAP = 4_000      # parsed/raw tool input, serialized
_ASSISTANT_CHARS_CAP = 4_000  # assistant message text hosting the call
_INTENT_CHARS_CAP = 1_000     # preceding user message text
_OUTPUT_CHARS_CAP = 2_000     # correlated tool_result content

# --- Total-response byte budget -------------------------------------------
# Cumulative serialized size after which we stop appending records and set the
# top-level ``output_truncated`` flag (DISTINCT from the count-based
# ``truncated``: the former means "output capped by size", the latter "more
# records matched than ``limit``").  Generous — only bites pathological output.
_OUTPUT_BYTES_BUDGET = 4_000_000  # ~4 MB of serialized records


# ``_cap_field`` is the shared :func:`ai_r.find_file_edits.cap_field`
# (imported above): one truncation contract — same marker, same structured
# fallback — for both record surfaces.


# --- Smart output truncation ----------------------------------------------
# Lines matching this pattern are treated as "interesting" for the ``smart``
# output-cap mode: on a failing call the useful signal is usually the
# error/exception/traceback line, which a naive head-slice can drop when the
# error sits at the tail of a long log.  ``smart`` mode surfaces those lines
# up front (deduped) AND keeps the tail so the terminal error is never lost.
_ERROR_LINE_RE = re.compile(
    r"error|fatal|exception|traceback|failed|panic|exit code|denied|not found",
    re.IGNORECASE,
)
_SMART_MAX_ERROR_LINES = 20


def _cap_output(text: str, cap: int, mode: str) -> tuple[str, bool]:
    """Bound ``text`` to ``cap`` chars, returning ``(capped, truncated)``.

    ``mode`` selects the strategy when ``text`` exceeds ``cap``:

    * ``"head"`` — keep the first ``cap`` chars (legacy behaviour).
    * ``"tail"`` — keep the last ``cap`` chars (the terminal output, e.g.
      the final error).
    * ``"smart"`` — surface deduped error-ish lines (see ``_ERROR_LINE_RE``)
      up front, then a ``…[truncated]…`` marker, then the tail of the text.
      Guarantees an error line at the very end of ``text`` is preserved (it
      appears both in the error-line block and the tail).

    ``text`` at or under ``cap`` is returned unchanged with ``truncated``
    ``False`` for every mode.
    """
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= cap:
        return text, False
    if mode == "tail":
        return "…[truncated]" + text[-cap:], True
    if mode == "smart":
        seen: set[str] = set()
        error_lines: List[str] = []
        for line in text.split("\n"):
            if not _ERROR_LINE_RE.search(line):
                continue
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            error_lines.append(line)
            if len(error_lines) >= _SMART_MAX_ERROR_LINES:
                break
        tail = text[-(cap // 2):] if cap > 1 else text[-1:]
        parts = []
        if error_lines:
            parts.append("\n".join(error_lines))
        parts.append("…[truncated]…")
        parts.append(tail)
        result = "\n".join(parts)
        safe_limit = 2 * cap
        if len(result) > safe_limit:
            result = result[:safe_limit] + "…[truncated]"
        return result, True
    # default / "head"
    return text[:cap] + "…[truncated]", True


def _match_ci(haystack: str, needle: str) -> bool:
    """Case-insensitive substring test."""
    return needle.lower() in haystack.lower()


def find_tool_calls(
    *,
    tool_name: Optional[str] = None,
    tool_name_pattern: Optional[str] = None,
    agent: Optional[str] = None,
    session: Optional[Any] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
    input_contains: Optional[str] = None,
    output_contains: Optional[str] = None,
    output_excludes: Optional[str] = None,
    is_error: Optional[bool] = None,
    output_mode: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Find every tool call across sessions, cross-agent by default.

    Args:
        tool_name: Exact match against ``tool_use[*].name``,
            case-insensitive.  Mutually exclusive with
            ``tool_name_pattern``.
        tool_name_pattern: Substring match against ``tool_use[*].name``,
            case-insensitive.  Mutually exclusive with ``tool_name``.
        agent: Optional filter, one of ``"claude"``, ``"codex"``,
            ``"opencode"``, ``"antigravity"``, ``"pi"``. ``None`` =
            all agents.
        session: Optional session scope — a single session uuid string
            or a list of uuid strings (same semantics/validation as the
            ``query`` facet, SSOT
            :func:`~ai_r.events.model.normalize_session_filter`).  ``None``
            = every session; a scan with a wide ``since``/``until`` but no
            ``session`` therefore surfaces calls from UNRELATED sessions,
            so pin the session when auditing one conversation.  An empty
            list or a non-string item raises :class:`ValueError` (never a
            silent unfiltered scan).
        since: Optional ISO 8601 lower bound (inclusive) on call
            timestamp.  Pass ``""`` or ``None`` to leave open.
        until: Optional ISO 8601 upper bound (inclusive) on call
            timestamp.  Pass ``""`` or ``None`` to leave open.
        limit: Maximum records to return.  ``0`` = no cap.  Default
            ``100``.
        input_contains: Optional case-insensitive substring the
            serialized tool ``input`` must contain.  Matched against the
            FULL (pre-cap) serialized input.  ``None`` = no filter.
        output_contains: Optional case-insensitive substring the
            correlated ``output`` must contain.  Matched against the FULL
            (pre-cap) output text.  ``None`` = no filter.
        output_excludes: Optional case-insensitive substring; a record
            whose FULL output contains it is DROPPED (e.g. harness noise
            markers).  ``None`` = no filter.
        is_error: Tri-state outcome filter.  ``None`` = all; ``True`` =
            only failed calls; ``False`` = only succeeding calls.
        output_mode: How to truncate an over-cap ``output``: ``"head"``
            (first chars), ``"tail"`` (last chars), or ``"smart"``
            (error lines + tail).  ``None`` = adaptive — ``"smart"`` for
            error records, ``"head"`` otherwise.
        redact: When ``True`` (default) secrets in emitted record fields
            (``session_title``/``input``/``intent``/``assistant``/
            ``output``) are masked as ``[REDACTED_<TYPE>]`` and the
            response carries a ``redactions`` type→count dict when any
            replacement happened (see :mod:`ai_r.redact`).  ``False``
            returns the raw content.  Filters always match the RAW,
            pre-redaction text.

    Returns:
        A dict ``{"records": [...], "count": N, "truncated": bool,
        "output_truncated": bool}``.  ``count`` is the total number of
        matches; ``truncated`` is ``True`` when more records matched than
        ``limit`` (count-based); ``output_truncated`` is ``True`` when the
        cumulative serialized size hit the response byte budget and record
        appending stopped early (size-based) — the two are independent.
        When ``count == 0`` the dict additionally carries ``"diagnostics"``
        (scanned agents + session counts, corpus date bounds, cause hints
        — see :mod:`ai_r.diagnostics`) so an empty listing is explainable.
        Each record carries ``agent``, ``session_uuid``,
        ``session_title``, ``session_date``, ``message_index``,
        ``timestamp``, ``tool``, ``tool_kind`` (the wrapper-aware
        classification, one of
        :data:`~ai_r.events._common.TOOL_KIND`), ``tool_resolved`` (the
        real name under a Skill/Task/MCP wrapper — the subagent type, the
        skill name, or ``"<server>:<tool>"`` for an MCP call; ``None``
        for non-wrappers or when the input carries no name signal),
        ``input`` (parsed dict when the raw
        input was a JSON string), ``intent`` (the immediately
        preceding user message text or ``None``), ``assistant``
        (the assistant text of the message hosting the call),
        ``is_error`` (bool: the correlated call outcome — ``True`` when
        the agent flagged the call as failed; authoritative for Claude
        and OpenCode, best-effort ``False`` for Codex/Antigravity/Pi and
        whenever no result correlates to the call) and ``output`` (the
        correlated ``tool_result`` content, ``""`` when none) plus
        ``is_error_reliable`` (bool: ``True`` only for Claude/OpenCode,
        whose outcome flag is authoritative; ``False`` for the other
        agents where ``is_error`` is best-effort) and
        ``truncated_fields`` (list naming any of ``input``/``intent``/
        ``assistant``/``output`` that were char-capped for this record;
        empty when none tripped).  The
        ``input``/``intent``/``assistant``/``output`` fields are each
        bounded to a per-field char cap; an over-cap value is sliced with
        a ``…[truncated]`` marker.  Call↔result correlation is by
        ``tool_use_id`` (Claude ``tool_use.id`` / OpenCode ``callID``);
        agents whose calls carry no id never correlate and keep
        ``is_error=False`` with an empty ``output``.

    Raises:
        ValueError: on invalid arguments — ``tool_name`` AND
            ``tool_name_pattern`` set together; both name filters omitted
            AND no content filter (none of ``input_contains``/
            ``output_contains``/``output_excludes``/``is_error``) given;
            ``limit`` negative; unparseable ``since``/``until``; unknown
            ``agent``; malformed ``session`` (empty list or non-string
            item); empty ``*_contains``/``output_excludes``; non-bool
            ``is_error``; unknown ``output_mode``.  A name filter is
            OPTIONAL: omitting both ``tool_name`` and
            ``tool_name_pattern`` is valid as long as at least one content
            filter is present (the "any tool with this signal"
            composition), but a fully unfiltered call stays a loud error.
    """
    name_exact = tool_name
    name_substr = tool_name_pattern
    if name_exact is not None and name_substr is not None:
        raise ValueError(
            "tool_name and tool_name_pattern are mutually exclusive; "
            "set at most one"
        )
    if name_exact is None and name_substr is None and not (
        input_contains is not None
        or output_contains is not None
        or output_excludes is not None
        or is_error is not None
    ):
        raise ValueError(
            "provide tool_name, tool_name_pattern, or at least one content "
            "filter (input_contains / output_contains / output_excludes / "
            "is_error)"
        )
    if name_exact is not None and (
        not isinstance(name_exact, str) or not name_exact.strip()
    ):
        raise ValueError("tool_name must be a non-empty string")
    if name_substr is not None and (
        not isinstance(name_substr, str) or not name_substr.strip()
    ):
        raise ValueError("tool_name_pattern must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(
            f"limit must be a non-negative integer, got {limit!r}"
        )
    for fname, fval in (
        ("input_contains", input_contains),
        ("output_contains", output_contains),
        ("output_excludes", output_excludes),
    ):
        if fval is not None and (not isinstance(fval, str) or not fval.strip()):
            raise ValueError(f"{fname} must be a non-empty string")
    if is_error is not None and not isinstance(is_error, bool):
        raise ValueError(
            f"is_error must be None or a bool, got {is_error!r}"
        )
    if output_mode is not None and output_mode not in {"head", "tail", "smart"}:
        raise ValueError(
            "output_mode must be one of 'head', 'tail', 'smart', "
            f"got {output_mode!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")

    since_dt = parse_iso_bound(since, "since")
    until_dt = parse_iso_bound(until, "until")
    targets = target_agents(agent)
    # F3.2-style session scope: single uuid or a list of uuids, validated
    # by the same SSOT the ``query`` facet uses (fail-loud on an empty list
    # or non-string items).  ``None`` = every session.
    wanted_sessions = normalize_session_filter(session)

    exact_lc = name_exact.strip().lower() if name_exact is not None else None
    substr_lc = name_substr.strip().lower() if name_substr is not None else None

    records: List[dict[str, Any]] = []
    # Per-agent list_sessions() results, reused by the empty-result
    # diagnostics below so an empty result never pays for a second scan.
    scanned_sessions: dict[str, Any] = {}

    for agent_name in targets:
        parser = PARSERS[agent_name]
        agent_sessions = parser.list_sessions()
        scanned_sessions[agent_name.value.lower()] = agent_sessions
        for sess in agent_sessions:
            if wanted_sessions is not None and sess.uuid not in wanted_sessions:
                continue
            try:
                messages = parser.read_messages(sess.uuid)
            except (FileNotFoundError, ValueError, OSError):
                continue
            session_iso = iso(sess.date)
            session_title = sess.title
            session_ts: Optional[Any] = to_utc_aware(sess.date)
            # Correlate each tool call with its result (which lives on a
            # DIFFERENT, following message) by ``tool_use_id``.  Both the
            # ``tool_use`` call and its ``tool_result`` carry the same id when
            # the source format exposes one (Claude ``tool_use.id``, OpenCode
            # ``callID``).  ``is_error`` is authoritative for Claude/OpenCode
            # and best-effort ``False`` elsewhere; agents whose ``tool_use``
            # lacks an id simply won't correlate and default to no-outcome.
            result_by_id: dict[str, dict[str, Any]] = {}
            for m in messages:
                for tr in getattr(m, "tool_result", ()) or ():
                    if not isinstance(tr, dict):
                        continue
                    tr_id = tr.get("tool_use_id")
                    if isinstance(tr_id, str) and tr_id:
                        result_by_id[tr_id] = tr
            for idx, msg in enumerate(messages):
                if msg.role != "assistant":
                    continue
                if not msg.tool_use:
                    continue
                msg_ts: Optional[Any] = to_utc_aware(
                    getattr(msg, "timestamp", None)
                )
                intent = previous_user_intent(messages, idx)
                for tool in msg.tool_use:
                    if not isinstance(tool, dict):
                        continue
                    name = tool.get("name", "")
                    if not isinstance(name, str):
                        continue
                    name_lc = name.lower()
                    if exact_lc is not None:
                        if name_lc != exact_lc:
                            continue
                    elif substr_lc is not None:
                        if substr_lc not in name_lc:
                            continue
                    # Neither name filter set: any tool name passes; the
                    # content filters below (input/output/is_error) carry
                    # the selection.
                    tool_ts: Optional[Any] = to_utc_aware(
                        tool.get("timestamp")
                    )
                    call_ts: Optional[Any] = (
                        tool_ts if tool_ts is not None
                        else msg_ts if msg_ts is not None
                        else session_ts
                    )
                    if since_dt is not None and (
                        call_ts is None or call_ts < since_dt
                    ):
                        continue
                    if until_dt is not None and (
                        call_ts is None or call_ts > until_dt
                    ):
                        continue
                    # Correlated outcome (default: no result found → not an
                    # error, empty output).  ``tool_use_id`` is present on the
                    # call for Claude/OpenCode; other agents leave it absent so
                    # this stays a no-op there.
                    tu_id = tool.get("tool_use_id")
                    result = (
                        result_by_id.get(tu_id)
                        if isinstance(tu_id, str) and tu_id
                        else None
                    )
                    call_is_error = (
                        bool(result.get("is_error")) if result else False
                    )
                    raw_output = result.get("content", "") if result else ""

                    # --- Content filters (cheap; run BEFORE capping) --------
                    # Match on the FULL, pre-cap text so a filter never misses
                    # a hit that fell past the field cap.  Records that fail
                    # any active filter are skipped before record assembly.
                    coerced_input = _coerce_input(tool.get("input", ""))
                    if isinstance(coerced_input, str):
                        input_str = coerced_input
                    else:
                        try:
                            input_str = json.dumps(
                                coerced_input, ensure_ascii=False
                            )
                        except (TypeError, ValueError):
                            input_str = str(coerced_input)
                    output_str = (
                        raw_output if isinstance(raw_output, str)
                        else str(raw_output)
                    )
                    if is_error is not None and call_is_error != is_error:
                        continue
                    if input_contains is not None and not _match_ci(
                        input_str, input_contains
                    ):
                        continue
                    if output_contains is not None and not _match_ci(
                        output_str, output_contains
                    ):
                        continue
                    if output_excludes is not None and _match_ci(
                        output_str, output_excludes
                    ):
                        continue

                    capped_input, input_trunc = _cap_field(
                        coerced_input, _INPUT_CHARS_CAP
                    )
                    capped_intent, intent_trunc = _cap_field(
                        intent, _INTENT_CHARS_CAP
                    )
                    capped_asst, asst_trunc = _cap_field(
                        msg.text or "", _ASSISTANT_CHARS_CAP
                    )
                    # Adaptive default: surface error lines on failures,
                    # legacy head-slice on successes; explicit ``output_mode``
                    # overrides.
                    eff_mode = output_mode or (
                        "smart" if call_is_error else "head"
                    )
                    capped_output, output_trunc = _cap_output(
                        output_str, _OUTPUT_CHARS_CAP, eff_mode
                    )
                    truncated_fields = [
                        f for f, hit in (
                            ("input", input_trunc),
                            ("intent", intent_trunc),
                            ("assistant", asst_trunc),
                            ("output", output_trunc),
                        ) if hit
                    ]
                    # F3.1: wrapper-aware classification + the real name
                    # under a Skill/Task/MCP wrapper (None when the input
                    # carries no recognisable name — honest, never guessed).
                    call_kind, call_resolved = resolve_tool(
                        name, coerced_input
                    )
                    records.append({
                        "agent": agent_name.value.lower(),
                        "session_uuid": sess.uuid,
                        "session_title": session_title,
                        "session_date": session_iso,
                        "message_index": idx,
                        "timestamp": (
                            iso(call_ts) if call_ts is not None else None
                        ),
                        "tool": name,
                        "tool_kind": call_kind,
                        "tool_resolved": call_resolved,
                        "input": capped_input,
                        "intent": capped_intent,
                        "assistant": capped_asst,
                        "is_error": call_is_error,
                        "is_error_reliable": (
                            agent_name.value.lower() in {"claude", "opencode"}
                        ),
                        "output": capped_output,
                        "truncated_fields": truncated_fields,
                    })

    records.sort(key=lambda r: (r["timestamp"] is None, r["timestamp"] or ""))
    total = len(records)
    truncated = False
    if limit and len(records) > limit:
        records = records[:limit]
        truncated = True

    # Emission-time redaction (F2.1): runs AFTER the limit slice so only
    # emitted records pay for it, and after the field caps so the cost is
    # bounded by the response size.  Filters above already matched on the
    # RAW pre-redaction text.
    redactions: dict[str, int] = {}
    if redact:
        for rec in records:
            for field in ("session_title", "input", "intent",
                          "assistant", "output", "tool_resolved"):
                new_val, counts = redact_value(rec.get(field))
                if counts:
                    rec[field] = new_val
                    merge_redaction_counts(redactions, counts)

    # Size-based safeguard: stop emitting records once the cumulative
    # serialized size exceeds the response byte budget.  Distinct from the
    # count-based ``truncated`` above — ``output_truncated`` means "output
    # capped by size", so a caller can tell "more records exist" (raise
    # ``limit``) from "output too big" (fields already field-capped, but the
    # sheer record count blew the budget).
    output_truncated = False
    budgeted: List[dict[str, Any]] = []
    running = 0
    for rec in records:
        running += len(json.dumps(rec, ensure_ascii=False, default=str))
        if running > _OUTPUT_BYTES_BUDGET and budgeted:
            output_truncated = True
            break
        budgeted.append(rec)
    if output_truncated:
        records = budgeted

    response: dict[str, Any] = {
        "records": records,
        "count": total,
        "truncated": truncated,
        "output_truncated": output_truncated,
    }
    if redactions:
        response["redactions"] = redactions
    if total == 0:
        # Zero matches: attach the corpus diagnostics so an empty listing
        # is explainable (missing source dir vs all-excluding filter vs a
        # genuine no-match).  Imported lazily to keep module import light
        # and mirror ``find_file_edits``.
        from ai_r.diagnostics import empty_result_diagnostics

        response["diagnostics"] = empty_result_diagnostics(
            agent=agent,
            since=since,
            until=until,
            filters={
                "session": session,
                "tool_name": tool_name,
                "tool_name_pattern": tool_name_pattern,
                "input_contains": input_contains,
                "output_contains": output_contains,
                "output_excludes": output_excludes,
                "is_error": is_error,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return response
