"""MCP server entry point for ai-r.

Exposes these tools over the Model Context Protocol:

* :func:`list_sessions`  — enumerate sessions, optionally filtered by agent.
* :func:`read_session`   — load a single session by ``uuid`` (``agent``
  optional: omitted → the id is resolved across every parser; a rare
  cross-agent id collision returns a candidate list, not an error).
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
    # Re-exported for downstream consumers/tests that historically import
    # it from here (read_session no longer uses it directly).
    coerce_agent as _coerce_agent,  # noqa: F401
    find_file_edits as _find_file_edits_core,
    iso as _iso,
    previous_user_intent as _previous_user_intent,
    target_agents as _target_agents,
)
from ai_r.diagnostics import empty_result_diagnostics as _empty_diagnostics  # noqa: E402
from ai_r.find_tool_calls import find_tool_calls as _find_tool_calls_core  # noqa: E402
from ai_r.session_diff import session_diff as _session_diff_core  # noqa: E402
from ai_r.session_stats import session_stats as _session_stats_core  # noqa: E402
from ai_r.parsers import Session  # noqa: E402
from ai_r.parsers._common import project_dir_matches  # noqa: E402
from ai_r.parsers._noise import NOISE_MODES, noise_allows  # noqa: E402
from ai_r.ranking import bm25_scores as _bm25_scores, tokenize as _tokenize  # noqa: E402
from ai_r.resume import resume_command  # noqa: E402
from ai_r.redact import (  # noqa: E402
    merge_redaction_counts as _merge_redactions,
    redact_value as _redact_value,
)
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


def _redact_fields(
    obj: dict[str, Any],
    fields: Sequence[str],
    redactions: dict[str, int],
) -> None:
    """Mask secrets in the named ``obj`` fields in place, folding counts.

    Emission-time redaction helper (F2.1) shared by the MCP wrappers whose
    output shaping lives in this module (``read_session`` /
    ``search_sessions`` / ``list_sessions``).  Absent fields are skipped;
    counts accumulate into ``redactions``.
    """
    for field in fields:
        if field not in obj:
            continue
        new_val, counts = _redact_value(obj[field])
        if counts:
            obj[field] = new_val
            _merge_redactions(redactions, counts)


mcp = FastMCP(
    name="ai-r",
    instructions=(
        "ai-r: read Claude, Codex, OpenCode, Antigravity and Pi session "
        f"files. Server version: {__version__}."
    ),
)


def _session_summary(session: Session) -> dict[str, Any]:
    """Project a :class:`Session` to a JSON-safe summary dict.

    ``project_dir`` / ``launch_surface`` are top-level fields (next to
    ``kind`` / ``parent_uuid``) and stay ``None`` when the source format
    carries no signal — absence is honest, never fabricated (F1.4).
    ``resume_command`` (F2.2) is the ready-to-run shell one-liner that
    reopens the session in its agent's CLI, ``None`` when no real
    command exists (Antigravity, subagent sessions, reference-only
    Desktop sessions) — text only, never executed (SSOT
    :mod:`ai_r.resume`).
    """
    result = {
        "uuid": session.uuid,
        "agent": session.agent.value,
        "title": session.title,
        "date": _iso(session.date),
        "message_count": session.message_count,
        "kind": session.kind,
        "parent_uuid": session.parent_uuid,
        "project_dir": session.project_dir,
        "launch_surface": session.launch_surface,
        "resume_command": resume_command(session),
    }
    if session.extra:
        result["extra"] = session.extra
    return result


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


_TOOL_RESULT_SNIPPET_CHARS = 120


def _tool_result_summary(tr: dict) -> str:
    """Render one ``tool_result`` entry, surfacing success vs. error.

    * a failed call → ``[tool_result ERROR: <first ~120 chars>]`` so the
      core audit question "did this actually work?" is answerable from the
      projection alone;
    * a successful call → ``[tool_result ok]`` (or ``[tool_result ok:
      <snippet>]`` when a short content snippet is available).

    ``content`` is untrusted session data; it is collapsed to a single
    line and bounded by :data:`_TOOL_RESULT_SNIPPET_CHARS`.
    """
    is_error = bool(tr.get("is_error"))
    raw = tr.get("content", "")
    snippet = ""
    if isinstance(raw, str) and raw.strip():
        snippet = " ".join(raw.split())[:_TOOL_RESULT_SNIPPET_CHARS]
    if is_error:
        return f"[tool_result ERROR: {snippet}]" if snippet else "[tool_result ERROR]"
    return f"[tool_result ok: {snippet}]" if snippet else "[tool_result ok]"


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
    # understanding what happened.
    for tool in getattr(m, "tool_use", ()) or ():
        if isinstance(tool, dict):
            chunks.append(_tool_use_summary(tool))
        else:
            chunks.append("[tool_use]")

    # tool_result outcomes surface success/error so read_session can answer
    # "did this edit/command work?".  When the message carries narration
    # text, only *errors* are surfaced (a plain success is not load-bearing
    # next to the text); when the message is result-only, every result is
    # rendered so the record is never empty.
    for tr in getattr(m, "tool_result", ()) or ():
        if not isinstance(tr, dict):
            if not text:
                chunks.append("[tool_result]")
            continue
        if text and not bool(tr.get("is_error")):
            continue
        chunks.append(_tool_result_summary(tr))
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
    noise: str = "include",
    project_dir: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """List discoverable sessions, optionally filtered by ``agent``.

    Results are sorted by date (newest first) and paginated with
    ``limit``/``offset`` so the payload stays small. The default ``limit``
    guards against dumping an unbounded number of sessions.

    Each summary carries ``kind`` (``"agent"`` for a top-level session,
    ``"subagent"`` for a spawned subagent/sidechain) and ``parent_uuid``
    (the parent session's uuid for subagents, else ``None``).  Subagent
    detection covers Claude, OpenCode, Codex and Pi; Antigravity's format
    has no parent signal, so it always reports ``kind="agent"``.

    Each summary also carries the F1.4 origin fields, ``None`` when the
    source format has no signal (never fabricated):

    * ``project_dir`` — the project directory the session ran in
      (Claude: transcript ``cwd`` / Desktop metadata / verified slug
      decode; Codex: ``session_meta.cwd``; OpenCode:
      ``session.directory``; Pi: header ``cwd``; Antigravity: no signal).
    * ``launch_surface`` — where the session was driven from (Claude:
      ``"claude-cli"`` | ``"claude-desktop"``; Codex: the raw
      ``originator``, e.g. ``"codex_vscode"``; Antigravity:
      ``"antigravity-ide"`` | ``"antigravity-cli"``; OpenCode/Pi: no
      signal).

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
        noise: Noise filter — a session is *noise* when it is a spawned
            subagent (``kind == "subagent"`` or ``parent_uuid`` set).
            ``"include"`` (default) returns everything, ``"exclude"`` drops
            noise sessions, ``"only"`` returns only noise sessions.
            ``kind`` and ``noise`` compose (AND).
        project_dir: Keep only sessions whose ``project_dir`` equals this
            path or is a **descendant** of it (path-boundary aware:
            ``/a/b`` matches ``/a/b`` and ``/a/b/sub``, never ``/a/bc``);
            trailing slashes ignored.  Sessions without a ``project_dir``
            signal never match.  Composes with the other filters (AND).
        redact: When ``True`` (default) secrets in emitted ``title`` /
            ``extra`` values are masked as ``[REDACTED_<TYPE>]`` and the
            response carries a ``redactions`` type→count dict when any
            replacement happened; ``False`` returns raw titles.

    Returns:
        ``{"sessions": [...], "total": int, "offset": int, "limit": int,
        "truncated": bool}``. ``total`` is the full count matching the
        ``agent`` (and ``kind``/``noise``) filter; ``truncated`` is True
        when more sessions remain beyond this page.  When ``total == 0``
        the dict additionally carries ``diagnostics`` (scanned agents +
        session counts, source-dir presence, cause hints) so an empty
        inventory is explainable.
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
    if noise not in NOISE_MODES:
        return {"error": "invalid_argument",
                "message": f"noise must be one of {'/'.join(NOISE_MODES)}, "
                           f"got {noise!r}"}
    if project_dir is not None and (
        not isinstance(project_dir, str) or not project_dir.strip()
    ):
        return {"error": "invalid_argument",
                "message": "project_dir must be a non-empty path string "
                           f"or null, got {project_dir!r}"}
    try:
        targets = _target_agents(agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}

    summaries: List[dict[str, Any]] = []
    # Per-agent list_sessions() results, reused by the empty-result
    # diagnostics below so an empty inventory never pays for a second scan.
    scanned_sessions: dict[str, Any] = {}
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        agent_sessions = parser.list_sessions()
        scanned_sessions[agent_name.value.lower()] = agent_sessions
        for session in agent_sessions:
            if kind is not None and session.kind != kind:
                continue
            if not noise_allows(session, noise):
                continue
            if project_dir is not None and not project_dir_matches(
                session.project_dir, project_dir
            ):
                continue
            summaries.append(_session_summary(session))

    # Global newest-first sort: parsers sort per-agent, but across agents we
    # merge into one timeline so offset/limit pages show the freshest sessions.
    summaries.sort(key=lambda s: s.get("date") or "", reverse=True)

    total = len(summaries)
    page = summaries[offset:] if limit == 0 else summaries[offset:offset + limit]
    redactions: dict[str, int] = {}
    if redact:
        for summary in page:
            _redact_fields(summary, ("title", "extra"), redactions)
    result: dict[str, Any] = {
        "sessions": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": (offset + len(page)) < total,
    }
    if redactions:
        result["redactions"] = redactions
    if total == 0:
        result["diagnostics"] = _empty_diagnostics(
            agent=agent,
            filters={
                "kind": kind,
                # "include" is the no-op default — never a cause of emptiness.
                "noise": None if noise == "include" else noise,
                "project_dir": project_dir,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return result


@mcp.tool()
def read_session(
    uuid: str,
    agent: Optional[str] = None,
    offset: int = 0,
    limit: int = _MESSAGES_CAP,
    redact: bool = True,
) -> dict[str, Any]:
    """Read a single session by ``uuid``; ``agent`` is an optional hint.

    Args:
        uuid: Session identifier.
        agent: One of ``claude``, ``codex``, ``opencode``, ``antigravity``,
            ``pi``.  **Optional**: when omitted, the ``uuid`` is looked up
            across every parser (session ids are unique across agents in
            practice).  If — rarely — the same id exists under several
            agents, a ``candidates`` list is returned (not an error) so
            the caller can re-ask with an explicit ``agent``.
        offset: Zero-based index of the first message to return
            (applied to the projected ``{role, content}`` list).
        limit: Maximum number of messages to return.  Defaults to
            :data:`_MESSAGES_CAP` (100).  A non-positive value means
            "no upper bound".
        redact: When ``True`` (default) secrets in the emitted ``title``,
            message ``content``/``intent``/``qa`` are masked as
            ``[REDACTED_<TYPE>]`` and the response carries a
            ``redactions`` type→count dict when any replacement happened
            (see ``ai_r.redact``); ``False`` returns the raw content.

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

        On an id collision (agent omitted, several agents own the id):
        ``{"ambiguous": True, "uuid": ..., "candidates": [...],
        "count": N, "message": ...}`` where each candidate is a session
        summary carrying its ``agent``.

        On a missing session, returns an ``error`` dict instead of
        raising (``agents_scanned`` lists the parsers probed when the
        agent was omitted).
    """
    if not uuid or not str(uuid).strip():
        return {"error": "invalid_argument", "message": "uuid must be non-empty"}
    if offset < 0:
        return {"error": "invalid_argument", "message": "offset must be >= 0"}
    if not isinstance(limit, int) or isinstance(limit, bool):
        return {"error": "invalid_argument", "message": "limit must be an integer"}
    try:
        targets = _target_agents(agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}

    matches: List[Session] = []
    value_errors: List[str] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        try:
            matches.append(parser.read_session(uuid))
        except ValueError as exc:
            # Parsers reject malformed uuids (path separators, whitespace,
            # …) with ValueError; collect them — they only surface when NO
            # parser resolved the id.
            value_errors.append(str(exc))
        except FileNotFoundError:
            continue

    if not matches:
        if value_errors:
            # At least one parser rejected the id as malformed and none
            # resolved it — a structured invalid_argument, matching the
            # historical single-agent behaviour.
            return {"error": "invalid_argument", "message": value_errors[0]}
        return {
            "error": "not_found",
            "uuid": uuid,
            "agent": targets[0].value if agent else None,
            "agents_scanned": [t.value.lower() for t in targets],
        }

    if len(matches) > 1:
        # Same id under several agents: NOT an error — return the
        # candidates so the caller can disambiguate via ``agent``.
        return {
            "ambiguous": True,
            "uuid": uuid,
            "candidates": [_session_summary(s) for s in matches],
            "count": len(matches),
            "message": (
                "session id matches multiple agents; pass agent to "
                "disambiguate"
            ),
        }

    session = matches[0]

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
    # Emission-time redaction (F2.1): after pagination so only the emitted
    # page pays; the raw transcript on disk is never touched.
    if redact:
        redactions: dict[str, int] = {}
        _redact_fields(summary, ("title", "extra"), redactions)
        for entry in projected:
            _redact_fields(entry, ("content", "intent", "qa"), redactions)
        if redactions:
            summary["redactions"] = redactions
    return summary


@mcp.tool()
def find_file_edits(
    path: str,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
    include_input: bool = False,
    redact: bool = True,
) -> dict[str, Any]:
    """Find every file edit across sessions, cross-agent by default.

    ``redact=True`` (default) masks secrets in emitted record fields as
    ``[REDACTED_<TYPE>]`` and adds a ``redactions`` type→count dict when
    any replacement happened; ``redact=False`` returns raw content.

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
            redact=redact,
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
    input_contains: Optional[str] = None,
    output_contains: Optional[str] = None,
    output_excludes: Optional[str] = None,
    is_error: Optional[bool] = None,
    output_mode: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Find every tool call across sessions, cross-agent by default.

    ``redact=True`` (default) masks secrets in emitted record fields as
    ``[REDACTED_<TYPE>]`` and adds a ``redactions`` type→count dict when
    any replacement happened; ``redact=False`` returns raw content.
    Filters always match the RAW, pre-redaction text.

    Exactly one of ``tool_name`` (exact, case-insensitive) or
    ``tool_name_pattern`` (substring, case-insensitive) must be set.

    Optional filters combine with AND: ``input_contains`` /
    ``output_contains`` (case-insensitive substring on the full,
    pre-cap input/output), ``output_excludes`` (drop records whose
    output contains it) and ``is_error`` (tri-state: ``None`` all,
    ``True`` failures only, ``False`` successes only).  ``output_mode``
    selects output truncation — ``"head"``/``"tail"``/``"smart"``;
    ``None`` is adaptive (``"smart"`` on errors, ``"head"`` otherwise).
    Each record also carries ``is_error_reliable`` (``True`` only for
    Claude/OpenCode).

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
            input_contains=input_contains,
            output_contains=output_contains,
            output_excludes=output_excludes,
            is_error=is_error,
            output_mode=output_mode,
            redact=redact,
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
    redact: bool = True,
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

    ``redact=True`` (default) masks secrets in the emitted diff/hunks/
    intents as ``[REDACTED_<TYPE>]`` and adds a ``redactions`` type→count
    dict when any replacement happened; ``redact=False`` returns raw.

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
            redact=redact,
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
    noise: str = "include",
    redact: bool = True,
) -> dict[str, Any]:
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
        noise: Noise filter — a session is *noise* when it is a spawned
            subagent (``kind == "subagent"`` or ``parent_uuid`` set).
            * ``"include"`` — no filtering (default).
            * ``"exclude"`` — search only top-level agent sessions.
            * ``"only"``    — search only subagent sessions.
            Applied *before* matching, so excluded sessions never pay
            the body-scan cost.
        redact: When ``True`` (default) secrets in the emitted ``title`` /
            ``snippet`` / ``extra`` fields are masked as
            ``[REDACTED_<TYPE>]`` and the response carries a
            ``redactions`` type→count dict when any replacement
            happened; ``False`` returns raw content.  Matching always
            runs on the RAW stored text, so searching for a literal
            secret still finds its session — only the displayed
            snippet is masked.

    Returns:
        A dict ``{"results": [...], "count": N}`` where ``results`` is the
        list of session summaries and ``count`` is their total. When
        ``scope`` is ``"body"`` or ``"all"`` and a match is found, each
        summary includes a ``"snippet"`` field with the first matching
        message excerpt (up to 200 chars) and may carry ``body_truncated``.
        When a scan matches nothing (``count == 0``), the dict additionally
        carries ``diagnostics`` (scanned agents + session counts, corpus
        date bounds, cause hints) so an empty result is explainable.

    Errors are returned as a top-level ``{"error": ..., "message": ...}``
    dict (matches the existing convention).
    """
    needle = (query or "").strip()
    if not needle:
        return {"results": [], "count": 0}

    if scope not in ("title", "body", "all"):
        return {
            "error": "invalid_argument",
            "message": f"unknown scope {scope!r}; expected title, body, or all",
        }

    op_upper = (operator or "AND").upper()
    if op_upper not in ("AND", "OR", "NOT"):
        return {
            "error": "invalid_argument",
            "message": f"unknown operator {operator!r}; expected AND, OR, or NOT",
        }

    if not isinstance(limit, int) or limit < 0:
        return {
            "error": "invalid_argument",
            "message": f"limit must be a non-negative integer, got {limit!r}",
        }

    sort_lower = (sort or "relevance").lower()
    if sort_lower not in ("relevance", "date"):
        return {
            "error": "invalid_argument",
            "message": f"unknown sort {sort!r}; expected relevance or date",
        }

    if noise not in NOISE_MODES:
        return {
            "error": "invalid_argument",
            "message": f"noise must be one of {'/'.join(NOISE_MODES)}, "
                       f"got {noise!r}",
        }

    try:
        targets = _target_agents(agent)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}

    positive, negative = _parse_query(needle)
    summaries: List[dict[str, Any]] = []
    # Text each match was found in, kept parallel to ``summaries``. Only
    # populated/used when ``sort == "relevance"`` so that date-sort never
    # pays for tokenisation.
    score_texts: List[str] = []

    # Per-agent list_sessions() results, reused by the empty-result
    # diagnostics below so an empty result never pays for a second scan.
    scanned_sessions: dict[str, Any] = {}
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        agent_sessions = parser.list_sessions()
        scanned_sessions[agent_name.value.lower()] = agent_sessions
        for session in agent_sessions:
            if not noise_allows(session, noise):
                continue
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
    # Emission-time redaction (F2.1): after ranking + limit so only emitted
    # summaries pay; matching above ran on the raw haystack.
    redactions: dict[str, int] = {}
    if redact:
        for summary in summaries:
            _redact_fields(summary, ("title", "snippet", "extra"), redactions)
    result: dict[str, Any] = {"results": summaries, "count": len(summaries)}
    if redactions:
        result["redactions"] = redactions
    if not summaries:
        result["diagnostics"] = _empty_diagnostics(
            agent=agent,
            filters={
                "query": needle,
                "scope": scope,
                "operator": op_upper,
                # "include" is the no-op default — never a cause of emptiness.
                "noise": None if noise == "include" else noise,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return result


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
    noise: str = "include",
    project_dir: Optional[str] = None,
    kind: Optional[str] = None,
    parent: Optional[str] = None,
    group: Optional[str] = None,
    redact: bool = True,
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

    ``noise`` filters at the *session* level before events are read — a
    session is noise when it is a spawned subagent (``kind == "subagent"``
    or ``parent_uuid`` set): ``"include"`` (default, no filtering),
    ``"exclude"`` (top-level sessions only), ``"only"`` (subagent sessions
    only).  Ignored on the ``relative_to`` walk, like every other filter
    facet.

    ``project_dir`` also filters at the *session* level: keep only events
    of sessions whose ``project_dir`` equals this path or is a
    **descendant** of it (path-boundary aware, trailing slashes ignored) —
    "events of this project".  Sessions without a ``project_dir`` signal
    never match.  Ignored on the ``relative_to`` walk, like every other
    filter facet.

    ``kind`` / ``parent`` / ``group`` are accepted for forward-compat but
    **not yet implemented** (Phase 2/3: plan + subagent facets).  Passing a
    non-``None`` value is a fail-loud error (returns the standard
    ``invalid_argument`` dict) rather than a silent no-op.

    ``redact=True`` (default) masks secrets in the emitted ``text`` /
    ``intent`` fields as ``[REDACTED_<TYPE>]`` and adds a top-level
    ``redactions`` type→count dict when any replacement happened;
    ``redact=False`` returns raw content.  Redaction is emission-time only:
    the ``text`` facet (and every other filter) matches the RAW stored text.

    Returns ``{"events": [...], "count": N}`` or the standard
    ``{"error": ..., "message": ...}`` dict on invalid arguments.  When
    ``count == 0`` the dict additionally carries ``diagnostics`` (scanned
    agents + session counts, corpus date bounds, cause hints) so an empty
    result is explainable.
    """
    # Filled by the core scan with the per-agent list_sessions() results,
    # reused by the empty-result diagnostics so an empty result never pays
    # for a second corpus walk.
    scanned_sessions: dict[str, Any] = {}
    redactions: dict[str, int] = {}
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
            noise=noise,
            project_dir=project_dir,
            kind=kind,
            parent=parent,
            group=group,
            scanned_sessions_out=scanned_sessions,
            redact=redact,
            redactions_out=redactions,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    result: dict[str, Any] = {"events": events, "count": len(events)}
    if redactions:
        result["redactions"] = redactions
    if not events:
        result["diagnostics"] = _empty_diagnostics(
            agent=agent,
            since=since,
            until=until,
            filters={
                "type": type,
                "session": session,
                "file": file,
                "tool": tool,
                "text": text,
                "relative_to": relative_to,
                # "include" is the no-op default — never a cause of emptiness.
                "noise": None if noise == "include" else noise,
                "project_dir": project_dir,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return result


@mcp.tool()
def plan(
    session: Optional[str] = None,
    kind: Optional[str] = None,
    group: str = "task",
    agent: Optional[str] = None,
    redact: bool = True,
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
        redact: When ``True`` (default) secrets in the emitted plan fields
            (``title``/``steps``/``refs``…) are masked as
            ``[REDACTED_<TYPE>]`` and the response carries a ``redactions``
            type→count dict when any replacement happened; ``False``
            returns raw content.

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
    result: dict[str, Any] = {"plans": plans, "count": len(plans)}
    # Emission-time redaction (F2.1): the core runs its internal query with
    # ``redact=False`` and defers the single masking pass to this wrapper.
    if redact:
        redacted_plans, counts = _redact_value(plans)
        if counts:
            result["plans"] = redacted_plans
            result["redactions"] = counts
    return result


@mcp.tool()
def get_body(
    id: str,
    shallow: bool = False,
    max_chars: int = 500_000,
    redact: bool = True,
) -> dict[str, Any]:
    """Return the on-demand body for an event / plan ``id``.

    For a ``plan_event`` id: the full plan text and/or Codex ``steps``
    (bodies are deliberately kept off the event stream so callers pay for
    them only when needed).  For a ``user_turn`` / ``assistant_turn`` id:
    the turn text.

    ``shallow=True`` (plans only) returns just the *final* plan of the id's
    task, dropping the bodies of superseded ``draft`` revisions — the S6
    case where a subagent receives one plan without the draft noise
    (``dropped_drafts`` lists the ids that were elided).

    ``max_chars`` bounds the returned ``body``/``text`` (default 500_000,
    generous enough that ordinary bodies are never cut; pass ``0`` to
    disable).  When it trips, the field is sliced with a ``…[truncated]``
    marker and ``body_truncated: true`` is set.

    ``redact=True`` (default) masks secrets in the emitted
    ``text``/``body``/``title``/``steps`` as ``[REDACTED_<TYPE>]`` and adds
    a ``redactions`` type→count dict when any replacement happened;
    ``redact=False`` returns the raw content.

    Returns the body dict, or ``{"error": ..., "message": ...}`` on a bad id.
    """
    if not id or not str(id).strip():
        return {"error": "invalid_argument", "message": "id must be non-empty"}
    return _get_body_core(id, shallow=shallow, max_chars=max_chars, redact=redact)


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
    redact: bool = True,
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
        redact: When ``True`` (default) secrets in the stitched output are
            masked as ``[REDACTED_<TYPE>]`` and the result carries a
            ``redactions`` type→count dict when any replacement happened;
            ``False`` returns raw content.

    Returns:
        ``{"files": [{"file", "edits", "diff", "hunks"}], "count", "caveats"}``
        (same shape + caveats as ``session_diff``) or the standard
        ``{"error": ..., "message": ...}`` dict on an unsupported ``format``.
    """
    try:
        return _diff_core(rows, per_file=per_file, format=format, redact=redact)
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
