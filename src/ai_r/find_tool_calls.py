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
from typing import Any, List, Optional

from ai_r.find_file_edits import (
    parse_iso_bound,
    previous_user_intent,
    to_utc_aware,
)
from ai_r.parsers import (
    PARSERS,
    iso,
    target_agents,
)
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


def _cap_field(value: Any, cap: int) -> tuple[Any, bool]:
    """Return ``(value, truncated)`` bounding a field to ``cap`` chars.

    A ``str`` longer than ``cap`` is sliced with a trailing marker.  A
    non-string value (parsed dict/list) is serialized to measure size; only
    when its JSON form exceeds ``cap`` do we replace it with the truncated
    string form (small structured inputs pass through unchanged as the parsed
    object).  ``None`` and short values are returned untouched.
    """
    if value is None:
        return value, False
    if isinstance(value, str):
        if len(value) > cap:
            return value[:cap] + "…[truncated]", True
        return value, False
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = str(value)
    if len(serialized) > cap:
        return serialized[:cap] + "…[truncated]", True
    return value, False


def find_tool_calls(
    *,
    tool_name: Optional[str] = None,
    tool_name_pattern: Optional[str] = None,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
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
        since: Optional ISO 8601 lower bound (inclusive) on call
            timestamp.  Pass ``""`` or ``None`` to leave open.
        until: Optional ISO 8601 upper bound (inclusive) on call
            timestamp.  Pass ``""`` or ``None`` to leave open.
        limit: Maximum records to return.  ``0`` = no cap.  Default
            ``100``.

    Returns:
        A dict ``{"records": [...], "count": N, "truncated": bool,
        "output_truncated": bool}``.  ``count`` is the total number of
        matches; ``truncated`` is ``True`` when more records matched than
        ``limit`` (count-based); ``output_truncated`` is ``True`` when the
        cumulative serialized size hit the response byte budget and record
        appending stopped early (size-based) — the two are independent.
        Each record carries ``agent``, ``session_uuid``,
        ``session_title``, ``session_date``, ``message_index``,
        ``timestamp``, ``tool``, ``input`` (parsed dict when the raw
        input was a JSON string), ``intent`` (the immediately
        preceding user message text or ``None``), ``assistant``
        (the assistant text of the message hosting the call),
        ``is_error`` (bool: the correlated call outcome — ``True`` when
        the agent flagged the call as failed; authoritative for Claude
        and OpenCode, best-effort ``False`` for Codex/Antigravity/Pi and
        whenever no result correlates to the call) and ``output`` (the
        correlated ``tool_result`` content, ``""`` when none) plus
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
        ValueError: on invalid arguments (neither or both of
            ``tool_name``/``tool_name_pattern`` set, ``limit``
            negative, unparseable ``since``/``until``, unknown
            ``agent``).
    """
    name_exact = tool_name
    name_substr = tool_name_pattern
    if (name_exact is None) == (name_substr is None):
        raise ValueError(
            "exactly one of tool_name or tool_name_pattern must be set"
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

    since_dt = parse_iso_bound(since, "since")
    until_dt = parse_iso_bound(until, "until")
    targets = target_agents(agent)

    exact_lc = name_exact.strip().lower() if name_exact is not None else None
    substr_lc = name_substr.strip().lower() if name_substr is not None else None

    records: List[dict[str, Any]] = []

    for agent_name in targets:
        parser = PARSERS[agent_name]
        for session in parser.list_sessions():
            try:
                messages = parser.read_messages(session.uuid)
            except (FileNotFoundError, ValueError, OSError):
                continue
            session_iso = iso(session.date)
            session_title = session.title
            session_ts: Optional[Any] = to_utc_aware(session.date)
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
                    else:
                        assert substr_lc is not None
                        if substr_lc not in name_lc:
                            continue
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
                    capped_input, input_trunc = _cap_field(
                        _coerce_input(tool.get("input", "")), _INPUT_CHARS_CAP
                    )
                    capped_intent, intent_trunc = _cap_field(
                        intent, _INTENT_CHARS_CAP
                    )
                    capped_asst, asst_trunc = _cap_field(
                        msg.text or "", _ASSISTANT_CHARS_CAP
                    )
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
                    is_error = bool(result.get("is_error")) if result else False
                    raw_output = result.get("content", "") if result else ""
                    capped_output, output_trunc = _cap_field(
                        raw_output, _OUTPUT_CHARS_CAP
                    )
                    truncated_fields = [
                        f for f, hit in (
                            ("input", input_trunc),
                            ("intent", intent_trunc),
                            ("assistant", asst_trunc),
                            ("output", output_trunc),
                        ) if hit
                    ]
                    records.append({
                        "agent": agent_name.value.lower(),
                        "session_uuid": session.uuid,
                        "session_title": session_title,
                        "session_date": session_iso,
                        "message_index": idx,
                        "timestamp": (
                            iso(call_ts) if call_ts is not None else None
                        ),
                        "tool": name,
                        "input": capped_input,
                        "intent": capped_intent,
                        "assistant": capped_asst,
                        "is_error": is_error,
                        "output": capped_output,
                        "truncated_fields": truncated_fields,
                    })

    records.sort(key=lambda r: (r["timestamp"] is None, r["timestamp"] or ""))
    total = len(records)
    truncated = False
    if limit and len(records) > limit:
        records = records[:limit]
        truncated = True

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

    return {
        "records": records,
        "count": total,
        "truncated": truncated,
        "output_truncated": output_truncated,
    }
