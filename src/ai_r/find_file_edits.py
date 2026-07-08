"""Cross-agent ``find_file_edits`` core, shared by the CLI and MCP server.

The pure-Python scan logic lives here so the CLI and the MCP tool
both delegate to a single implementation.  The MCP tool is a thin
wrapper (see :mod:`ai_r.mcp_server`) that catches
:class:`ValueError` and converts it to the MCP error-dict
convention; the CLI handler (:func:`ai_r.cli._run_find_file_edits`)
catches the same exception and prints it to stderr.

The module also re-exports the small set of helpers that downstream
consumers historically imported from :mod:`ai_r.mcp_server`
(``_target_agents``, ``_coerce_agent``, ``_PARSERS``,
``_EDIT_TOOLS``, ``_EDIT_PATH_KEYS``) so existing call sites and
tests keep working.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence

from ai_r.parsers import (
    PARSERS,
    coerce_agent,
    iso,
    target_agents,
)
from ai_r.redact import merge_redaction_counts, redact_value

__all__ = [
    "EDIT_TOOLS",
    "EDIT_PATH_KEYS",
    # ``PARSERS``, ``coerce_agent``, ``target_agents`` and ``iso`` are
    # re-exported from :mod:`ai_r.parsers` (the canonical source of
    # truth) so downstream consumers and tests that historically imported
    # them from here keep working.
    "PARSERS",
    "cap_field",
    "coerce_agent",
    "target_agents",
    "iso",
    "parse_iso_bound",
    "to_utc_aware",
    "edit_path_from_input",
    "previous_user_intent",
    "find_file_edits",
]


EDIT_TOOLS: frozenset[str] = frozenset({
    "Edit", "edit", "Write", "write",
    "MultiEdit", "NotebookEdit",
    "str_replace", "patch", "file",
    "file_edit", "write_file", "create_file", "apply_patch",
    "edit_file", "update_file", "multi_edit",
})


EDIT_PATH_KEYS: tuple[str, ...] = ("file_path", "notebook_path", "path")


# Codex CLI routes file writes through a shell-exec tool (``exec_command`` /
# ``local_shell_call``) instead of a structured edit tool, so the target path
# lives inside the shell command string. These tool names trigger a
# conservative quote-aware redirection scan (see :func:`_shell_redirect_targets`).
_SHELL_EXEC_TOOLS: frozenset[str] = frozenset({"exec_command", "local_shell_call"})


# --- Per-record field caps (chars) + total-response byte budget ------------
# Mirrors ``find_tool_calls``: ``limit`` bounds the record COUNT only, never
# bytes.  Without caps a single record can carry an uncapped user ``intent``
# (a whole pasted document) / uncapped ``assistant`` text, so a handful of
# records blow the MCP response far past any sane size (observed: a
# 3.2M-char response).  Over-long fields are truncated at emission time with
# a ``…[truncated]`` marker and named in the per-record ``truncated_fields``.
# The opt-in full ``input`` body (``include_input=True``) is deliberately NOT
# field-capped — that flag *promises* the full body (``get_body`` is the
# bounded on-demand route) — but it does count toward the byte budget.
_INTENT_CHARS_CAP = 1_000     # preceding user message text
_ASSISTANT_CHARS_CAP = 4_000  # assistant message text hosting the edit

# Cumulative serialized size after which record emission stops and the
# top-level ``output_truncated`` flag is set (DISTINCT from the count-based
# ``truncated``: the former means "output capped by size", the latter "more
# records matched than ``limit``").  Generous — only bites pathological
# output.  Same value as ``find_tool_calls``.
_OUTPUT_BYTES_BUDGET = 4_000_000  # ~4 MB of serialized records


def cap_field(value: Any, cap: int) -> tuple[Any, bool]:
    """Return ``(value, truncated)`` bounding a field to ``cap`` chars.

    A ``str`` longer than ``cap`` is sliced with a trailing marker.  A
    non-string value (parsed dict/list) is serialized to measure size; only
    when its JSON form exceeds ``cap`` do we replace it with the truncated
    string form (small structured inputs pass through unchanged as the parsed
    object).  ``None`` and short values are returned untouched.

    Shared by :func:`find_file_edits` and
    :func:`ai_r.find_tool_calls.find_tool_calls` (this module is the
    import-order base of the two).
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


