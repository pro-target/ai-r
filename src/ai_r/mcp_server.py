"""MCP server entry point for ai-r.

Exposes three tools over the Model Context Protocol:

* :func:`list_sessions`  — enumerate sessions, optionally filtered by agent.
* :func:`read_session`   — load a single session by ``uuid`` and ``agent``.
* :func:`search_sessions` — case-insensitive search across title and/or
  message bodies with AND/OR/NOT operators and Google-style ``-term``
  negative prefixes.

Errors are returned as dicts (never raised) so the MCP client can
surface them in a structured way.

Transport: stdio.  No logging, no stdout writes outside the MCP
protocol — that would corrupt the JSON-RPC stream.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from ai_r import __version__  # noqa: E402
from ai_r.find_file_edits import (  # noqa: E402
    PARSERS as _PARSERS,
    coerce_agent as _coerce_agent,
    find_file_edits as _find_file_edits_core,
    iso as _iso,
    target_agents as _target_agents,
)
from ai_r.parsers import Session  # noqa: E402

__all__ = ["mcp", "main"]


_MESSAGES_CAP = 100
_LIST_LIMIT_DEFAULT = 100


mcp = FastMCP(
    name="ai-r",
    instructions=(
        "ai-r: read Claude, Codex, OpenCode, Antigravity and Pi session "
        f"files. Server version: {__version__}."
    ),
)


def _session_summary(session: Session) -> dict[str, Any]:
    """Project a :class:`Session` to a JSON-safe summary dict."""
    return {
        "uuid": session.uuid,
        "agent": session.agent.value,
        "title": session.title,
        "date": _iso(session.date),
        "message_count": session.message_count,
    }


def _codex_text(parts: object) -> str:
    """Concatenate Codex message parts into a single string.

    .. deprecated::
        Kept as a thin backcompat helper for existing callers/tests; the
        canonical path is :func:`ai_r.parsers.codex.read_messages`.
    """
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    chunks: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text", "")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


def _pi_text(parts: object) -> str:
    """Concatenate Pi text parts, skipping thinking/tool-call blocks.

    .. deprecated::
        Kept as a thin backcompat helper for existing callers/tests; the
        canonical path is :func:`ai_r.parsers.pi.read_messages`.
    """
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    chunks: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type", "") not in ("text", "input_text", "output_text", ""):
            continue
        text = part.get("text", "")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


def _project_message_content(m: Any) -> str:
    """Return compact MCP content for text or user/assistant tool-only messages."""
    text = getattr(m, "text", "")
    if isinstance(text, str) and text:
        return text

    chunks: List[str] = []
    for tool in getattr(m, "tool_use", ()) or ():
        name = tool.get("name") if isinstance(tool, dict) else None
        chunks.append(f"[tool_use: {name}]" if name else "[tool_use]")
    for _ in getattr(m, "tool_result", ()) or ():
        chunks.append("[tool_result]")
    return "\n".join(chunks)


def _project_messages(messages: Sequence[Any]) -> List[dict[str, Any]]:
    """Project parser ``Message`` objects to ``{role, content}`` dicts.

    Only ``user``/``assistant`` roles are surfaced, preserving the
    historical MCP output shape; ``tool`` messages are dropped.  No cap
    is applied here — pagination (``offset``/``limit``) is the caller's
    responsibility.
    """
    out: List[dict[str, Any]] = []
    for m in messages:
        if m.role not in ("user", "assistant"):
            continue
        content = _project_message_content(m)
        if not content:
            continue
        out.append({"role": m.role, "content": content})
    return out


def _extract_messages(
    session: Session,
    offset: int = 0,
    limit: int = _MESSAGES_CAP,
) -> List[dict[str, Any]]:
    """Best-effort message extraction for a session, with pagination.

    Single dispatcher covering ALL supported agents
    (claude/codex/opencode/pi/antigravity): resolves the owning parser
    from :data:`_PARSERS`, calls its public ``read_messages(session.uuid)``,
    projects each :class:`~ai_r.parsers.models.Message` to a
    ``{role, content}`` dict, then applies ``[offset:offset+limit]``.
    Only ``user``/``assistant`` roles surface (historical MCP shape);
    ``tool`` messages are dropped before pagination, so ``offset``/``limit``
    index into the *projected* list.

    ``limit`` defaults to :data:`_MESSAGES_CAP` (the historical cap) but
    is no longer a hard silent ceiling — callers may raise it.  A
    non-positive ``limit`` is treated as "no upper bound" (returns every
    projected message from ``offset`` onward).

    Any parser-level I/O or decode failure (``FileNotFoundError``,
    ``ValueError``, ``OSError``) yields ``[]`` so MCP callers always get
    a list back.
    """
    parser = _PARSERS.get(session.agent)
    if parser is None:
        return []
    try:
        raw = parser.read_messages(session.uuid)
    except (FileNotFoundError, ValueError, OSError):
        return []
    projected = _project_messages(raw)
    if offset > 0:
        projected = projected[offset:]
    if limit and limit > 0:
        projected = projected[:limit]
    return projected


@mcp.tool()
def list_sessions(
    agent: Optional[str] = None,
    limit: int = _LIST_LIMIT_DEFAULT,
    offset: int = 0,
) -> dict[str, Any]:
    """List discoverable sessions, optionally filtered by ``agent``.

    Results are sorted by date (newest first) and paginated with
    ``limit``/``offset`` so the payload stays small. The default ``limit``
    guards against dumping an unbounded number of sessions.

    Args:
        agent: One of ``claude``, ``codex``, ``opencode``, ``antigravity``,
            ``pi``. When omitted, every supported agent is queried.
        limit: Max sessions in this page. ``0`` means no cap (use with care:
            may return a very large payload). Defaults to 100.
        offset: Zero-based index of the first session to return. Use with
            ``limit`` to page through ``total``.

    Returns:
        ``{"sessions": [...], "total": int, "offset": int, "limit": int,
        "truncated": bool}``. ``total`` is the full count matching the
        ``agent`` filter; ``truncated`` is True when more sessions remain
        beyond this page.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        return {"error": "invalid_argument",
                "message": f"limit must be a non-negative integer, got {limit!r}"}
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return {"error": "invalid_argument",
                "message": f"offset must be a non-negative integer, got {offset!r}"}
    try:
        targets = _target_agents(agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}

    summaries: List[dict[str, Any]] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        for session in parser.list_sessions():
            summaries.append(_session_summary(session))

    # Global newest-first sort: parsers sort per-agent, but across agents we
    # merge into one timeline so offset/limit pages show the freshest sessions.
    summaries.sort(key=lambda s: s.get("date") or "", reverse=True)

    total = len(summaries)
    page = summaries[offset:] if limit == 0 else summaries[offset:offset + limit]
    return {
        "sessions": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": (offset + len(page)) < total,
    }


