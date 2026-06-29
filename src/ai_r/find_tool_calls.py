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

__all__ = [
    "find_tool_calls",
]

# Refuse to JSON-decode tool inputs above this size.  Codex sessions
# can carry ``function_call.arguments`` payloads in the tens of MB
# (base64 blobs, etc.); decoding those is a memory-exhaustion vector.
_MAX_INPUT_BYTES = 1_000_000  # 1 MB


def _coerce_input(raw: Any) -> Any:
    """Best-effort JSON decode of a tool input payload.

    Some agents (codex ``function_call``) carry the input as a JSON
    string; others (claude ``tool_use``) carry a dict directly.  When
    ``raw`` is a non-empty string we try ``json.loads``; on success the
    decoded value is returned, otherwise the original string is kept
    (so non-JSON payloads still surface to the caller).  Non-string
    inputs are returned unchanged.

    Strings larger than :data:`_MAX_INPUT_BYTES` are returned as-is
    without attempting ``json.loads`` to avoid unbounded memory use.
    """
    if isinstance(raw, str):
        if len(raw) > _MAX_INPUT_BYTES:
            return raw
        if raw.strip():
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return raw
    return raw


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
        A dict ``{"records": [...], "count": N, "truncated": bool}``.
        Each record carries ``agent``, ``session_uuid``,
        ``session_title``, ``session_date``, ``message_index``,
        ``timestamp``, ``tool``, ``input`` (parsed dict when the raw
        input was a JSON string), ``intent`` (the immediately
        preceding user message text or ``None``) and ``assistant``
        (the assistant text of the message hosting the call).

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
                        "input": _coerce_input(tool.get("input", "")),
                        "intent": intent,
                        "assistant": msg.text or "",
                    })

    records.sort(key=lambda r: (r["timestamp"] is None, r["timestamp"] or ""))
    total = len(records)
    truncated = False
    if limit and len(records) > limit:
        records = records[:limit]
        truncated = True
    return {"records": records, "count": total, "truncated": truncated}
