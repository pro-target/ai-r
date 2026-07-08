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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Union

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from ai_r import __version__  # noqa: E402
from ai_r.find_file_edits import (  # noqa: E402
    PARSERS as _PARSERS,
    cap_field as _cap_field,
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
from ai_r.incidents import incidents as _incidents_core  # noqa: E402
from ai_r.network import network as _network_core  # noqa: E402
from ai_r.session_diff import session_diff as _session_diff_core  # noqa: E402
from ai_r.session_stats import (  # noqa: E402
    TOKEN_SCAN_LIMIT as _SESSION_STATS_TOKEN_SCAN_LIMIT,
    children_of as _children_of,
    session_stats as _session_stats_core,
)
from ai_r.tokens import (  # noqa: E402
    component_tokens,
    rollup_component_tokens,
    session_tokens,
)
from ai_r.parsers import ParserModule, Session  # noqa: E402
from ai_r.parsers._common import project_dir_matches  # noqa: E402
from ai_r.parsers._noise import NOISE_MODES, noise_allows  # noqa: E402
from ai_r.ranking import bm25_scores as _bm25_scores, tokenize as _tokenize  # noqa: E402
from ai_r.semantic import semantic_order as _semantic_order  # noqa: E402
from ai_r.outcome import session_outcome as _session_outcome  # noqa: E402
from ai_r.resume import resume_command  # noqa: E402
from ai_r.activity import session_activity, stall_seconds  # noqa: E402
from ai_r.serve import resolve_transport, run_http  # noqa: E402
from ai_r.redact import (  # noqa: E402
    merge_redaction_counts as _merge_redactions,
    redact_value as _redact_value,
)
from ai_r.events import (  # noqa: E402
    query as _query_core,
    plan as _plan_core,
    plan_feedback as _plan_feedback_core,
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
# Cache size cap. The shared long-lived http server
# (``AI_R_MCP_TRANSPORT=http``) holds ONE warm cache for every agent, so it
# must be able to hold a whole corpus: a cap below the session count makes a
# full-corpus ``scope="body"`` search thrash — it LRU-evicts the very entries
# it is about to reuse and re-parses every file, erasing the warm-repeat win.
# Measured on a ~1492-session corpus: at the old 256 cap the "warm" repeat was
# as slow as cold (1x); with the cap above the corpus it is ~17x faster.
# Default holds a large corpus; tune with ``AI_R_HAYSTACK_CACHE_MAX``. Each
# entry is already bounded by ``_HAYSTACK_CHARS_CAP`` so total stays bounded.
def _resolve_haystack_cache_max(env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("AI_R_HAYSTACK_CACHE_MAX")
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return _HAYSTACK_CACHE_MAX_DEFAULT
        if value > 0:
            return value
    return _HAYSTACK_CACHE_MAX_DEFAULT


_HAYSTACK_CACHE_MAX_DEFAULT = 2048
_HAYSTACK_CACHE_MAX = _resolve_haystack_cache_max()
# Second, size-based cap: the entry count alone lets a long-lived shared
# server grow to (count × ``_HAYSTACK_CHARS_CAP``) ≈ multiple GiB of resident
# haystack strings. Cap the *summed* haystack chars too so RSS stays bounded
# regardless of how large individual sessions are. Default: 512M chars
# (~0.5–1 GiB of str payload) — comfortably above a real warm corpus while
# refusing unbounded growth. Tunable via ``AI_R_HAYSTACK_CACHE_CHARS_MAX``.
_HAYSTACK_CACHE_CHARS_MAX_DEFAULT = 512_000_000


def _resolve_haystack_cache_chars_max(
    env: Optional[Mapping[str, str]] = None,
) -> int:
    env = os.environ if env is None else env
    raw = env.get("AI_R_HAYSTACK_CACHE_CHARS_MAX")
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return _HAYSTACK_CACHE_CHARS_MAX_DEFAULT
        if value > 0:
            return value
    return _HAYSTACK_CACHE_CHARS_MAX_DEFAULT


_HAYSTACK_CACHE_CHARS_MAX = _resolve_haystack_cache_chars_max()
# Soft TTL is a defensive backstop only: if a source path cannot be statted
# (OSError), we still serve a cached entry but bound its staleness so a
# transiently-unreadable file does not pin stale content forever.
_HAYSTACK_CACHE_TTL_SEC = 300

_haystack_cache: "OrderedDict[tuple[str, str, float], tuple[str, bool]]" = OrderedDict()
# Running sum of ``len(haystack)`` across cached entries, kept in lockstep with
# ``_haystack_cache`` so the size-cap eviction never has to re-scan every value.
_haystack_cache_chars = 0
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


# Reference-by-default boundary (scenario QRY-1): the ``query`` MCP wrapper
# cuts every emitted event ``text`` to this many characters — the full body
# stays on-demand via ``get_body``.
_EVENT_TEXT_PREVIEW_CHARS = 160


def _preview_event_texts(
    events: List[dict[str, Any]],
    max_chars: int = _EVENT_TEXT_PREVIEW_CHARS,
) -> None:
    """Cut each event's ``text`` to a preview, in place (QRY-1 contract).

    Output-boundary projection ONLY: the core (:mod:`ai_r.events.query`) and
    every in-process consumer of full event text (``plan`` / ``diff`` /
    ``session_stats`` / ``session_diff`` / ``find_*``) keep seeing the full
    text — this runs on the MCP wrapper's already-materialized row dicts.
    It also runs AFTER emission-time redaction (the core redacts before
    returning), so a secret at the head of a long body is masked in the
    preview too.  A real cut is marked with a trailing ``…`` and
    ``text_truncated: true``; shorter texts are left untouched (no flag).
    ``id`` / ``refs`` / ``sha256`` are never modified, so ``get_body(id)``
    still resolves the full body.
    """
    for ev in events:
        text = ev.get("text")
        if isinstance(text, str) and len(text) > max_chars:
            ev["text"] = text[:max_chars] + "…"
            ev["text_truncated"] = True


def _unknown_tool_args(
    tool_params: Mapping[str, Any], arguments: Mapping[str, Any]
) -> list[str]:
    """Argument keys the caller passed that the tool's schema does not declare.

    The FastMCP transport validates arguments against a pydantic model built
    from each tool's signature, and pydantic *silently drops* unknown keys
    before the function runs — so a caller that mistypes a facet or invents a
    parameter (e.g. ``plan(limit=…)`` or ``list_sessions(since=…)``, both seen
    in real usage) gets a misleadingly successful, unfiltered result.  This is
    the same silent-drop failure the retired ``kind`` facet was tombstoned to
    avoid; the check below closes it for the whole surface.  Returns the sorted
    unknown keys (``[]`` when every key is declared).
    """
    allowed = set((tool_params or {}).get("properties", {}))
    return sorted(k for k in arguments if k not in allowed)


class _StrictArgsFastMCP(FastMCP):
    """FastMCP that fails loud on unknown tool arguments instead of dropping them.

    Overrides :meth:`call_tool` to reject any argument key absent from the
    tool's declared schema with the project's standard
    ``{"error": "invalid_argument", ...}`` envelope, *before* the tool runs —
    so a silently-ignored typo can never masquerade as a real, unfiltered
    answer.  An unknown *tool name* falls through to the base class unchanged.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        tool = self._tool_manager.get_tool(name)
        # ``Mapping`` (not bare ``dict``): any mapping-shaped arguments the
        # transport hands over get the unknown-key check — a non-dict mapping
        # must not slide past the guard into pydantic's silent key drop.  The
        # remaining fall-throughs are fail-LOUD in the base class already: an
        # unknown tool name raises, non-mapping arguments fail validation.
        if tool is not None and isinstance(arguments, Mapping):
            unknown = _unknown_tool_args(tool.parameters, arguments)
            if unknown:
                allowed = sorted((tool.parameters or {}).get("properties", {}))
                return {
                    "error": "invalid_argument",
                    "message": (
                        f"unknown argument(s): {', '.join(unknown)}. "
                        f"{name} accepts: {', '.join(allowed) or '(none)'}."
                    ),
                }
        return await super().call_tool(name, arguments)


mcp = _StrictArgsFastMCP(
    name="ai-r",
    instructions=(
        "ai-r: read Claude, Codex, OpenCode, Antigravity and Pi session "
        f"files. Server version: {__version__}."
    ),
)


def _session_summary(
    session: Session,
    now: Optional[datetime] = None,
    stale_sec: Optional[float] = None,
) -> dict[str, Any]:
    """Project a :class:`Session` to a JSON-safe summary dict.

    ``project_dir`` / ``launch_surface`` are top-level fields (next to
    ``kind`` / ``parent_uuid``) and stay ``None`` when the source format
    carries no signal — absence is honest, never fabricated (F1.4).
    ``models`` lists the session's unique model ids in order of first
    appearance (``Session.models``); ``[]`` without a signal — same
    honesty rule.
    ``resume_command`` (F2.2) is the ready-to-run shell one-liner that
    reopens the session in its agent's CLI, ``None`` when no real
    command exists (Antigravity, subagent sessions, reference-only
    Desktop sessions) — text only, never executed (SSOT
    :mod:`ai_r.resume`).

    When ``now`` is supplied (``list_sessions`` samples the wall clock once
    per call and passes it in), the A3 recency fields are attached:

    * ``last_activity`` — the last-activity timestamp as an explicit ISO
      string (same instant as ``date``, kept alongside it for a clearly
      named field; ``date`` is retained for backward compatibility);
    * ``age_sec`` — whole seconds since ``last_activity``;
    * ``activity`` — ``"fresh"`` / ``"stale"`` (recency of the last written
      record only — NOT a claim about process liveness; see
      :mod:`ai_r.activity`).

    ``stale_sec`` defaults to :func:`ai_r.activity.stall_seconds` when a
    ``now`` is given but no explicit threshold.  Without ``now`` the summary
    is byte-identical to the historical shape (no recency fields).
    """
    date_iso = _iso(session.date)
    result: dict[str, Any] = {
        "uuid": session.uuid,
        "agent": session.agent.value,
        "title": session.title,
        "date": date_iso,
        "message_count": session.message_count,
        "kind": session.kind,
        "parent_uuid": session.parent_uuid,
        "project_dir": session.project_dir,
        "launch_surface": session.launch_surface,
        # Unique models observed in the session, in order of first
        # appearance (see Session.models); [] when the format carries no
        # signal — honest absence, never fabricated (model dimension).
        "models": list(session.models),
        "resume_command": resume_command(session),
    }
    if now is not None:
        threshold = stall_seconds() if stale_sec is None else stale_sec
        recency = session_activity(session.date, now, threshold)
        result["last_activity"] = date_iso
        result["age_sec"] = recency["age_sec"]
        result["activity"] = recency["activity"]
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


def _message_token_blocks(
    messages: Sequence[Any],
    hard_cap: int = 0,
) -> List[Optional[dict[str, Any]]]:
    """Per-surfaced-message exact token blocks, aligned with ``_project_messages``.

    Walks the messages applying the *exact same* surface-drop logic as
    :func:`_project_messages` (drop non-user/assistant records without qa,
    then drop empty-content records) and returns one entry per SURFACED
    message — so the result lines up positionally with
    ``_project_messages(messages, hard_cap)``.  Each entry is the message's
    exact ``tokens`` block (F3.3) to attach, or ``None`` when the record
    carries no per-message usage (Codex / Antigravity / user turns).

    Claude dedup: a streamed API call writes several JSONL records sharing
    one ``(message.id, requestId)`` — every one carries an identical
    ``tokens`` block tagged with an internal ``_call`` key.  The block is
    emitted only on the FIRST surviving (surfaced) record per ``_call``;
    later records of the same call get ``None``.  The internal ``_call``
    key is stripped from the emitted block so it never leaks to the client.
    OpenCode / Pi carry no ``_call`` key → each assistant block is attached
    directly.

    CRITICAL: this walk is over the FULL message list (the caller runs it
    BEFORE the ``[offset:offset+limit]`` page slice), so which record is
    "first" for a ``_call`` is decided on absolute positions and never
    shifts with the page window.
    """
    out: List[Optional[dict[str, Any]]] = []
    seen_calls: set[str] = set()
    for m in messages:
        qa = getattr(m, "qa", ()) or ()
        if m.role not in ("user", "assistant") and not qa:
            continue
        content = _project_message_content(m)
        if not content:
            continue
        block = getattr(m, "tokens", None)
        emit: Optional[dict[str, Any]] = None
        if isinstance(block, dict):
            call = block.get("_call")
            if isinstance(call, str):
                # Claude streamed-call dedup on absolute position.
                if call not in seen_calls:
                    seen_calls.add(call)
                    emit = {k: v for k, v in block.items() if k != "_call"}
            else:
                # OpenCode / Pi: no dedup key, attach directly.
                emit = dict(block)
        if emit is not None:
            # A per-message block is always the format's own recorded usage
            # → tag it ``source="exact"`` so the tier reads the same as the
            # session block (and never mixes with the estimate component
            # breakdown).
            emit["source"] = "exact"
        out.append(emit)
        if hard_cap and hard_cap > 0 and len(out) >= hard_cap:
            break
    return out


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
        # Model dimension: the producing model, only where the parser carried
        # one (``Message.model``) — the key is absent without a signal, never
        # a fabricated null (mirrors the event-layer inheritance).
        model = getattr(m, "model", None)
        if model is not None:
            entry["model"] = model
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
        _haystack_store(key, value)
    return value


def _haystack_store(
    key: "tuple[str, str, float]", value: "tuple[str, bool]"
) -> None:
    """Insert ``value`` under ``key``, purging stale siblings and over-cap tail.

    Caller must hold :data:`_haystack_cache_lock`. Three invariants:

    * **No dead-key pileup on mtime change.** A rebuilt session (new mtime →
      new key) would otherwise leave its previous ``(agent, uuid, old_mtime)``
      entry wedged in the LRU until count-eviction reached it — the regression
      fixed in 00e4248 re-appearing. We eagerly drop every prior entry for the
      same ``(agent, uuid)`` so exactly one live version per session survives.
    * **Count cap** (``_HAYSTACK_CACHE_MAX``): bound the number of entries.
    * **Char cap** (``_HAYSTACK_CACHE_CHARS_MAX``): bound summed haystack size
      so a long-lived shared server's RSS can't balloon to GiB from many large
      sessions. Both caps evict oldest-first (LRU) until satisfied.
    """
    global _haystack_cache_chars
    # Purge any stale-mtime sibling(s) for this (agent, uuid) before inserting.
    agent_name, uuid = key[0], key[1]
    stale = [
        k for k in _haystack_cache
        if k[0] == agent_name and k[1] == uuid and k != key
    ]
    for k in stale:
        old = _haystack_cache.pop(k)
        _haystack_cache_chars -= len(old[0])

    prev = _haystack_cache.get(key)
    if prev is not None:
        _haystack_cache_chars -= len(prev[0])
    _haystack_cache[key] = value
    _haystack_cache.move_to_end(key)
    _haystack_cache_chars += len(value[0])

    # Never evict the entry we just stored, even if it alone exceeds the char
    # cap (a single >cap session must still be servable); stop at 1 remaining.
    while _haystack_cache and (
        len(_haystack_cache) > _HAYSTACK_CACHE_MAX
        or _haystack_cache_chars > _HAYSTACK_CACHE_CHARS_MAX
    ):
        if len(_haystack_cache) == 1:
            break
        _, evicted = _haystack_cache.popitem(last=False)
        _haystack_cache_chars -= len(evicted[0])


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

    Each summary also carries ``models`` — the unique model ids observed
    in the session, in order of first appearance (Claude: assistant
    ``message.model``; Codex: ``turn_context.model``; OpenCode:
    ``message.data.modelID``; Pi: assistant ``message.model``;
    Antigravity records no model signal).  ``[]`` when the format carries
    no signal — honest absence, never fabricated.

    Each summary also carries the A3 recency signal, measured against a
    single wall-clock ``now`` sampled once for the whole call:

    * ``last_activity`` — the last-activity timestamp as an explicit ISO
      string (same instant as ``date``; ``date`` is kept for backward
      compatibility);
    * ``age_sec`` — whole seconds since ``last_activity`` (clamped at ``0``
      when a future timestamp implies writer/reader clock skew);
    * ``activity`` — ``"fresh"`` if ``age_sec`` is at or under the
      ``AI_R_STALL_SEC`` threshold (default ``600`` s = 10 min),
      ``"stale"`` if past it.

    Honest contract (F1.1): ``activity`` describes only the **recency of the
    last written record**.  It is **not** a claim about process liveness — a
    session file cannot show whether its producer is still running.
    "Running but silent" vs. "crashed" is a consumer-side inference
    (correlate ``activity == "stale"`` with an OS pid-alive check); ai-r does
    not fabricate it.

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
    # Sample the wall clock and the fresh/stale threshold ONCE per call so
    # every session's A3 recency (``age_sec`` / ``activity``) is measured
    # against a single, consistent "now" (the pure classifier lives in
    # :mod:`ai_r.activity` and never reads the clock itself).
    now = datetime.now(timezone.utc)
    stale_sec = stall_seconds()
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
            summaries.append(_session_summary(session, now=now, stale_sec=stale_sec))

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
    with_tokens: bool = False,
    include_subagents: bool = False,
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
        with_tokens: When ``True`` (F3.3) attach token usage read at
            request time (nothing runs in the background):

            * ``summary["tokens"]`` — the session's flat
              :func:`ai_r.tokens.session_tokens` block (exact where the agent
              records usage, a labeled estimate otherwise, honest
              ``source=None`` without any signal);
            * ``summary["component_tokens"]`` — the
              :func:`ai_r.tokens.component_tokens` breakdown: the transcript's
              estimated token volume split across ai-r's event taxonomy
              (``user_turn`` / ``assistant_turn`` / ``thinking`` / ``plan``
              and a ``tool_call`` per-``tool_kind`` sub-dict), always
              ``source="estimate"`` (never merged with the exact tier),
              ``None`` on an empty transcript;
            * per-message ``tokens`` on projected entries that carry exact
              usage — Claude (deduplicated per streamed API call), OpenCode
              and Pi.  Codex / Antigravity / user turns carry **no**
              ``tokens`` key at all (absent, not ``null``).  The dedup/attach
              is decided on absolute message positions BEFORE pagination, so
              page boundaries never shift which record is "first" for a call.

            Default ``False``: output is byte-identical to the historical
            shape (no ``tokens`` / ``component_tokens`` key on the summary or
            any message).  The token blocks carry only integers and
            ai-r-authored labels (never raw session text), so they stay
            outside the F2.1 redaction pass by construction.
        include_subagents: When ``True`` attach
            ``summary["subagent_rollup"]`` — the parent session's
            ``component_tokens`` block plus one per spawned subagent child
            (resolved via :func:`ai_r.session_stats.children_of` on
            ``parent_uuid``) and a ``total`` folding parent + children through
            the ``aggregate`` ``component_tokens`` metric.  A childless parent
            (or an agent like Antigravity that never records ``parent_uuid``)
            yields an empty ``children`` list and a ``total`` equal to the
            parent's own block — honest, not an error.  Independent of
            ``with_tokens``.  Default ``False``.

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
        * ``outcome`` — session outcome classification (F2.3):
          ``{status: success|failure|mixed|unknown, signals, user_verdict,
          markers, tool_results, tool_errors, error_rate,
          error_rate_reliable}``.  ``status`` combines the tool-call
          error rate (real flag only for Claude/OpenCode —
          ``None`` elsewhere, never guessed) with a calibrated bilingual
          success/failure dictionary over the tail user turns;
          ``"unknown"`` when neither signal exists (SSOT
          :mod:`ai_r.outcome`).

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
    if not isinstance(with_tokens, bool):
        return {"error": "invalid_argument",
                "message": f"with_tokens must be a bool, got {with_tokens!r}"}
    if not isinstance(include_subagents, bool):
        return {"error": "invalid_argument",
                "message": (
                    f"include_subagents must be a bool, "
                    f"got {include_subagents!r}"
                )}
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

    # Read the raw structured messages ONCE: the projection consumes them
    # below and the outcome classifier (F2.3) scans the same list — no
    # second parse of the transcript.
    session_parser: Optional[ParserModule] = _PARSERS.get(session.agent)
    raw_messages: List[Any] = []
    if session_parser is not None:
        try:
            raw_messages = session_parser.read_messages(session.uuid)
        except (FileNotFoundError, ValueError, OSError):
            raw_messages = []
    projected = _project_messages(
        raw_messages, hard_cap=_MESSAGES_HARD_CAP + 1
    )
    messages_truncated = len(projected) > _MESSAGES_HARD_CAP
    if messages_truncated:
        projected = projected[:_MESSAGES_HARD_CAP]
    total = len(projected)
    # Per-message exact token blocks (F3.3, only when requested).  Built
    # over the FULL projected list with the SAME hard cap so it aligns 1:1
    # with ``projected`` — the Claude ``_call`` dedup is thus decided on
    # absolute positions, BEFORE the page slice below.
    token_blocks: List[Optional[dict[str, Any]]] = []
    if with_tokens:
        token_blocks = _message_token_blocks(
            raw_messages, hard_cap=_MESSAGES_HARD_CAP + 1
        )
        if messages_truncated:
            token_blocks = token_blocks[:_MESSAGES_HARD_CAP]
        # Attach on absolute positions before pagination so a call's block
        # is not re-emitted on a later page when its first record was
        # already surfaced (and consumed) on an earlier one.
        for entry, block in zip(projected, token_blocks):
            if block is not None:
                entry["tokens"] = block
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
    # Session outcome (F2.3): tool-call error rate + user-verdict
    # dictionary over the tail user turns; honest "unknown" when neither
    # signal exists.  The block carries only ai-r-authored strings and
    # dictionary marker labels (never raw session text), so it stays
    # outside the redaction pass by construction (SSOT ai_r.outcome).
    summary["outcome"] = _session_outcome(raw_messages, session.agent)
    # Session token usage (F3.3, only when requested): the flat block (exact
    # where the agent records it, labeled estimate otherwise) plus a separate
    # per-component estimate breakdown.  Both reuse the already-parsed
    # ``raw_messages`` so the transcript is not parsed a second time.
    # Integers + ai-r labels only, so they are outside the redaction pass by
    # construction.
    if with_tokens:
        summary["tokens"] = session_tokens(session, messages=raw_messages)
        summary["component_tokens"] = component_tokens(
            raw_messages, agent=session.agent
        )
    # Subagent rollup (F3.3, only when requested): parent's component_tokens +
    # one per spawned child, folded via :func:`ai_r.tokens.rollup_component_tokens`
    # (the SSOT that drops the parent's double-counted ``task`` bucket when
    # children are present — NIT 2 — and yields ``total: None`` when nothing is
    # measurable — NIT 3).  Independent of ``with_tokens``.  A childless parent
    # (or an agent that never records ``parent_uuid``) yields empty ``children``
    # and a ``total`` equal to the parent block.
    if include_subagents:
        parent_block = component_tokens(raw_messages, agent=session.agent)
        children_out: List[dict[str, Any]] = []
        child_blocks: List[Optional[dict[str, Any]]] = []
        for child in _children_of(session.uuid):
            child_parser: Optional[ParserModule] = _PARSERS.get(child.agent)
            child_msgs: List[Any] = []
            if child_parser is not None:
                try:
                    child_msgs = child_parser.read_messages(child.uuid)
                except (FileNotFoundError, ValueError, OSError):
                    child_msgs = []
            child_block = component_tokens(child_msgs, agent=child.agent)
            children_out.append({
                "uuid": child.uuid,
                "agent": child.agent.value.lower(),
                "component_tokens": child_block,
            })
            child_blocks.append(child_block)
        summary["subagent_rollup"] = {
            "parent": parent_block,
            "children": children_out,
            "total": rollup_component_tokens(parent_block, child_blocks),
        }
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


# A fully-unscoped ``find_file_edits`` (no agent, no since, no until) used to
# scan and emit the WHOLE corpus — months of history nobody asked for, and the
# main ingredient of an oversized response.  The MCP wrapper now narrows such
# calls to the last N days and says so in the response (``default_since`` +
# ``note``) — any explicit scope disables the default entirely.
_FIND_EDITS_DEFAULT_SINCE_DAYS = 7


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

    Size-bounded output: over-long ``intent`` / ``assistant`` fields are cut
    with a ``…[truncated]`` marker (named in the per-record
    ``truncated_fields``) and emission stops at a total byte budget
    (``output_truncated`` — distinct from the count-based ``truncated``).

    Default time window: a call with NO narrowing filter at all (no
    ``agent`` / ``since`` / ``until``) is scoped to the last
    7 days instead of the whole corpus; the response then carries
    ``default_since`` (the applied bound) plus a ``note`` saying so.  Any
    explicit scope disables the default — pass e.g. ``since="1970-01-01"``
    to deliberately scan the full history.

    Thin wrapper over :func:`ai_r.find_file_edits.find_file_edits`
    that translates the core ``ValueError`` contract into the
    ``{"error": "invalid_argument", "message": str(exc)}`` shape the
    MCP client expects.
    """
    # Mirror the ``session_stats`` scoped-check: an empty/whitespace value is
    # as unscoped as an absent one.
    scoped = bool(
        (agent and str(agent).strip())
        or (since and str(since).strip())
        or (until and str(until).strip())
    )
    default_since: Optional[str] = None
    if not scoped:
        default_since = (
            datetime.now(timezone.utc)
            - timedelta(days=_FIND_EDITS_DEFAULT_SINCE_DAYS)
        ).isoformat(timespec="seconds")
        since = default_since
    try:
        result = _find_file_edits_core(
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
    if default_since is not None:
        result["default_since"] = default_since
        result["note"] = (
            f"no agent/since/until filter was given: results are limited to "
            f"the last {_FIND_EDITS_DEFAULT_SINCE_DAYS} days "
            f"(since={default_since}). Pass agent, since or until "
            f"(e.g. since=\"1970-01-01\" for the full corpus) to widen the "
            f"scope."
        )
    return result


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
    Claude/OpenCode) plus the wrapper-aware classification: ``tool_kind``
    (``edit``/``write``/``read``/``bash``/``task``/``skill``/``mcp``/
    ``web``/``other``) and ``tool_resolved`` — the real name under a
    Skill/Task/MCP wrapper (subagent type, skill name, or
    ``"<server>:<tool>"``); ``None`` when there is no wrapper or the
    input carries no name signal.

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
    with_tokens: bool = False,
    token_scan_limit: int = _SESSION_STATS_TOKEN_SCAN_LIMIT,
) -> dict[str, Any]:
    """Summarise sessions, grouped and ranked — the *bird's-eye* audit view.

    Where ``find_file_edits`` / ``find_tool_calls`` return flat record
    streams, this rolls the *sessions themselves* up by one dimension so you
    can see how the work is distributed in a single call.

    ``group_by`` is one of:

    * ``"agent"`` (default) — claude vs codex vs opencode vs ...
    * ``"dir"``   — by working directory / project (the normalized
      ``project_dir`` first — one real directory = one bucket across
      agents — then ``cwd`` for codex/pi / project slug for claude;
      ``"(unknown)"`` for agents without any signal).
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

    ``with_tokens=True`` (F3.3) additionally reads every matched session's
    **token usage at request time** (nothing runs in the background) and
    adds a folded ``tokens`` block to each group and to ``totals``:
    ``{input, output, reasoning, cache_read, cache_write, total, exact,
    estimated, unknown}``.  Per session the numbers are *exact* where the
    agent's own files record usage (Claude ``message.usage``, Codex
    ``token_count``, OpenCode ``message.data.tokens``, Pi ``usage``); a
    session without a recorded signal (e.g. Antigravity) gets a
    transcript-volume **estimate** — tokenized with the optional
    `tiktoken` dependency (``pip install "ai-r[tokens]"``) when installed,
    else a rough chars/4 heuristic — and counts under ``estimated``, never
    silently mixed in as exact; no signal at all counts under ``unknown``.
    Sums that no session carried stay ``null`` (never a fabricated 0).
    The block contains only ai-r-computed integers and labels — no raw
    session text — so it is outside the redaction surface by construction.
    Default ``False``: byte-identical historical output, no extra reads.

    Scan guard (``token_scan_limit``): because ``with_tokens`` reads every
    matched session's files at request time, an **unscoped** run over a huge
    corpus is a multi-hour I/O storm.  When ``with_tokens`` is set with no
    narrowing filter (``agent``/``since``/``until``) and more than
    ``token_scan_limit`` sessions match, the call returns
    ``{"error": "scope_required", ...}`` (naming the count and the limit)
    INSTEAD of scanning — the check runs on the cheap inventory count before
    any file is read.  Narrow the scope, or raise ``token_scan_limit`` (``0``
    disables the cap) to force the full scan.  A permitted-but-large scan runs
    but carries a ``warning``.

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
            with_tokens=with_tokens,
            token_scan_limit=token_scan_limit,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def incidents(
    agent: Optional[str] = None,
    session: Optional[Union[str, List[str]]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    category: Optional[str] = None,
    confirmed: str = "include",
    reaction_window: int = 6,
    limit: int = 50,
    noise: str = "include",
    project_dir: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Dangerous shell commands + regret reactions — the *incidents* preset.

    One call answers "where did an agent run something destructive — and
    did it then apologise?".  A preset over the existing core, not a second
    engine: ONE ``query`` scan (``type="tool_call"``, ``tool_kind="bash"``)
    supplies the candidates, a deterministic **danger dictionary** (harvested
    from public agent-guardrail rule sets, calibrated on real history)
    selects the dangerous commands, and a bilingual (ru + en) **regret
    dictionary** scans the next ``reaction_window`` messages (default 6) for
    an apology/rollback reaction — the two-step check behind the
    ``confirmed`` flag.  Zero LLM, zero guessing: no dictionary hit → no
    incident; no reaction → ``confirmed: false``, never inferred.

    Filters (all parameters): ``agent``, ``session`` (uuid or list of
    uuids), ``since``/``until`` (ISO bounds on the call ts), ``category``
    (``fs``/``git``/``db``/``net`` — unknown values fail loud),
    ``confirmed`` (``include`` default | ``only`` | ``exclude``), ``noise``
    and ``project_dir`` (session-level, same semantics as ``query``).

    Each incident record carries the query event ``id`` (walk its context
    via ``query(relative_to=...)`` / ``read_session``), the matched
    ``patterns`` + ``categories``, a char-capped ``command`` fragment
    centred on the hit (token budget — full context stays on-demand),
    ``is_error`` (``null`` when the agent's format has no correlated
    outcome signal — honest, cross-agent), ``confirmed`` and ``reaction``
    (``message_index``/``offset``/``role``/marker labels/capped preview;
    ``null`` when unconfirmed).  ``count``/``confirmed_count``/
    ``by_pattern`` always reflect the FULL match set; ``limit`` (default
    50, ``0`` = no cap) bounds only the emitted records (``truncated``).

    Dictionary caveat (documented, not hidden): patterns are a
    deterministic dictionary, not a shell interpreter — a command that
    merely *mentions* a dangerous string (e.g. ``echo "rm -rf /"``) can
    still match.  Matching runs on the extracted command field (a Bash
    ``description`` alone never fires) and always on the RAW stored text;
    ``redact=true`` (default) masks secrets only in the emitted
    ``session_title``/``command``/``reaction.preview`` fields
    (``redactions`` type→count dict when anything was masked).  When
    ``count == 0`` the response carries ``diagnostics`` so an empty result
    is explainable (missing source dir vs all-excluding filter vs a
    genuinely clean history).

    Thin wrapper over :func:`ai_r.incidents.incidents` that translates the
    core ``ValueError`` contract into the ``{"error": "invalid_argument",
    "message": str(exc)}`` shape the MCP client expects.
    """
    try:
        return _incidents_core(
            agent=agent,
            session=session,
            since=since,
            until=until,
            category=category,
            confirmed=confirmed,
            reaction_window=reaction_window,
            limit=limit,
            noise=noise,
            project_dir=project_dir,
            redact=redact,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


@mcp.tool()
def network(
    agent: Optional[str] = None,
    session: Optional[Union[str, List[str]]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    kind: Optional[str] = None,
    risk: str = "include",
    domain: Optional[str] = None,
    limit: int = 50,
    noise: str = "include",
    project_dir: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Network-egress audit — the *network* preset (F4.3).

    One call answers "where did an agent reach out to the network — and
    how risky did those requests look?".  A preset over the existing core,
    not a second engine: ONE ``query`` scan (``type="tool_call"``,
    ``tool_kind="web"``) supplies the candidates — Claude
    ``WebFetch``/``WebSearch``, OpenCode ``webfetch``, Codex ``web_search``
    (surfaced from ``web_search_call`` rollout records),
    Gemini/Antigravity ``web_fetch``/``google_web_search``; Pi records no
    web tool (honest absence).  The request target (``url``/``query``) is
    extracted from each call's own input and assessed with a deterministic
    **risk dictionary**: ``plain_http``, ``credentials_in_url``,
    ``secret_in_url`` / ``secret_in_query`` (the redaction patterns double
    as the detector), ``ip_literal_host``, ``private_or_local_host``,
    ``punycode_host``.  Zero LLM, zero guessing: no extractable target →
    honest ``null`` fields; a risk fires only on parse/regex evidence.

    Filters (all parameters): ``agent``, ``session`` (uuid or list of
    uuids), ``since``/``until`` (ISO bounds on the call ts), ``kind``
    (``fetch``|``search`` — derived from the extracted fields, unknown
    values fail loud), ``risk`` (``include`` default | ``only`` |
    ``exclude``), ``domain`` (host equals-or-subdomain match), ``noise``
    and ``project_dir`` (session-level, same semantics as ``query``).

    Each request record carries the query event ``id`` (walk its context
    via ``query(relative_to=...)`` / ``read_session``), the derived
    ``kind``, char-capped ``url``/``query`` (token budget — full context
    stays on-demand), ``domain``, the ``risks`` labels and tri-state
    ``is_error`` (``null`` when the agent's format has no correlated
    outcome signal — honest, cross-agent).  ``count``/``risky_count``/
    ``by_domain``/``by_risk`` always reflect the FULL match set; ``limit``
    (default 50, ``0`` = no cap) bounds only the emitted records
    (``truncated``).

    Honesty caveats (documented, not hidden): risk labels are a
    deterministic dictionary, not a threat oracle; MCP-mediated network
    access (browser-automation servers etc.) stays under
    ``tool_kind="mcp"`` — a name alone cannot prove an MCP server touches
    the network, so it is never guessed into this audit.  Risk assessment
    runs on the RAW stored strings; ``redact=true`` (default) masks
    secrets only in the emitted ``url``/``query``/``session_title`` fields
    (``redactions`` type→count dict when anything was masked).  When
    ``count == 0`` the response carries ``diagnostics`` so an empty result
    is explainable.

    Thin wrapper over :func:`ai_r.network.network` that translates the
    core ``ValueError`` contract into the ``{"error": "invalid_argument",
    "message": str(exc)}`` shape the MCP client expects.
    """
    try:
        return _network_core(
            agent=agent,
            session=session,
            since=since,
            until=until,
            kind=kind,
            risk=risk,
            domain=domain,
            limit=limit,
            noise=noise,
            project_dir=project_dir,
            redact=redact,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}


# --- diff-shaped response size caps (session_diff / diff) -------------------
# A session with one big ``Write`` (an 89 KB HTML body was observed) used to
# return the full body TWICE — once in the write hunk, once in the stitched
# per-file ``diff`` — for a 145K-char MCP response no field bounded.  Mirror
# the ``find_file_edits`` bound (same shared :func:`cap_field`): over-long
# fields are cut with a ``…[truncated]`` marker and named in the per-file
# ``truncated_fields`` (indexed paths, e.g. ``edits[2].hunks[0].content``),
# and whole-file emission stops at a total byte budget (``output_truncated``).
# Caps run AFTER the core's redaction pass — the ``network`` ordering, so a
# boundary-sliced secret can never leak.  The full body stays reachable on
# demand via ``get_body`` (the edit's ``tool_call`` id) / ``read_session``.
_DIFF_INTENT_CHARS_CAP = 1_000   # per-edit driving user intent (FFE value)
_DIFF_HUNK_CHARS_CAP = 4_000     # per-hunk body field (old/new/content/cmd)
_DIFF_TEXT_CHARS_CAP = 20_000    # per-file stitched ``diff`` text
_DIFF_OUTPUT_BYTES_BUDGET = 4_000_000  # ~4 MB of serialized file entries


def _cap_diff_result(result: dict[str, Any]) -> dict[str, Any]:
    """Size-bound a ``{"files": [...]}`` diff result in place.

    Shared by the ``session_diff`` and ``diff`` MCP wrappers (same shape;
    the ``diff`` verb's flat per-file ``hunks`` list aliases the very hunk
    dicts under ``edits[*].hunks``, so one walk bounds both views).  Error
    dicts and unknown shapes pass through untouched.
    """
    files = result.get("files")
    if not isinstance(files, list):
        return result
    for entry in files:
        if not isinstance(entry, dict):
            continue
        truncated_fields: List[str] = []
        for e_idx, edit in enumerate(entry.get("edits") or []):
            if not isinstance(edit, dict):
                continue
            new_val, hit = _cap_field(edit.get("intent"), _DIFF_INTENT_CHARS_CAP)
            if hit:
                edit["intent"] = new_val
                truncated_fields.append(f"edits[{e_idx}].intent")
            for h_idx, hunk in enumerate(edit.get("hunks") or []):
                if not isinstance(hunk, dict):
                    continue
                for field in ("old", "new", "content", "cmd"):
                    if field not in hunk:
                        continue
                    new_val, hit = _cap_field(hunk.get(field), _DIFF_HUNK_CHARS_CAP)
                    if hit:
                        hunk[field] = new_val
                        truncated_fields.append(
                            f"edits[{e_idx}].hunks[{h_idx}].{field}"
                        )
        new_val, hit = _cap_field(entry.get("diff"), _DIFF_TEXT_CHARS_CAP)
        if hit:
            entry["diff"] = new_val
            truncated_fields.append("diff")
        entry["truncated_fields"] = truncated_fields

    # Byte-budget backstop (mirrors ``find_file_edits``): stop emitting whole
    # file entries once the cumulative serialized size exceeds the budget.
    # ``count`` keeps the TRUE total, so ``output_truncated=True`` +
    # ``count > len(files)`` reads as "more files changed than shown".
    output_truncated = False
    budgeted: List[dict[str, Any]] = []
    running = 0
    for entry in files:
        running += len(json.dumps(entry, ensure_ascii=False, default=str))
        if running > _DIFF_OUTPUT_BYTES_BUDGET and budgeted:
            output_truncated = True
            break
        budgeted.append(entry)
    if output_truncated:
        result["files"] = budgeted
    result["output_truncated"] = output_truncated
    return result


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

    Size-bounded output (mirrors ``find_file_edits``): over-long ``intent``
    / hunk bodies / per-file ``diff`` text are cut with a ``…[truncated]``
    marker and named in the per-file ``truncated_fields`` (indexed paths),
    and whole-file emission stops at a total byte budget
    (``output_truncated``; ``count`` keeps the true total).  The full edit
    body stays reachable on demand via ``get_body`` / ``read_session``.

    Thin wrapper over :func:`ai_r.session_diff.session_diff` that
    translates the core ``ValueError`` contract into the
    ``{"error": "invalid_argument", "message": str(exc)}`` shape the MCP
    client expects.
    """
    try:
        result = _session_diff_core(
            session_uuid=session_uuid,
            agent=agent,
            path=path,
            redact=redact,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    return _cap_diff_result(result)


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
            * ``"semantic"`` — F5.1 (optional ``ai-r[semantic]``): the
              BM25 top-50 candidates re-ranked by *meaning* with a local
              multilingual embedding model (cross-lingual ru↔en,
              synonyms); the response carries a ``semantic`` dict —
              either the active ranking (``active: true``, model,
              candidate count, blend weight) or the honest degradation
              notice (``active: false`` + plain-words ``reason`` +
              ``fallback: "bm25"``, order stays BM25 — never a crash).
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
        date bounds, cause hints) so an empty result is explainable.  With
        ``sort="semantic"`` the dict also carries a ``semantic`` report
        (active ranking vs BM25 fallback + reason).

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
    if sort_lower not in ("relevance", "date", "semantic"):
        return {
            "error": "invalid_argument",
            "message": f"unknown sort {sort!r}; "
                       "expected relevance, date or semantic",
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

    semantic_info: dict[str, Any] = {}
    if sort_lower in ("relevance", "semantic") and summaries:
        # Flatten phrase terms into BM25 query tokens; lazily tokenise only
        # the matched docs (never the whole haystack cache).
        query_tokens: List[str] = []
        for term in positive:
            query_tokens.extend(_tokenize(term))
        docs_tokens = [_tokenize(text) for text in score_texts]
        scores = _bm25_scores(query_tokens, docs_tokens)
        order: Optional[List[int]] = None
        if sort_lower == "semantic":
            # F5.1: BM25 supplies the candidate pool; the local embedding
            # model re-ranks it by meaning.  ``None`` = the optional
            # dependencies/model are missing or failed — honest BM25
            # fallback (reported in the ``semantic`` response field),
            # never a crash.
            order, semantic_info = _semantic_order(
                " ".join(positive) or needle, score_texts, scores
            )
        if order is None:
            # ``sorted`` is stable: equal scores preserve list_sessions
            # order (newest-first), giving a deterministic recency
            # tie-break.
            order = sorted(
                range(len(summaries)), key=lambda i: scores[i], reverse=True
            )
        summaries = [summaries[i] for i in order]
    elif sort_lower == "semantic" and not summaries:
        # Zero matches: nothing to rank, but still report availability so
        # the caller learns whether semantic ranking was even possible.
        _order_unused, semantic_info = _semantic_order(needle, [], [])
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
    if semantic_info:
        result["semantic"] = semantic_info
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
    session: Optional[Union[str, List[str]]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    file: Optional[str] = None,
    tool: Optional[str] = None,
    tool_kind: Optional[str] = None,
    model: Optional[str] = None,
    text: Optional[str] = None,
    sort: str = "date",
    relative_to: Optional[str] = None,
    direction: str = "prev",
    n: Union[int, str] = 1,
    step_type: str = "user_turn",
    limit: int = 0,
    with_intent: bool = False,
    noise: str = "include",
    project_dir: Optional[str] = None,
    parent: Optional[str] = None,
    group: Optional[str] = None,
    kind: Optional[str] = None,
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
    * ``session`` — restrict to a single session uuid, OR a list of uuids
      (the union of those sessions' events in one call — e.g. the ids
      picked from a ``search_sessions`` / ``list_sessions`` result).
      Duplicates collapse; an unknown uuid contributes nothing.  An empty
      list or a non-string item is a fail-loud ``invalid_argument`` —
      never a silent unfiltered scan.
    * ``since`` / ``until`` — ISO-8601 bounds (inclusive) on the event ts.
    * ``file`` — substring matched against an event's referenced file path.
    * ``tool`` — substring (pattern) matched against the referenced tool
      name OR the resolved name under a wrapper (``tool_resolved``) — so
      ``tool="commit"`` also finds the Skill call that ran the ``commit``
      skill.
    * ``tool_kind`` — exact match against the wrapper-aware classification
      of a tool call: ``edit`` / ``write`` / ``read`` / ``bash`` / ``task``
      (subagent spawn) / ``skill`` / ``mcp`` / ``web`` / ``other``.
      Every ``tool_call`` event carries ``tool_kind`` (in ``refs`` and as
      a top-level field); wrappers whose input names the real actor also
      carry ``tool_resolved`` — the subagent type under Task/Agent/
      spawn_agent, the skill name under Skill/SlashCommand, or
      ``"<server>:<tool>"`` for a Claude-style ``mcp__<server>__<tool>``
      call.  No signal → no ``tool_resolved`` (never guessed).  An
      unknown ``tool_kind`` value is a fail-loud ``invalid_argument``.
    * ``model`` — exact, case-insensitive match against the model that
      produced the event's message: an ``assistant_turn`` /
      ``tool_call`` / ``plan_event`` inherits the model of the assistant
      message behind it and carries it as a top-level ``model`` field
      (absent without a signal — user turns, Antigravity — so
      ``aggregate(group_by="model")`` buckets those under
      ``"(unknown)"``).  Model ids are agent-defined strings (no fixed
      vocabulary); events without a signal never match; an empty string
      is a fail-loud ``invalid_argument``.
    * ``text`` — substring matched against event text.  With
      ``sort="relevance"`` survivors are BM25-ranked using the **same
      scorer** as ``search_sessions``; ``sort="semantic"`` (F5.1,
      optional ``ai-r[semantic]``) re-ranks the BM25 top-50 candidates
      by *meaning* with a local multilingual embedding model
      (cross-lingual ru↔en, synonyms) — the response carries a
      ``semantic`` dict reporting either the active ranking
      (``active: true``, model, candidate count, blend weight) or the
      honest degradation (``active: false`` + plain-words ``reason`` +
      ``fallback: "bm25"`` — the order is then plain BM25, never a
      crash); ``sort="date"`` (default) orders by timestamp ascending.
    * ``relative_to`` (event id) + ``direction`` (``prev``|``next``) +
      ``n`` (a positive integer, default ``1``, or ``"all"``) — the
      neighbouring-turn walk.  A numeric string (``"3"``) is deprecated
      and will be rejected in 0.6.0 — pass an int or ``"all"``.
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

    ``parent`` also filters at the *session* level: keep only events of
    sessions that are a **descendant** (transitively, any depth) of this
    session uuid in the subagent ``parent_uuid`` tree — the whole spawned
    subtree below ``parent`` (direct children plus nested).  ``parent``
    itself is excluded (its own events are reachable via
    ``session=<parent>``).  An unknown uuid matches nothing (honest empty
    result).  Ignored on the ``relative_to`` walk, like every other filter
    facet.

    ``group`` filters at the *event* level, plan_events only: keep only the
    plan_events whose ``task_id`` (the plan-task grouping key — plan-file
    slug or normalized title) equals this value.  Non-plan events never
    match when ``group`` is set, so combining ``group`` with a non-plan
    ``type`` yields an honest empty result.

    ``redact=True`` (default) masks secrets in the emitted ``text`` /
    ``intent`` fields as ``[REDACTED_<TYPE>]`` and adds a top-level
    ``redactions`` type→count dict when any replacement happened;
    ``redact=False`` returns raw content.  Redaction is emission-time only:
    the ``text`` facet (and every other filter) matches the RAW stored text.

    Events are reference-by-default: each emitted event's ``text`` is a
    **preview** cut to ~160 chars (applied after redaction).  A real cut is
    marked with a trailing ``…`` and ``text_truncated: true`` (absent when
    nothing was cut).  ``id``/``refs``/``sha256`` are untouched — fetch the
    full body on demand with ``get_body(id)``.

    ``kind`` was **removed** — it duplicated ``noise`` (``noise="only"`` for
    subagents, ``noise="exclude"`` for top-level).  It is kept in the signature
    only as a fail-loud tombstone: passing any value returns an
    ``invalid_argument`` error pointing at ``noise`` rather than silently
    ignoring it (the MCP transport would otherwise drop an unknown argument and
    return an unfiltered result — a silent wrong answer).

    Returns ``{"events": [...], "count": N}`` or the standard
    ``{"error": ..., "message": ...}`` dict on invalid arguments.  When
    ``count == 0`` the dict additionally carries ``diagnostics`` (scanned
    agents + session counts, corpus date bounds, cause hints) so an empty
    result is explainable.
    """
    if kind is not None:
        return {
            "error": "invalid_argument",
            "message": (
                "the 'kind' facet was removed as a duplicate of 'noise'; "
                "use noise='only' for subagent sessions or noise='exclude' "
                "for top-level sessions"
            ),
        }
    # Filled by the core scan with the per-agent list_sessions() results,
    # reused by the empty-result diagnostics so an empty result never pays
    # for a second corpus walk.
    scanned_sessions: dict[str, Any] = {}
    redactions: dict[str, int] = {}
    semantic_info: dict[str, Any] = {}
    try:
        events = _query_core(
            type=type,
            agent=agent,
            session=session,
            since=since,
            until=until,
            file=file,
            tool=tool,
            tool_kind=tool_kind,
            model=model,
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
            parent=parent,
            group=group,
            scanned_sessions_out=scanned_sessions,
            redact=redact,
            redactions_out=redactions,
            semantic_out=semantic_info,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    # Reference-by-default (QRY-1): cut emitted ``text`` to a preview at the
    # output boundary — AFTER the core's emission-time redaction, so secrets
    # in a long head are masked before the cut.  In-process consumers call
    # the core directly and keep the full text.
    _preview_event_texts(events)
    result: dict[str, Any] = {"events": events, "count": len(events)}
    if redactions:
        result["redactions"] = redactions
    if semantic_info:
        result["semantic"] = semantic_info
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
                "tool_kind": tool_kind,
                "model": model,
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
    bodies: str = "final",
    feedback: bool = True,
    rounds: str = "all",
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

    F3.4 default schema (measured ≈×3.7 cheaper than "everything inlined"):
    the ``final`` plan's full text is inlined (``body`` +
    ``body_source`` — ``"approval_edited_by_user"`` when the user's
    approval carried an edited plan, which is the AUTHORITATIVE text and
    overrides the signal/file body, else ``"plan_signal"``); drafts stay
    references (bodies via ``get_body``); every «plan quote → user comment»
    pair extracted from the user's plan responses is returned under
    ``feedback``, each with a ``ref`` (``"<session>:pf<N>"``) that
    ``get_body`` resolves to the FULL raw response.  Only agents with an
    interactive plan-approval flow have the feedback signal (today: Claude —
    an ``ExitPlanMode`` verdict or a rejected plan-file ``Write``); others
    honestly contribute nothing.  Technical failures and bare no-comment
    rejections are filtered out.

    F3.4 v2 additions: every plan atom carries ``version`` — its 1-based
    revision number within the task group, chronological (drafts are
    ``v1…vN-1``, the final is ``vN``); every feedback pair carries
    ``plan_version`` (the answered revision's number), ``round`` (1-based
    feedback-round number within the session — one round per user response
    that produced pairs) and ``section`` — the heading of the plan section
    the quote anchors to.  Quotes are selected from the RENDERED plan, so
    the anchor match strips markdown markup from both sides; a quote that
    matches no section — or more than one — gets an honest ``null``
    anchor, never a nearest guess.

    Args:
        session: Restrict to one session uuid (recommended).
        kind: Optional filter — ``draft`` | ``final`` | ``completed_major``.
        group: Grouping strategy; only ``"task"`` is supported.
        agent: Optional agent filter (claude/codex/opencode/antigravity/pi).
        redact: When ``True`` (default) secrets in the emitted plan/feedback
            fields (``title``/``steps``/``body``/``quote``/``comment``…)
            are masked as ``[REDACTED_<TYPE>]`` and the response carries a
            ``redactions`` type→count dict when any replacement happened;
            ``False`` returns raw content.
        bodies: ``"final"`` (default) inlines the final plan's full text;
            ``"none"`` returns reference-only atoms.
        feedback: ``True`` (default) adds the ``feedback`` pair list +
            ``feedback_count``; ``False`` omits both (historical shape).
        rounds: ``"all"`` (default) returns every feedback round;
            ``"last"`` keeps only each session's final round (v2).  Any
            other value fails loud.

    Returns:
        ``{"plans": [...], "count": N, "feedback": [...],
        "feedback_count": M}`` — each plan carries
        ``id/session_id/agent/title/task_id/kind/version/path/steps/status/
        refs/sha256`` (+ ``body``/``body_source`` on the ``final`` when
        ``bodies="final"``); each feedback pair carries
        ``session_id/agent/plan_id/plan_version/verdict/round/quote/comment/
        section/ref/ts`` (``verdict`` ∈ ``rejected`` | ``stay_in_plan_mode``;
        ``quote`` is ``null`` for a free-text comment; ``plan_version``/
        ``section`` are ``null`` without a signal).  Draft bodies and raw
        responses stay on-demand via :func:`get_body`.  Standard
        ``{"error": ..., "message": ...}`` dict on invalid arguments.
    """
    try:
        if rounds not in ("all", "last"):
            raise ValueError(
                f"rounds must be 'all' or 'last', got {rounds!r}"
            )
        plans = _plan_core(
            session=session, kind=kind, group=group, agent=agent,
            bodies=bodies,
        )
        pairs = (
            _plan_feedback_core(session=session, agent=agent, rounds=rounds)
            if feedback else None
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    result: dict[str, Any] = {"plans": plans, "count": len(plans)}
    if pairs is not None:
        result["feedback"] = pairs
        result["feedback_count"] = len(pairs)
    # Emission-time redaction (F2.1): the core runs its internal query with
    # ``redact=False`` and defers the single masking pass to this wrapper.
    if redact:
        redactions: dict[str, int] = {}
        redacted_plans, counts = _redact_value(plans)
        if counts:
            result["plans"] = redacted_plans
            _merge_redactions(redactions, counts)
        if pairs is not None:
            redacted_pairs, fb_counts = _redact_value(pairs)
            if fb_counts:
                result["feedback"] = redacted_pairs
                _merge_redactions(redactions, fb_counts)
        if redactions:
            result["redactions"] = redactions
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
            ``date`` / ``kind`` / ``file`` / ``model`` — query rows carry
            the producing ``model`` where the format records one / …).
            Missing/empty values bucket under ``"(unknown)"``.
        metrics: Which numbers each bucket carries.  One or more of
            ``count`` / ``sessions`` / ``edits`` / ``intents`` / ``agents`` /
            ``messages`` / ``files`` / ``tokens`` / ``component_tokens``.
            Defaults to ``["count"]``.
            ``tokens`` (F3.3) folds per-row ``tokens`` blocks (the shape
            ``session_stats(with_tokens=True)`` rows carry, or a bare int
            total) into ``{input, output, reasoning, cache_read,
            cache_write, total, exact, estimated, unknown}`` — sums over
            rows that carry each field (``null`` when none does) plus
            honest provenance counters (``exact + estimated + unknown ==
            len(rows)``).
            ``component_tokens`` (F3.3) folds per-row ``component_tokens``
            blocks (the shape :func:`ai_r.tokens.component_tokens` produces,
            as ``read_session(with_tokens=True)`` attaches) into summed
            event-taxonomy components (``user_turn`` / ``assistant_turn`` /
            ``thinking`` / ``plan`` and a ``tool_call`` per-kind sub-dict) +
            ``total`` + provenance counters (``estimated`` / ``unknown``;
            never ``exact`` — always an estimate).  A component/kind no row
            carried stays absent (never a fabricated ``0``).
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
        (same shape + caveats as ``session_diff``, size-bounded the same
        way: capped fields named in the per-file ``truncated_fields``, byte
        budget → ``output_truncated``) or the standard
        ``{"error": ..., "message": ...}`` dict on an unsupported ``format``.
    """
    try:
        result = _diff_core(rows, per_file=per_file, format=format, redact=redact)
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    return _cap_diff_result(result)


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
        ``{"session_id", "agent", "model", "candidates": [...], "verified",
        "self"}`` where ``session_id`` / ``agent`` describe the
        highest-priority candidate, ``model`` is the current session's
        model — the LAST assistant ``model`` recorded in its transcript
        (``null`` when identity is incomplete or the format records no
        model signal — never guessed) — and ``candidates`` is the full
        cascade for disambiguation.  Returns ``{"error": ...,
        "message": ...}`` on an unknown ``agent`` hint.
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
    """Concatenate message text + thinking + tool_use inputs + tool_result contents.

    Lowercased once on return. Includes content that lives in tool calls
    and tool results, not just plain text — this is what makes the
    full-text search actually useful for finding references buried in
    Bash/file/etc. invocations.

    ``m.thinking`` (model reasoning) is folded in too so body/search
    matches reasoning for ALL agents (feature-for-all-where-signal).  This
    also preserves OpenCode's searchability after its reasoning was moved
    out of ``text`` into ``thinking``, and newly surfaces Claude / Codex /
    Pi reasoning that was previously discarded.
    """
    chunks: List[str] = []
    total_chars = 0
    for m in messages:
        text = getattr(m, "text", "")
        if isinstance(text, str) and text:
            chunks.append(text)
            total_chars += len(text)
        thinking = getattr(m, "thinking", "")
        if isinstance(thinking, str) and thinking:
            chunks.append(thinking)
            total_chars += len(thinking)
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
    """Entry point for the ``ai-r-mcp`` console script.

    Transport is selected by ``AI_R_MCP_TRANSPORT`` (default ``stdio`` for full
    back-compat).  ``http`` runs a single shared streamable-http server on
    localhost — the fix for the per-agent stdio swarm that re-scans the corpus
    N times (see :mod:`ai_r.serve`).
    """
    transport = resolve_transport()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return 0
    return run_http(mcp)


if __name__ == "__main__":
    raise SystemExit(main())