@mcp.tool()
def read_session(
    uuid: str,
    agent: str,
    offset: int = 0,
    limit: int = _MESSAGES_CAP,
) -> dict[str, Any]:
    """Read a single session by ``uuid`` and ``agent``.

    Args:
        uuid: Session identifier.
        agent: One of ``claude``, ``codex``, ``opencode``, ``antigravity``, ``pi``.
        offset: Zero-based index of the first message to return
            (applied to the projected ``{role, content}`` list).
        limit: Maximum number of messages to return.  Defaults to
            :data:`_MESSAGES_CAP` (100).  A non-positive value means
            "no upper bound".

    Returns:
        A dict with session metadata plus:

        * ``messages`` — the projected ``{role, content}`` list, sliced
          to ``[offset:offset+limit]``.
        * ``total`` — the full uncapped projected message count (the
          length the slice was taken from).
        * ``offset`` / ``limit`` — the pagination echo values actually
          used.

        On a missing session, returns an ``error`` dict instead of
        raising.
    """
    if not uuid or not str(uuid).strip():
        return {"error": "invalid_argument", "message": "uuid must be non-empty"}
    if offset < 0:
        return {"error": "invalid_argument", "message": "offset must be >= 0"}
    try:
        agent_name = _coerce_agent(agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}

    parser = _PARSERS[agent_name]
    try:
        session = parser.read_session(uuid)
    except ValueError as exc:
        # Parsers reject malformed uuids (path separators, whitespace, …)
        # with ValueError; surface them as structured invalid_argument
        # instead of letting them propagate as an uncaught server error.
        return {"error": "invalid_argument", "message": str(exc)}
    except FileNotFoundError:
        return {
            "error": "not_found",
            "uuid": uuid,
            "agent": agent_name.value,
        }

    projected = _extract_messages(session, offset=0, limit=0)
    total = len(projected)
    if offset > 0:
        projected = projected[offset:]
    if limit and limit > 0:
        projected = projected[:limit]

    summary = _session_summary(session)
    summary["messages"] = projected
    summary["total"] = total
    summary["offset"] = offset
    summary["limit"] = limit
    return summary