def parse_iso_bound(value: Optional[str], name: str) -> Optional[datetime]:
    """Parse an ISO 8601 bound string for the ``find_file_edits`` filter.

    Returns ``None`` for empty/``None`` input (meaning "unbounded"). Raises
    :class:`ValueError` with a clear message on unparseable input.  The
    returned datetime is UTC-aware: ``Z`` and explicit offsets are honoured;
    naive strings (no offset) are interpreted as UTC.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{name} must be an ISO 8601 string, got {value!r}: {exc}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a datetime to UTC-aware for safe comparison with aware bounds.

    ``None`` passes through.  Naive datetimes are assumed to be UTC (which
    matches what every parser produces — they're either tz-aware UTC
    epochs or naive ISO strings; treating both as UTC is the only
    consistent rule).  Aware datetimes are converted to UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def edit_path_from_input(payload: object) -> Optional[str]:
    """Return the first non-empty path-like value from a tool input dict.

    Checks the conventional top-level keys (``file_path``,
    ``notebook_path``, ``path``) first; falls back to walking a
    ``files[*].path`` list — used by opencode ``patch`` parts and
    any other multi-file payload.  Returns ``None`` for unrecognised
    shapes.
    """
    if not isinstance(payload, dict):
        return None
    for key in EDIT_PATH_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    files = payload.get("files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, dict):
                continue
            for key in EDIT_PATH_KEYS:
                val = entry.get(key)
                if isinstance(val, str) and val:
                    return val
    return None


def _extract_shell_command(raw_input: object) -> str:
    """Best-effort recovery of the command string from a shell-exec tool input.

    Handles the dict shapes codex ``exec_command`` / ``local_shell_call`` use
    (``cmd`` / ``command`` / ``args`` / ``argv``; ``command``/``args`` may be
    an argv list joined into one line) and bare strings. Returns ``""`` when
    no command can be recovered.
    """
    payload: object = raw_input
    if isinstance(raw_input, str) and raw_input.strip():
        try:
            payload = json.loads(raw_input)
        except (ValueError, TypeError):
            return raw_input
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("cmd", "command", "args", "argv"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, list) and val:
                return " ".join(str(v) for v in val)
    return ""


def _shell_redirect_targets(cmd: str) -> list[tuple[str, bool]]:
    """Return ``(path, is_append)`` for file-write redirections in a shell cmd.

    Quote-aware: ``>`` inside single/double quotes — e.g. regex or grep
    patterns like ``"<h[^>]*>"`` — is ignored. Only stdout redirections
    count (``>`` / ``>>`` / ``&>`` / ``&>>``); numeric fd redirects
    (``2>`` / ``1>``) and arrows inside words (``->`` / ``=>``) are skipped.

    Conservative by design: ``find_file_edits`` is an audit tool, so false
    negatives are preferred over false positives. Writes via ``tee`` /
    ``sed -i`` / ``cp`` / ``mv`` are NOT detected (documented limitation) —
    redirect-head writes like ``printf '...' > path`` and ``cat > path
    <<EOF`` are.
    """
    targets: list[tuple[str, bool]] = []
    i, n = 0, len(cmd)
    in_s = in_d = False
    # A redirection is suppressed when the char before the '>' looks like a
    # fd marker (``2>``), a chained '>' (``>>`` handled separately), or an
    # in-word arrow (``->`` / ``=>`` / ``:>`` / path sep).
    skip_prev = set("0123456789>-=:/")
    stops = set(" \t\n;|&<>()'\"")
    while i < n:
        c = cmd[i]
        if not in_d and c == "'":
            in_s = not in_s
            i += 1
            continue
        if not in_s and c == '"':
            in_d = not in_d
            i += 1
            continue
        if in_s or in_d:
            i += 1
            continue
        is_amp = c == "&" and i + 1 < n and cmd[i + 1] == ">"
        if c == ">" or is_amp:
            op_start = i
            gt = i + 1 if is_amp else i  # index of the '>' itself
            prev = cmd[op_start - 1] if op_start > 0 else ""
            append = gt + 1 < n and cmd[gt + 1] == ">"
            last = gt + 1 if append else gt
            if prev in skip_prev:
                i = last + 1
                continue
            k = last + 1
            while k < n and cmd[k] in " \t":
                k += 1
            start = k
            while k < n and cmd[k] not in stops:
                k += 1
            path = cmd[start:k].strip()
            if path and not path.startswith("&"):
                targets.append((path, append))
            i = k
            continue
        i += 1
    return targets


def _input_reference(input_obj: Any) -> tuple[str, int]:
    """Return ``(sha256, chars)`` for an edit's ``input`` payload.

    Used by :func:`find_file_edits` when ``include_input=False`` to emit a
    light-weight *reference* to the body instead of inlining it: the auditor
    sees a body exists (``input_chars > 0``) and can fetch it on demand via
    ``get_body`` / ``read_session`` keyed by ``session_uuid`` +
    ``message_index``.  The hash is over the JSON-canonical form (sorted keys,
    non-ASCII preserved) so it is deterministic across runs; ``chars`` is the
    length of that same canonical form.
    """
    canonical = json.dumps(
        input_obj, sort_keys=True, ensure_ascii=False, default=str
    )
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return sha, len(canonical)


def previous_user_intent(
    messages: Sequence[Any], index: int
) -> Optional[str]:
    """Walk backwards from ``index`` to find the previous user text."""
    for j in range(index - 1, -1, -1):
        if j < 0 or j >= len(messages):
            continue
        msg = messages[j]
        role = getattr(msg, "role", None)
        text = getattr(msg, "text", "") or ""
        if role == "user" and isinstance(text, str) and text.strip():
            return text
    return None


def find_file_edits(
    *,
    path: str,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
    include_input: bool = True,
    redact: bool = True,
    size_caps: bool = True,
) -> dict[str, Any]:
    """Find every file edit across sessions, cross-agent by default.

    Args:
        path: Substring matched against ``file_path`` / ``notebook_path``
            / ``path`` fields in the tool input (case-sensitive).
        agent: Optional filter, one of ``"claude"``, ``"codex"``,
            ``"opencode"``, ``"antigravity"``, ``"pi"``. ``None`` =
            all agents.
        since: Optional ISO 8601 lower bound (inclusive) on edit
            timestamp. Pass ``""`` or ``None`` to leave open.
        until: Optional ISO 8601 upper bound (inclusive) on edit
            timestamp. Pass ``""`` or ``None`` to leave open.
        limit: Maximum records to return. ``0`` = no cap. Default ``100``.
        include_input: When ``True`` (the default — kept for backward
            compatibility with in-repo consumers), each record inlines the
            full edit body under ``"input"``.  When ``False``
            (*reference-by-default*), the body is **not** inlined; instead the
            record carries a light-weight reference — ``"input_sha256"``
            (hash of the JSON-canonical body) and ``"input_chars"`` (its
            length) — so an audit listing stays small while still signalling
            a body exists.  Fetch the body on demand via ``get_body`` /
            ``read_session`` keyed by ``session_uuid`` + ``message_index``.
        redact: When ``True`` (default) secrets in emitted record fields
            (``session_title``/``intent``/``assistant``/``input``) are
            masked as ``[REDACTED_<TYPE>]`` and the response carries a
            ``redactions`` type→count dict when any replacement happened
            (see :mod:`ai_r.redact`).  ``False`` returns raw content.
            The ``path`` filter and ``input_sha256`` reference always use
            the RAW, pre-redaction content.
        size_caps: When ``True`` (default) emitted records are size-bounded
            like ``find_tool_calls``: ``intent`` / ``assistant`` are capped
            (:data:`_INTENT_CHARS_CAP` / :data:`_ASSISTANT_CHARS_CAP`, cut
            with a ``…[truncated]`` marker and named in the per-record
            ``truncated_fields``), and emission stops once the cumulative
            serialized size exceeds :data:`_OUTPUT_BYTES_BUDGET` (top-level
            ``output_truncated``).  The opt-in full ``input`` body is never
            field-capped (``include_input=True`` promises the full body) but
            counts toward the budget.  Internal rollups (``session_stats`` /
            ``file_frequency``) pass ``False``: they fold DISTINCT intents on
            raw text (a cap could merge two long intents — a count drift) and
            must never lose records to a byte budget.

    Returns:
        A dict ``{"records": [...], "count": N, "truncated": bool,
        "output_truncated": bool}``.  Each record carries ``"input"`` when
        ``include_input=True``, else ``"input_sha256"`` + ``"input_chars"``;
        with ``size_caps`` it also carries ``"truncated_fields"`` (the
        fields the per-record cap cut, ``[]`` when none).  When
        ``count == 0`` the dict additionally carries ``"diagnostics"``
        (scanned agents + session counts, corpus date bounds, cause hints —
        see :mod:`ai_r.diagnostics`) so an empty listing is explainable.

    Raises:
        ValueError: on invalid arguments (``path`` empty, ``limit`` negative,
            unparseable ``since``/``until``, unknown ``agent``).

    Codex CLI note: codex writes files through a shell-exec tool
    (``exec_command`` / ``local_shell_call``), so the target path is recovered
    from the command string via a conservative redirection scan
    (:func:`_shell_redirect_targets`). One such tool call can yield several
    records when it writes multiple files (``a > f1 && b > f2``).
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if not isinstance(limit, int) or limit < 0:
        raise ValueError(
            f"limit must be a non-negative integer, got {limit!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")
    if not isinstance(size_caps, bool):
        raise ValueError(f"size_caps must be a bool, got {size_caps!r}")

    since_dt = parse_iso_bound(since, "since")
    until_dt = parse_iso_bound(until, "until")
    targets = target_agents(agent)

    records: List[dict[str, Any]] = []
    # Per-agent list_sessions() results, reused by the empty-result
    # diagnostics below so an empty result never pays for a second scan.
    scanned_sessions: dict[str, Any] = {}

    for agent_name in targets:
        parser = PARSERS[agent_name]
        agent_sessions = parser.list_sessions()
        scanned_sessions[agent_name.value.lower()] = agent_sessions
        for session in agent_sessions:
            try:
                messages = parser.read_messages(session.uuid)
            except (FileNotFoundError, ValueError, OSError):
                continue
            session_iso = iso(session.date)
            session_title = session.title
            session_ts: Optional[datetime] = to_utc_aware(session.date)
            for idx, msg in enumerate(messages):
                if msg.role != "assistant":
                    continue
                if not msg.tool_use:
                    continue
                msg_ts: Optional[datetime] = to_utc_aware(
                    getattr(msg, "timestamp", None)
                )
                intent = previous_user_intent(messages, idx)
                for tool in msg.tool_use:
                    if not isinstance(tool, dict):
                        continue
                    name = tool.get("name", "")
                    is_shell = name in _SHELL_EXEC_TOOLS
                    if name not in EDIT_TOOLS and not is_shell:
                        continue
                    tool_ts: Optional[datetime] = to_utc_aware(tool.get("timestamp"))
                    edit_ts: Optional[datetime] = (
                        tool_ts if tool_ts is not None
                        else msg_ts if msg_ts is not None
                        else session_ts
                    )
                    if since_dt is not None and (
                        edit_ts is None or edit_ts < since_dt
                    ):
                        continue
                    if until_dt is not None and (
                        edit_ts is None or edit_ts > until_dt
                    ):
                        continue
                    # Build (file, input, tool) candidates for this call.
                    # Structured edit tools yield one; shell-exec tools may
                    # yield several (one per redirected file path).
                    if is_shell:
                        cmd = _extract_shell_command(tool.get("input", ""))
                        candidates: List[tuple[str, dict[str, Any], str]] = [
                            (fpath, {"cmd": cmd, "edit": "append" if append else "write"}, name)
                            for fpath, append in _shell_redirect_targets(cmd)
                            if path in fpath
                        ]
                    else:
                        raw_input = tool.get("input", "")
                        payload: object = raw_input
                        if isinstance(raw_input, str) and raw_input.strip():
                            try:
                                payload = json.loads(raw_input)
                            except (ValueError, TypeError):
                                payload = raw_input
                        file_path = edit_path_from_input(payload)
                        if file_path is None or path not in file_path:
                            candidates = []
                        else:
                            candidates = [(
                                file_path,
                                payload if isinstance(payload, dict) else {},
                                name,
                            )]
                    for file_path, input_obj, tool_label in candidates:
                        record: dict[str, Any] = {
                            "agent": agent_name.value.lower(),
                            "session_uuid": session.uuid,
                            "session_title": session_title,
                            "session_date": session_iso,
                            "message_index": idx,
                            "timestamp": iso(edit_ts) if edit_ts is not None else None,
                            "tool": tool_label,
                            "file": file_path,
                            "intent": intent,
                            "assistant": msg.text or "",
                        }
                        if include_input:
                            record["input"] = input_obj
                        else:
                            # Reference-by-default: signal the body exists
                            # without inlining it (see ``include_input`` doc).
                            sha, chars = _input_reference(input_obj)
                            record["input_sha256"] = sha
                            record["input_chars"] = chars
                        records.append(record)

    records.sort(key=lambda r: (r["timestamp"] is None, r["timestamp"] or ""))
    total = len(records)
    truncated = False
    if limit and len(records) > limit:
        records = records[:limit]
        truncated = True

    # Emission-time per-record field caps (mirrors ``find_tool_calls``):
    # after the limit slice so only emitted records pay, BEFORE redaction so
    # the redaction cost is bounded by the response size.  Gated by
    # ``size_caps`` — internal rollups need raw, complete records.
    if size_caps:
        for rec in records:
            truncated_fields: List[str] = []
            for field, cap in (
                ("intent", _INTENT_CHARS_CAP),
                ("assistant", _ASSISTANT_CHARS_CAP),
            ):
                new_val, hit = cap_field(rec.get(field), cap)
                if hit:
                    rec[field] = new_val
                    truncated_fields.append(field)
            rec["truncated_fields"] = truncated_fields

    # Emission-time redaction (F2.1): after the limit slice so only emitted
    # records pay for it.  The ``path`` filter and the ``input_sha256``
    # reference above were computed on the RAW content.
    redactions: dict[str, int] = {}
    if redact:
        for rec in records:
            for field in ("session_title", "intent", "assistant", "input"):
                if field not in rec:
                    continue
                new_val, counts = redact_value(rec[field])
                if counts:
                    rec[field] = new_val
                    merge_redaction_counts(redactions, counts)

    # Size-based safeguard (mirrors ``find_tool_calls``): stop emitting
    # records once the cumulative serialized size exceeds the response byte
    # budget.  Distinct from the count-based ``truncated`` above —
    # ``output_truncated`` means "output capped by size", so a caller can
    # tell "more records exist" (raise ``limit``) from "output too big"
    # (fields already capped, but the sheer record count blew the budget).
    output_truncated = False
    if size_caps:
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

    result: dict[str, Any] = {
        "records": records, "count": total, "truncated": truncated,
        "output_truncated": output_truncated,
    }
    if redactions:
        result["redactions"] = redactions
    if total == 0:
        # Zero matches: attach the corpus diagnostics so an empty listing
        # is explainable (missing source dir vs all-excluding filter vs a
        # genuine no-match).  Imported lazily — ``ai_r.diagnostics``
        # imports helpers from THIS module, so a top-level import here
        # would be a cycle.
        from ai_r.diagnostics import empty_result_diagnostics

        result["diagnostics"] = empty_result_diagnostics(
            agent=agent, since=since, until=until, filters={"path": path},
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return result
