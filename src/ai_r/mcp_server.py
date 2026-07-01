"""MCP server entry point for ai-r.

Exposes these tools over the Model Context Protocol:

* :func:`list_sessions`  — enumerate sessions, optionally filtered by agent.
* :func:`read_session`   — load a single session by ``uuid`` and ``agent``.
* :func:`find_file_edits` — find file edits across sessions.
* :func:`find_tool_calls` — find arbitrary tool calls across sessions.
* :func:`session_diff`   — reconstruct what a session changed, without git.
* :func:`session_stats`  — group + rank sessions (by agent/dir/date/kind).
* :func:`search_sessions` — case-insensitive search across title and/or
  message bodies with AND/OR/NOT operators and Google-style ``-term``
  negative prefixes.
* :func:`query` — filter/search the unified session *event* stream
  (user/assistant turns + normalized tool calls) by facets, including
  the ``relative_to``+``direction``+``n`` neighbouring-turn walk.
* :func:`plan` — normalized plan atoms for a session (final vs drafts,
  grouped by task; per-agent plan signals normalized away).
* :func:`get_body` — on-demand body for an event/plan id (``shallow`` for
  final-plan-only, drafts elided).
* :func:`aggregate` — generic rollup over ``query`` rows (reproduces
  ``session_stats`` / ``file_frequency`` by ``group_by`` + ``metrics``).
* :func:`diff` — stitch edit rows into a per-file unified diff (reproduces
  ``session_diff``).
* :func:`detect_current` — runtime identity (session + agent) from env/fs.

Errors are returned as dicts (never raised) so the MCP client can
surface them in a structured way.

Transport: stdio.  No logging, no stdout writes outside the MCP
protocol — that would corrupt the JSON-RPC stream.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import time
from collections import OrderedDict
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
    previous_user_intent as _previous_user_intent,
    target_agents as _target_agents,
)
from ai_r.find_tool_calls import find_tool_calls as _find_tool_calls_core  # noqa: E402
from ai_r.session_diff import session_diff as _session_diff_core  # noqa: E402
from ai_r.session_stats import session_stats as _session_stats_core  # noqa: E402
from ai_r.parsers import Session  # noqa: E402
from ai_r.ranking import bm25_scores as _bm25_scores, tokenize as _tokenize  # noqa: E402
from ai_r.events import (  # noqa: E402
    query as _query_core,
    plan as _plan_core,
    get_body as _get_body_core,
    aggregate as _aggregate_core,
    diff as _diff_core,
    detect_current as _detect_current_core,
)

__all__ = ["mcp", "main"]


_MESSAGES_CAP = 100
_MESSAGES_HARD_CAP = 1000
_BODY_SEARCH_MESSAGE_CAP = 1000
_HAYSTACK_CHARS_CAP = 1_000_000
# Max chars of a single tool_use input value surfaced in read_session
# content.  tool_use.input is RAW, untrusted session content (a JSON
# string) — bound it so an oversized/adversarial blob cannot flood the
# MCP output.  Mirrors the size-bounding philosophy of
# ``ai_r.security.sanitize_session_text(max_chars=...)``.
_TOOL_INPUT_CHARS_CAP = 400
_LIST_LIMIT_DEFAULT = 100

# --- Body-search haystack cache -------------------------------------------
# Repeated ``scope="body"`` searches used to re-read + re-concatenate every
# session's messages on every call.  This LRU cache stores the built haystack
# (and its ``body_truncated`` flag) keyed by ``(agent, uuid, mtime)``.
#
# Invalidation strategy: MTIME-BASED (preferred).
# ``Session.path`` carries the source-of-truth path for every agent — the
# JSONL file for Claude/Codex/Pi/Antigravity and the SQLite DB path for
# OpenCode (see parsers/models.py:68).  We stat that path with
# ``os.path.getmtime``; if the mtime changed (or the stat fails / path is
# gone) the entry is treated as a miss and rebuilt.  For OpenCode the DB is
# shared across all sessions, so a DB mtime bump invalidates every OpenCode
# session's entry at once — which is the correct, safe behavior since any
# session may have grown.
_HAYSTACK_CACHE_MAX = 256
# Soft TTL is a defensive backstop only: if a source path cannot be statted
# (OSError), we still serve a cached entry but bound its staleness so a
# transiently-unreadable file does not pin stale content forever.
_HAYSTACK_CACHE_TTL_SEC = 300

_haystack_cache: "OrderedDict[tuple[str, str, float], tuple[str, bool]]" = OrderedDict()
_haystack_cache_lock = threading.Lock()


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
        "kind": session.kind,
        "parent_uuid": session.parent_uuid,
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


def _render_qa(qa: Any) -> str:
    """Render a message's ``qa`` entries as readable ``Q: ... A: ...`` lines.

    Each entry is a ``{"question", "options", "answer"}`` dict (the
    cross-agent interactive question→answer pair).  Rendering the
    *question alongside the answer* is the whole point: a bare answer
    label ("option B") is useless without the question it answered, so
    both are always emitted together.
    """
    lines: List[str] = []
    for entry in qa or ():
        if not isinstance(entry, dict):
            continue
        question = entry.get("question") or ""
        answer = entry.get("answer") or ""
        if not (question or answer):
            continue
        lines.append(f"[question→answer] Q: {question} A: {answer}")
    return "\n".join(lines)


# Per-tool "key input" keys, ordered by preference.  The first key present
# in the parsed input wins.  Bash-style commands and file-tool paths are the
# high-signal inputs for understanding *what* a tool call did.
_TOOL_INPUT_KEYS: tuple[str, ...] = (
    "command",
    "file_path",
    "path",
    "notebook_path",
    "pattern",
    "query",
    "url",
)


def _tool_use_summary(tool: dict) -> str:
    """Render one ``tool_use`` entry as ``[tool_use: NAME ...key input...]``.

    ``tool.input`` is the RAW tool input serialized to a string (JSON for
    structured inputs — see parsers/models.py:91-93).  It is untrusted
    session content, so:

    * we ``json.loads`` it and surface only a key input value (command /
      path / query …) when the JSON is an object;
    * on any decode failure we fall back to a truncated slice of the raw
      string;
    * every surfaced value is bounded by ``_TOOL_INPUT_CHARS_CAP`` so an
      oversized/adversarial blob cannot flood the output.
    """
    name = tool.get("name") if isinstance(tool, dict) else None
    if not name:
        return "[tool_use]"

    raw = tool.get("input") if isinstance(tool, dict) else None
    detail = ""
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            detail = raw[:_TOOL_INPUT_CHARS_CAP]
        else:
            if isinstance(parsed, dict):
                for key in _TOOL_INPUT_KEYS:
                    val = parsed.get(key)
                    if isinstance(val, str) and val.strip():
                        detail = f"{key}={val[:_TOOL_INPUT_CHARS_CAP]}"
                        break
                else:
                    # No known key — surface a compact slice of the dict.
                    detail = json.dumps(parsed)[:_TOOL_INPUT_CHARS_CAP]
            else:
                detail = str(parsed)[:_TOOL_INPUT_CHARS_CAP]

    return f"[tool_use: {name} {detail}]" if detail else f"[tool_use: {name}]"


def _project_message_content(m: Any) -> str:
    """Return compact MCP content for text or user/assistant tool-only messages.

    Interactive question→answer pairs (``m.qa``) are always rendered when
    present so the user's reply to an ``AskUserQuestion`` /
    ``request_user_input`` / ``question`` prompt surfaces in the message
    body even though it lives in a tool-result record.
    """
    qa_text = _render_qa(getattr(m, "qa", ()) or ())

    chunks: List[str] = []
    text = getattr(m, "text", "")
    if isinstance(text, str) and text:
        chunks.append(text)

    # tool_use summaries are surfaced even alongside text: an assistant
    # message often carries both narration ("I'll run them now.") *and* the
    # actual call, and the call input is the high-signal part for
    # understanding what happened.  tool_result placeholders stay dropped
    # when text is present (results are not load-bearing for read_session).
    for tool in getattr(m, "tool_use", ()) or ():
        if isinstance(tool, dict):
            chunks.append(_tool_use_summary(tool))
        else:
            chunks.append("[tool_use]")
    if not text:
        for _ in getattr(m, "tool_result", ()) or ():
            chunks.append("[tool_result]")
    if qa_text:
        chunks.append(qa_text)
    return "\n".join(chunks)


def _project_messages(
    messages: Sequence[Any],
    hard_cap: int = 0,
) -> List[dict[str, Any]]:
    """Project parser ``Message`` objects to ``{role, content}`` dicts.

    Only ``user``/``assistant`` roles are surfaced, preserving the
    historical MCP output shape; ``tool`` messages are dropped.  When
    ``hard_cap`` is positive, projection stops after that many surfaced
    messages.
    """
    out: List[dict[str, Any]] = []
    for idx, m in enumerate(messages):
        qa = getattr(m, "qa", ()) or ()
        # Surface user/assistant messages as before; additionally surface
        # ``tool``-role messages *only* when they carry an interactive
        # question→answer pair (Codex records the answer on the
        # function_call_output, a tool-role record).  A bare tool message
        # without qa stays dropped to preserve the historical MCP shape.
        if m.role not in ("user", "assistant") and not qa:
            continue
        content = _project_message_content(m)
        if not content:
            continue
        # Codex's answer-bearing record is role "tool"; relabel it "user"
        # so the answer reads as the user's reply (it is) and the output
        # shape stays {user|assistant}.
        role = m.role if m.role in ("user", "assistant") else "user"
        entry: dict[str, Any] = {"role": role, "content": content}
        # Timeline (Feature 2): surface the message timestamp in ISO form,
        # ``None`` when the parser carried no timestamp.  ``_iso`` requires a
        # datetime, so guard the ``None`` case explicitly.
        ts = getattr(m, "timestamp", None)
        entry["timestamp"] = _iso(ts) if ts is not None else None
        # Intent (Feature 1): for assistant messages that invoke a tool,
        # attach the previous user request that motivated the call.  Reuse
        # ``previous_user_intent`` over the *full* message list so the index
        # walk-back sees dropped tool/text records too.
        if role == "assistant" and (getattr(m, "tool_use", ()) or ()):
            intent = _previous_user_intent(messages, idx)
            if intent:
                entry["intent"] = intent
        if qa:
            entry["qa"] = [
                {
                    "question": e.get("question", ""),
                    "options": list(e.get("options", ()) or ()),
                    "answer": e.get("answer", ""),
                }
                for e in qa
                if isinstance(e, dict)
            ]
        out.append(entry)
        if hard_cap and hard_cap > 0 and len(out) >= hard_cap:
            break
    return out


def _extract_messages(
    session: Session,
    offset: int = 0,
    limit: int = _MESSAGES_CAP,
    hard_cap: int = _MESSAGES_HARD_CAP,
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
    projected = _project_messages(raw, hard_cap=hard_cap)
    if offset > 0:
        projected = projected[offset:]
    if limit and limit > 0:
        projected = projected[:limit]
    return projected


def _body_search_messages(session: Session) -> tuple[Sequence[Any], bool]:
    parser = _PARSERS.get(session.agent)
    if parser is None:
        return (), False
    try:
        messages = parser.read_messages(session.uuid)
    except (FileNotFoundError, ValueError, OSError):
        return (), False
    truncated = len(messages) > _BODY_SEARCH_MESSAGE_CAP
    return messages[:_BODY_SEARCH_MESSAGE_CAP], truncated


def _session_source_mtime(session: Session) -> Optional[float]:
    """Return the mtime of ``session.path``, or ``None`` if unstattable.

    ``None`` (OSError / missing / empty path) makes the caller fall back to
    the soft-TTL branch of the cache so a transiently-unreadable source does
    not crash the search.
    """
    path = getattr(session, "path", "") or ""
    if not path:
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _get_cached_haystack(
    session: Session,
    agent_name: str,
) -> tuple[str, bool]:
    """Return ``(haystack, body_truncated)`` for ``session``, cached by mtime.

    Cache key: ``(agent_name, session.uuid, mtime)`` where ``mtime`` is the
    mtime of ``session.path`` (JSONL file or, for OpenCode, the shared SQLite
    DB).  When the source mtime changes the entry is rebuilt, so a HIT
    produces byte-identical matching behavior to a MISS.

    Thread-safety: the lock is held only for the cheap OrderedDict check and
    the store; the expensive ``_body_search_messages`` + ``_build_haystack``
    run OUTSIDE the lock so one slow build does not serialize concurrent
    reads of other sessions.  A concurrent double-build of the same key is
    harmless (last writer wins; both produce identical output).

    If the source path cannot be statted (``mtime is None``), the key falls
    back to a soft TTL (:data:`_HAYSTACK_CACHE_TTL_SEC`) so we never pin
    stale content indefinitely for an unreadable path.
    """
    mtime = _session_source_mtime(session)
    now = time.monotonic()

    # Build the lookup key.  A known mtime is the cache validator; an unknown
    # mtime (None) folds a wall-clock TTL into the key via a coarse bucket so
    # the entry self-expires without a separate reaper.
    if mtime is not None:
        key = (agent_name, session.uuid, mtime)
    else:
        bucket = int(now // _HAYSTACK_CACHE_TTL_SEC)
        key = (agent_name, session.uuid, float(bucket))

    with _haystack_cache_lock:
        cached = _haystack_cache.get(key)
        if cached is not None:
            # LRU: move to most-recently-used so eviction hits oldest entries.
            _haystack_cache.move_to_end(key)
            return cached

    # Cache MISS — build outside the lock so concurrent reads of other
    # sessions are not blocked by this (potentially slow) read+concat.
    messages, body_truncated = _body_search_messages(session)
    haystack = _build_haystack(messages)
    value = (haystack, body_truncated)

    with _haystack_cache_lock:
        _haystack_cache[key] = value
        _haystack_cache.move_to_end(key)
        while len(_haystack_cache) > _HAYSTACK_CACHE_MAX:
            _haystack_cache.popitem(last=False)
    return value


@mcp.tool()
def list_sessions(
    agent: Optional[str] = None,
    limit: int = _LIST_LIMIT_DEFAULT,
    offset: int = 0,
    kind: Optional[str] = None,
) -> dict[str, Any]:
    """List discoverable sessions, optionally filtered by ``agent``.

    Results are sorted by date (newest first) and paginated with
    ``limit``/``offset`` so the payload stays small. The default ``limit``
    guards against dumping an unbounded number of sessions.

    Each summary carries ``kind`` (``"agent"`` for a top-level session,
    ``"subagent"`` for a spawned subagent/sidechain) and ``parent_uuid``
    (the parent session's uuid for subagents, else ``None``).  NOTE:
    subagent-tree detection is currently implemented for **Claude only**;
    every other agent always reports ``kind="agent"``.  This is a scope
    boundary, not a bug.

    Args:
        agent: One of ``claude``, ``codex``, ``opencode``, ``antigravity``,
            ``pi``. When omitted, every supported agent is queried.
        limit: Max sessions in this page. ``0`` means no cap (use with care:
            may return a very large payload). Defaults to 100.
        offset: Zero-based index of the first session to return. Use with
            ``limit`` to page through ``total``.
        kind: Optional filter. ``"agent"`` returns only top-level sessions,
            ``"subagent"`` returns only subagent sessions. When omitted
            (default), both kinds are returned.

    Returns:
        ``{"sessions": [...], "total": int, "offset": int, "limit": int,
        "truncated": bool}``. ``total`` is the full count matching the
        ``agent`` (and ``kind``) filter; ``truncated`` is True when more
        sessions remain beyond this page.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        return {"error": "invalid_argument",
                "message": f"limit must be a non-negative integer, got {limit!r}"}
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return {"error": "invalid_argument",
                "message": f"offset must be a non-negative integer, got {offset!r}"}
    if kind is not None and kind not in ("agent", "subagent"):
        return {"error": "invalid_argument",
                "message": f"kind must be 'agent', 'subagent' or null, got {kind!r}"}
    try:
        targets = _target_agents(agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}

    summaries: List[dict[str, Any]] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        for session in parser.list_sessions():
            if kind is not None and session.kind != kind:
                continue
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
        * ``messages_truncated`` — True when the MCP hard cap stopped
          extraction before every projected message could be returned.

        On a missing session, returns an ``error`` dict instead of
        raising.
    """
    if not uuid or not str(uuid).strip():
        return {"error": "invalid_argument", "message": "uuid must be non-empty"}
    if offset < 0:
        return {"error": "invalid_argument", "message": "offset must be >= 0"}
    if not isinstance(limit, int) or isinstance(limit, bool):
        return {"error": "invalid_argument", "message": "limit must be an integer"}
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

    projected = _extract_messages(
        session,
        offset=0,
        limit=0,
        hard_cap=_MESSAGES_HARD_CAP + 1,
    )
    messages_truncated = len(projected) > _MESSAGES_HARD_CAP
    if messages_truncated:
        projected = projected[:_MESSAGES_HARD_CAP]
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
    summary["messages_truncated"] = messages_truncated
    return summary


@mcp.tool()
def find_file_edits(
    path: str,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
    include_input: bool = False,
) -> dict[str, Any]:
    """Find every file edit across sessions, cross-agent by default.

    Reference-by-default: to keep an audit listing small, each record does
    **not** inline the full edit body.  Instead it carries a light-weight
    reference — ``input_sha256`` (hash of the body) and ``input_chars`` (its
    length) — so you can see a body exists and how big it is.  Fetch the body
    on demand with ``get_body`` / ``read_session`` (keyed by ``session_uuid``
    + ``message_index``).  Pass ``include_input=True`` to inline the full
    body under ``input`` instead.

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
            include_input=include_input,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def find_tool_calls(
    tool_name: Optional[str] = None,
    tool_name_pattern: Optional[str] = None,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Find every tool call across sessions, cross-agent by default.

    Exactly one of ``tool_name`` (exact, case-insensitive) or
    ``tool_name_pattern`` (substring, case-insensitive) must be set.

    Thin wrapper over :func:`ai_r.find_tool_calls.find_tool_calls`
    that translates the core ``ValueError`` contract into the
    ``{"error": "invalid_argument", "message": str(exc)}`` shape the
    MCP client expects.
    """
    try:
        return _find_tool_calls_core(
            tool_name=tool_name,
            tool_name_pattern=tool_name_pattern,
            agent=agent,
            since=since,
            until=until,
            limit=limit,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def session_stats(
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    group_by: str = "agent",
    top: int = 8,
    edit_path: str = "/",
) -> dict[str, Any]:
    """Summarise sessions, grouped and ranked — the *bird's-eye* audit view.

    Where ``find_file_edits`` / ``find_tool_calls`` return flat record
    streams, this rolls the *sessions themselves* up by one dimension so you
    can see how the work is distributed in a single call.

    ``group_by`` is one of:

    * ``"agent"`` (default) — claude vs codex vs opencode vs ...
    * ``"dir"``   — by working directory / project (``cwd`` for codex/pi,
      project slug for claude; ``"(unknown)"`` for agents without one).
    * ``"date"``  — by calendar day (``YYYY-MM-DD``).
    * ``"kind"``  — top-level *agent* sessions vs spawned *subagent* sessions.

    Each group carries its session count plus enrichment from the shared
    ``find_file_edits`` core: ``edits`` (file edits attributed to the group's
    sessions), ``intents`` (distinct requests behind those edits), the
    distinct ``agents`` in the group, and total ``messages``.

    RISK-4 note: subagent detection is currently **Claude-only**.  When no
    subagent sessions are in scope, a ``group_by="kind"`` result shows a
    single ``agent`` bucket — so the result always carries
    ``kind_split_available`` (``False`` here) plus a ``note`` making clear
    that this is NOT a verified "no subagents", just an absent split.

    Thin wrapper over :func:`ai_r.session_stats.session_stats` that
    translates the core ``ValueError`` contract into the
    ``{"error": "invalid_argument", "message": str(exc)}`` shape the MCP
    client expects.
    """
    try:
        return _session_stats_core(
            agent=agent,
            since=since,
            until=until,
            group_by=group_by,
            top=top,
            edit_path=edit_path,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def session_diff(
    session_uuid: str,
    agent: str,
    path: Optional[str] = None,
) -> dict[str, Any]:
    """Reconstruct *what the agent changed* in one session — without git.

    Stitches the session's own edit records (``Edit``/``MultiEdit``
    ``old_string``→``new_string``, ``Write`` ``content``, codex shell-exec
    redirections) into a per-file, chronological diff. Returns
    ``{"files": [...], "count": N, "caveats": [...]}``; each file carries
    its ordered ``edits`` (timestamp + intent + hunks) and a stitched,
    readable ``diff``.

    ``caveats`` always carries two honest blind spots: (1) this is a diff
    of the agent's *actions*, not the git outcome — manual edits / partial
    commits / merges are invisible; (2) RISK-3 — inherits the
    ``find_file_edits`` shell-redirect gap (``tee`` / ``sed -i`` / ``cp`` /
    ``mv`` / heredoc writes are not detected and are silently skipped).

    Thin wrapper over :func:`ai_r.session_diff.session_diff` that
    translates the core ``ValueError`` contract into the
    ``{"error": "invalid_argument", "message": str(exc)}`` shape the MCP
    client expects.
    """
    try:
        return _session_diff_core(
            session_uuid=session_uuid,
            agent=agent,
            path=path,
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
    sort: str = "relevance",
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
            Applied *after* sorting, so it keeps the top-ranked matches.
        sort: Result ordering.
            * ``"relevance"`` — BM25 relevance over the matched text
              (default).  Pure-stdlib scoring; ties keep newest-first.
            * ``"date"`` — newest-first by session date (the historical
              pre-ranking order).

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

    sort_lower = (sort or "relevance").lower()
    if sort_lower not in ("relevance", "date"):
        return [{
            "error": "invalid_argument",
            "message": f"unknown sort {sort!r}; expected relevance or date",
        }]

    try:
        targets = _target_agents(agent)
    except ValueError as exc:
        return [{"error": "invalid_argument", "message": str(exc)}]

    positive, negative = _parse_query(needle)
    summaries: List[dict[str, Any]] = []
    # Text each match was found in, kept parallel to ``summaries``. Only
    # populated/used when ``sort == "relevance"`` so that date-sort never
    # pays for tokenisation.
    score_texts: List[str] = []

    for agent_name in targets:
        parser = _PARSERS[agent_name]
        for session in parser.list_sessions():
            matched = False
            snippet_text = ""
            score_text = ""

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
                score_text = title_lc
            elif scope == "body":
                haystack, body_truncated = _get_cached_haystack(
                    session, agent_name
                )
                matched = _match(haystack, positive, negative, op_upper)
                if matched and positive:
                    snippet_text = _extract_snippet(haystack, positive)
                score_text = haystack
            else:
                title_lc = session.title.lower()
                haystack, body_truncated = _get_cached_haystack(
                    session, agent_name
                )
                in_title = any(t in title_lc for t in positive)
                in_body = any(t in haystack for t in positive)
                combined = f"{title_lc}\n{haystack}"
                matched = _match(combined, positive, negative, op_upper)
                if matched:
                    if in_body and positive:
                        snippet_text = _extract_snippet(haystack, positive)
                    elif in_title and positive:
                        snippet_text = _extract_snippet(title_lc, positive)
                score_text = combined

            if not matched:
                continue
            summary = _session_summary(session)
            if snippet_text:
                summary["snippet"] = snippet_text
            if scope in ("body", "all") and body_truncated:
                summary["body_truncated"] = True
            summaries.append(summary)
            score_texts.append(score_text)

    if sort_lower == "relevance" and summaries:
        # Flatten phrase terms into BM25 query tokens; lazily tokenise only
        # the matched docs (never the whole haystack cache).
        query_tokens: List[str] = []
        for term in positive:
            query_tokens.extend(_tokenize(term))
        docs_tokens = [_tokenize(text) for text in score_texts]
        scores = _bm25_scores(query_tokens, docs_tokens)
        # ``sorted`` is stable: equal scores preserve list_sessions order
        # (newest-first), giving a deterministic recency tie-break.
        order = sorted(
            range(len(summaries)), key=lambda i: scores[i], reverse=True
        )
        summaries = [summaries[i] for i in order]
    # ``sort == "date"`` keeps the existing newest-first insertion order.

    if limit:
        summaries = summaries[:limit]
    return summaries


@mcp.tool()
def query(
    type: Optional[str] = None,
    agent: Optional[str] = None,
    session: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    file: Optional[str] = None,
    tool: Optional[str] = None,
    text: Optional[str] = None,
    sort: str = "date",
    relative_to: Optional[str] = None,
    direction: str = "prev",
    n: str = "1",
    step_type: str = "user_turn",
    limit: int = 0,
    with_intent: bool = False,
    kind: Optional[str] = None,
    parent: Optional[str] = None,
    group: Optional[str] = None,
) -> dict[str, Any]:
    """Filter/search the unified session **event** stream — the workhorse verb.

    Every parser's messages + tool calls are normalized into one flat,
    agent-neutral event stream (``user_turn`` / ``assistant_turn`` /
    ``tool_call(<sub>)`` / ``plan_event``); this tool filters that stream
    by facets — *all* behaviour is parameters, never hard-wired variants.

    Facets:

    * ``type`` — ``user_turn`` | ``assistant_turn`` | ``tool_call`` |
      ``tool_call(edit|write|read|bash|other)`` | ``plan_event``.  Bare
      ``tool_call`` matches every subtype.
    * ``agent`` — one of claude/codex/opencode/antigravity/pi (all if omitted).
    * ``session`` — restrict to a single session uuid.
    * ``since`` / ``until`` — ISO-8601 bounds (inclusive) on the event ts.
    * ``file`` — substring matched against an event's referenced file path.
    * ``tool`` — substring (pattern) matched against the referenced tool name.
    * ``text`` — substring matched against event text.  With
      ``sort="relevance"`` survivors are BM25-ranked using the **same
      scorer** as ``search_sessions``; ``sort="date"`` (default) orders
      by timestamp ascending.
    * ``relative_to`` (event id) + ``direction`` (``prev``|``next``) +
      ``n`` (``"1"`` | ``"all"``) — the neighbouring-turn walk.
      Generalises the ``previous_user_intent`` used by ``find_file_edits``
      to both directions and any count.  ``step_type`` chooses which
      event type to collect (default ``user_turn``).  When
      ``relative_to`` is set, other filter facets are ignored.

    ``with_intent=True`` attaches a top-level ``intent`` (the request behind
    the event, via the same ``previous_user_intent`` walk-back the legacy
    tools use) to every returned event.  Default ``False`` keeps the base
    event shape unchanged.

    ``kind`` / ``parent`` / ``group`` are accepted for forward-compat but
    **not yet implemented** (Phase 2/3: plan + subagent facets).  Passing a
    non-``None`` value is a fail-loud error (returns the standard
    ``invalid_argument`` dict) rather than a silent no-op.

    Returns ``{"events": [...], "count": N}`` or the standard
    ``{"error": ..., "message": ...}`` dict on invalid arguments.
    """
    try:
        events = _query_core(
            type=type,
            agent=agent,
            session=session,
            since=since,
            until=until,
            file=file,
            tool=tool,
            text=text,
            sort=sort,
            relative_to=relative_to,
            direction=direction,
            n=n,
            step_type=step_type,
            limit=limit,
            with_intent=with_intent,
            kind=kind,
            parent=parent,
            group=group,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    return {"events": events, "count": len(events)}


@mcp.tool()
def plan(
    session: Optional[str] = None,
    kind: Optional[str] = None,
    group: str = "task",
    agent: Optional[str] = None,
) -> dict[str, Any]:
    """Normalized plan atoms for a session — final vs drafts, grouped by task.

    Wraps ``query(type="plan_event", …)`` and normalizes every agent's plan
    signal (Claude ``ExitPlanMode`` / ``Write plans/*.md``, Codex
    ``update_plan``, Antigravity ``implementation_plan.md``) into a single
    :class:`~ai_r.events.Plan` shape — the per-agent signal is an internal
    detail, never surfaced.

    Plans are grouped by *task* keyed on each plan's ``task_key`` — the
    plan-file slug when the agent has one (Claude ``plans/<slug>.md``,
    Antigravity ``implementation_plan.md`` path), falling back to the
    normalized title only when no plan file exists (Codex ``update_plan``).
    Within a task the latest plan is ``final`` and earlier revisions are
    ``draft``; plans of *earlier* completed tasks are ``completed_major``.

    Args:
        session: Restrict to one session uuid (recommended).
        kind: Optional filter — ``draft`` | ``final`` | ``completed_major``.
        group: Grouping strategy; only ``"task"`` is supported.
        agent: Optional agent filter (claude/codex/opencode/antigravity/pi).

    Returns:
        ``{"plans": [...], "count": N}`` (each plan carries
        ``id/session_id/agent/title/task_id/kind/path/steps/status/refs/
        sha256``; bodies are on-demand via :func:`get_body`) or the standard
        ``{"error": ..., "message": ...}`` dict on invalid arguments.
    """
    try:
        plans = _plan_core(session=session, kind=kind, group=group, agent=agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    return {"plans": plans, "count": len(plans)}


@mcp.tool()
def get_body(id: str, shallow: bool = False) -> dict[str, Any]:
    """Return the on-demand body for an event / plan ``id``.

    For a ``plan_event`` id: the full plan text and/or Codex ``steps``
    (bodies are deliberately kept off the event stream so callers pay for
    them only when needed).  For a ``user_turn`` / ``assistant_turn`` id:
    the turn text.

    ``shallow=True`` (plans only) returns just the *final* plan of the id's
    task, dropping the bodies of superseded ``draft`` revisions — the S6
    case where a subagent receives one plan without the draft noise
    (``dropped_drafts`` lists the ids that were elided).

    Returns the body dict, or ``{"error": ..., "message": ...}`` on a bad id.
    """
    if not id or not str(id).strip():
        return {"error": "invalid_argument", "message": "id must be non-empty"}
    return _get_body_core(id, shallow=shallow)


@mcp.tool()
def aggregate(
    rows: List[dict[str, Any]],
    group_by: str,
    metrics: Optional[List[str]] = None,
    rank_by: str = "default",
    kind_split: bool = False,
) -> dict[str, Any]:
    """Roll a list of row dicts up by ``group_by`` — the generic stats verb.

    Reproduces ``session_stats`` (``group_by`` ∈ ``agent``/``dir``/``date``/
    ``kind`` over a session inventory) and ``file_frequency``
    (``group_by="file"`` over a ``find_file_edits`` record stream) as a pure
    fold over already-materialized rows — no re-parsing.  ``session_stats`` is
    now a thin preset over this verb (``rank_by="stats"`` + ``kind_split``).

    Args:
        rows: The row dicts to fold (``query`` output, ``find_file_edits``
            records, or a session inventory).
        group_by: The bucket key — a row field name (``agent`` / ``dir`` /
            ``date`` / ``kind`` / ``file`` / …).  Missing/empty values bucket
            under ``"(unknown)"``.
        metrics: Which numbers each bucket carries.  One or more of
            ``count`` / ``sessions`` / ``edits`` / ``intents`` / ``agents`` /
            ``messages`` / ``files``.  Defaults to ``["count"]``.
        rank_by: Group ordering — ``"default"`` (edits→sessions→count→label,
            the ``file_frequency`` order) or ``"stats"`` (sessions→edits→label,
            the ``session_stats`` order).
        kind_split: When ``True``, add the ``session_stats`` RISK-4 fields
            (``kind_split_available`` + a degenerate-split ``note``).

    Returns:
        ``{"group_by", "groups": [...], "totals": {...}}`` (plus
        ``kind_split_available``/``note`` when ``kind_split``) or the standard
        ``{"error": ..., "message": ...}`` dict on an unknown metric/rank_by.
    """
    try:
        return _aggregate_core(
            rows,
            group_by=group_by,
            metrics=tuple(metrics) if metrics else ("count",),
            rank_by=rank_by,
            kind_split=kind_split,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def diff(
    rows: List[dict[str, Any]],
    per_file: bool = True,
    format: str = "unified",
) -> dict[str, Any]:
    """Stitch edit rows into a per-file chronological diff — the diff verb.

    Reproduces the synthesis of ``session_diff``: given the edit events of a
    session (``query(type="tool_call(edit)", session=…)`` — plus ``write`` /
    shell-redirect events), group them per file in chronological order and
    render a stitched, readable diff.  Bodies are fetched on demand (via each
    event's stored ``message_index``), never inlined on the row.

    Args:
        rows: Edit-event dicts (``query`` output).  Each must carry an ``id``
            and a ``refs`` list with a ``file`` entry; unresolvable rows skip.
        per_file: Group by file (the only mode today).
        format: ``"unified"`` (the only rendering today).

    Returns:
        ``{"files": [{"file", "edits", "diff", "hunks"}], "count", "caveats"}``
        (same shape + caveats as ``session_diff``) or the standard
        ``{"error": ..., "message": ...}`` dict on an unsupported ``format``.
    """
    try:
        return _diff_core(rows, per_file=per_file, format=format)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def detect_current(agent: Optional[str] = None) -> dict[str, Any]:
    """Return the current runtime identity (session + agent) from env/fs.

    NOT a session-query — this reads the runtime environment (env vars +
    per-session flag files), reusing the exact cascade behind the
    ``ai-r detect-agent`` / ``ai-r detect-session`` CLI subcommands.

    Args:
        agent: Optional hint (accepted for symmetry with the CLI's
            deprecated ``--agent`` flag); the cascade scans all agents.

    Returns:
        ``{"session_id", "agent", "candidates": [...], "verified", "self"}``
        where ``session_id`` / ``agent`` describe the highest-priority
        candidate and ``candidates`` is the full cascade for disambiguation.
        Returns ``{"error": ..., "message": ...}`` on an unknown ``agent``
        hint.
    """
    try:
        return _detect_current_core(agent=agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


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


def _build_haystack(
    messages: Sequence[Any],
    max_chars: int = _HAYSTACK_CHARS_CAP,
) -> str:
    """Concatenate message text + tool_use inputs + tool_result contents.

    Lowercased once on return. Includes content that lives in tool calls
    and tool results, not just plain text — this is what makes the
    full-text search actually useful for finding references buried in
    Bash/file/etc. invocations.
    """
    chunks: List[str] = []
    total_chars = 0
    for m in messages:
        text = getattr(m, "text", "")
        if isinstance(text, str) and text:
            chunks.append(text)
            total_chars += len(text)
        for tool in getattr(m, "tool_use", ()) or ():
            if isinstance(tool, dict):
                inp = tool.get("input", "")
                if inp:
                    chunk = str(inp)
                    chunks.append(chunk)
                    total_chars += len(chunk)
        for res in getattr(m, "tool_result", ()) or ():
            if isinstance(res, dict):
                content = res.get("content", "")
                if content:
                    chunk = str(content)
                    chunks.append(chunk)
                    total_chars += len(chunk)
        if max_chars and max_chars > 0 and total_chars >= max_chars:
            break
    haystack = "\n".join(chunks).lower()
    if max_chars and max_chars > 0:
        return haystack[:max_chars]
    return haystack


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