@mcp.tool()
def find_file_edits(
    path: str,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Find every file edit across sessions, cross-agent by default.

    Thin wrapper over :func:`ai_r.find_file_edits.find_file_edits`
    that translates the core ``ValueError`` contract into the
    ``{"error": "invalid_argument", "message": str(exc)}`` shape the
    MCP client expects.
    """
    try:
        return _find_file_edits_core(
            path=path,
            agent=agent,
            since=since,
            until=until,
            limit=limit,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def search_sessions(
    query: str,
    agent: Optional[str] = None,
    scope: str = "title",
    operator: str = "AND",
    limit: int = 50,
) -> List[dict[str, Any]]:
    """Case-insensitive search across sessions.

    Args:
        query: Search string. Supports:
            * Bare words: ``pwa manifest`` (AND default)
            * Quoted phrases: ``"exact phrase"``
            * Negative prefix: ``-claude`` (Google-style, always excluded)
        agent: Optional agent filter (claude/codex/opencode/antigravity/pi).
        scope: Where to look.
            * ``"title"`` — only ``session.title`` (default, backward-compat)
            * ``"body"``  — message text + ``tool_use[*].input`` +
              ``tool_result[*].content``
            * ``"all"``   — title OR body
        operator: How to combine positive terms.
            * ``"AND"`` — all positive terms must appear (default)
            * ``"OR"``  — at least one positive term must appear
            * ``"NOT"`` — no term (positive or negative) may appear
            Negative ``-term`` prefixes are always excluded regardless
            of operator.
        limit: Maximum number of results.  0 or negative = no limit.

    Returns:
        A list of session summaries. When ``scope`` is ``"body"`` or
        ``"all"`` and a match is found, the summary includes a
        ``"snippet"`` field with the first matching message excerpt
        (up to 200 chars).

    Errors are returned as ``{"error": ..., "message": ...}`` dicts in
    the list (matches the existing convention).
    """
    needle = (query or "").strip()
    if not needle:
        return []

    if scope not in ("title", "body", "all"):
        return [{
            "error": "invalid_argument",
            "message": f"unknown scope {scope!r}; expected title, body, or all",
        }]

    op_upper = (operator or "AND").upper()
    if op_upper not in ("AND", "OR", "NOT"):
        return [{
            "error": "invalid_argument",
            "message": f"unknown operator {operator!r}; expected AND, OR, or NOT",
        }]

    if not isinstance(limit, int) or limit < 0:
        return [{
            "error": "invalid_argument",
            "message": f"limit must be a non-negative integer, got {limit!r}",
        }]

    try:
        targets = _target_agents(agent)
    except ValueError as exc:
        return [{"error": "invalid_argument", "message": str(exc)}]

    positive, negative = _parse_query(needle)
    summaries: List[dict[str, Any]] = []

    for agent_name in targets:
        parser = _PARSERS[agent_name]
        for session in parser.list_sessions():
            matched = False
            snippet_text = ""

            if scope == "title":
                title_lc = session.title.lower()
                if op_upper == "AND":
                    matched = all(t in title_lc for t in positive) and all(
                        t not in title_lc for t in negative
                    )
                elif op_upper == "OR":
                    matched = bool(positive) and any(
                        t in title_lc for t in positive
                    ) and all(t not in title_lc for t in negative)
                else:
                    matched = all(
                        t not in title_lc for t in (positive + negative)
                    )
            elif scope == "body":
                try:
                    messages = parser.read_messages(session.uuid)
                except (FileNotFoundError, ValueError, OSError):
                    messages = []
                haystack = _build_haystack(messages)
                matched = _match(haystack, positive, negative, op_upper)
                if matched and positive:
                    snippet_text = _extract_snippet(haystack, positive)
            else:
                title_lc = session.title.lower()
                if op_upper == "AND":
                    in_title = all(t in title_lc for t in positive)
                elif op_upper == "OR":
                    in_title = bool(positive) and any(
                        t in title_lc for t in positive
                    )
                else:
                    in_title = all(
                        t not in title_lc for t in (positive + negative)
                    )
                try:
                    messages = parser.read_messages(session.uuid)
                except (FileNotFoundError, ValueError, OSError):
                    messages = []
                haystack = _build_haystack(messages)
                in_body = _match(haystack, positive, negative, op_upper)
                matched = in_title or in_body
                if matched:
                    if in_body and positive:
                        snippet_text = _extract_snippet(haystack, positive)
                    elif in_title and positive:
                        snippet_text = _extract_snippet(title_lc, positive)

            if not matched:
                continue
            summary = _session_summary(session)
            if snippet_text:
                summary["snippet"] = snippet_text
            summaries.append(summary)
            if limit and len(summaries) >= limit:
                return summaries
    return summaries


def _parse_query(query: str) -> tuple[list[str], list[str]]:
    """Split ``query`` into (positive_terms, negative_terms).

    Honors quoted phrases via ``shlex.split``. A leading ``-`` marks a
    term as negative. All terms are lowercased. Empty tokens are dropped.
    """
    tokens = shlex.split(query or "")
    positive: list[str] = []
    negative: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("-") and len(tok) > 1:
            negative.append(tok[1:].lower())
        else:
            positive.append(tok.lower())
    return positive, negative


def _build_haystack(messages: Sequence[Any]) -> str:
    """Concatenate message text + tool_use inputs + tool_result contents.

    Lowercased once on return. Includes content that lives in tool calls
    and tool results, not just plain text — this is what makes the
    full-text search actually useful for finding references buried in
    Bash/file/etc. invocations.
    """
    chunks: List[str] = []
    for m in messages:
        text = getattr(m, "text", "")
        if isinstance(text, str) and text:
            chunks.append(text)
        for tool in getattr(m, "tool_use", ()) or ():
            if isinstance(tool, dict):
                inp = tool.get("input", "")
                if inp:
                    chunks.append(str(inp))
        for res in getattr(m, "tool_result", ()) or ():
            if isinstance(res, dict):
                content = res.get("content", "")
                if content:
                    chunks.append(str(content))
    return "\n".join(chunks).lower()


def _match(
    haystack: str,
    positive: list[str],
    negative: list[str],
    operator: str,
) -> bool:
    """Evaluate the operator+negative-filter predicate against haystack."""
    op = (operator or "AND").upper()
    if op == "NOT":
        return all(term not in haystack for term in (positive + negative))
    if op == "AND":
        return all(term in haystack for term in positive) and all(
            term not in haystack for term in negative
        )
    if op == "OR":
        if not positive:
            return False
        return any(term in haystack for term in positive) and all(
            term not in haystack for term in negative
        )
    raise ValueError(f"unknown operator {operator!r}; expected AND, OR, or NOT")


def _extract_snippet(haystack: str, terms: list[str], max_len: int = 200) -> str:
    """Return a short excerpt around the first match of any term.

    Lowercased haystack, term matching is also lowercased. Adds leading/
    trailing ``...`` when the excerpt is clipped.
    """
    for term in terms:
        idx = haystack.find(term)
        if idx < 0:
            continue
        start = max(0, idx - 60)
        end = min(len(haystack), idx + max(0, len(term)) + 140)
        snippet = haystack[start:end].strip()
        if start > 0 and not snippet.startswith("..."):
            snippet = "..." + snippet
        if end < len(haystack) and not snippet.endswith("..."):
            snippet = snippet + "..."
        return snippet[:max_len]
    return ""


def main() -> int:
    """Entry point for the ``ai-r-mcp`` console script."""
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
