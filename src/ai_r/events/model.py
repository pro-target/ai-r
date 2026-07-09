"""Event stream construction — plan-signal detection + ``iter_events``.

Normalises each parser's :class:`~ai_r.parsers.models.Message` objects (plus
their embedded ``tool_use`` calls) into a flat, chronological sequence of
:class:`~ai_r.events._common.Event` records, and detects the agent-specific
*plan signals* that become ``plan_event`` records.

The plan-signal machinery (``_PlanSignal`` + ``_plan_signals_for_session`` +
``_normalize_task_key``) lives here because both this module (which emits the
``plan_event`` inline in the stream) and :mod:`ai_r.events.plan` (which
re-derives a signal for ``get_body`` / step enrichment) need it — keeping it in
one place below ``iter_events`` avoids an intra-package cycle.

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from ai_r.find_file_edits import to_utc_aware
from ai_r.parsers import PARSERS, Message, iso, target_agents
from ai_r.parsers._common import project_dir_matches
from ai_r.parsers._noise import noise_allows, validate_noise
from ai_r.user_refs import dedup_user_refs, extract_user_refs_from_text

from ai_r.events._common import (
    Event,
    _coerce_tool_input,
    _mk_event,
    _path_from_payload,
    classify_tool,
    resolve_tool,
)


# --- plan signals (INTERNAL, agent-specific normalization) ----------------
# Different agents record a "plan" through different signals.  This layer
# maps each parser's raw signal to a single normalized ``plan_event`` so the
# consumer never sees ``ExitPlanMode`` / ``update_plan`` /
# ``implementation_plan.md`` — only a unified plan.  The table below is a
# deliberate implementation detail; nothing outside this module keys off it.
#
# The recognised per-agent signals (``agent_signal`` tag in refs):
#
# * claude  — ``ExitPlanMode`` tool_use (input carries ``plan`` text) OR a
#             ``Write`` tool_use whose path matches ``plans/*.md`` (input
#             carries the full body).
# * codex   — ``update_plan`` tool_use (input carries ``steps[]`` with
#             ``status``); rewritten each call, so the LAST call in a task
#             group is the final plan.
# * antigravity — ``implementation_plan.md`` in the session's brain dir (a
#             file, not a message tool_use — emitted once per session).
# * opencode / pi — no plan signal → nothing emitted.

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_CLAUDE_PLAN_WRITE_RE = re.compile(r"plans/[^/]*\.md$", re.IGNORECASE)
# The ``plans/<slug>.md`` portion of a Claude plan-file path — the stable
# per-task slug, extracted so two different absolute paths that share the same
# plan slug still group as one task.
_CLAUDE_PLAN_SLUG_RE = re.compile(r"(plans/[^/]*\.md)$", re.IGNORECASE)

_WS_RE = re.compile(r"\s+")


def _normalize_task_key(title: Optional[str]) -> str:
    """Normalize a plan title into a stable task-grouping key.

    Grouping is by *task*, NOT by slug/filename: lower-cased, whitespace
    collapsed, surrounding punctuation trimmed.  Two plan revisions with the
    same human title collapse to one task even if their files differ.
    """
    text = (title or "").strip().lower()
    text = _WS_RE.sub(" ", text)
    return text.strip(" #:-—")


def _claude_plan_slug(path: Optional[str]) -> Optional[str]:
    """Return the ``plans/<slug>.md`` slug of a Claude plan path, else ``None``."""
    if not isinstance(path, str) or not path:
        return None
    match = _CLAUDE_PLAN_SLUG_RE.search(path)
    if match:
        return match.group(1).lower()
    return None


@dataclass(frozen=True)
class _PlanSignal:
    """One detected plan signal within a session (internal, pre-normalization).

    Carries everything the normalized :class:`Plan` needs plus the body that
    is *not* inlined into the :class:`Event` (fetched on demand by
    :func:`get_body`).

    ``task_key`` is the STABLE task-grouping key (see
    :func:`_plan_signals_for_session`).  It is the plan-file path/slug when
    the agent has one (Claude ``plans/<slug>.md``, Antigravity
    ``implementation_plan.md``) so title drift within one iteration chain
    never splits a single task; it falls back to the normalized title only
    when no file path is available (Codex ``update_plan``, or a Claude
    ``ExitPlanMode`` that precedes any plan-file Write).
    """

    title: str
    agent_signal: str
    path: Optional[str] = None
    body: Optional[str] = None
    steps: Optional[Tuple[dict, ...]] = None
    status: Optional[str] = None
    message_index: int = -1
    task_key: str = ""
    # The originating tool_use id (when the transcript records one) — lets
    # the plan-response layer (F3.4) correlate a user's approval/rejection
    # tool_result back to the exact plan revision it answered.  ``None`` for
    # file-based signals and for transcripts that predate call ids.
    tool_use_id: Optional[str] = None


def _title_from_markdown_body(body: str) -> Optional[str]:
    """Return the first ``# `` heading of a markdown plan body, if any."""
    if not isinstance(body, str) or not body:
        return None
    match = _HEADING_RE.search(body)
    if match:
        return match.group(1).strip()
    return None


def _plan_signal_from_tool(
    tool: dict, *, agent: str, message_index: int
) -> Optional[_PlanSignal]:
    """Detect a plan signal in one assistant ``tool_use`` entry.

    Covers the Claude (``ExitPlanMode`` / ``Write plans/*.md``) and Codex
    (``update_plan``) message-level signals.  Antigravity's file-based
    signal is handled separately in :func:`_antigravity_plan_signal`.
    Returns ``None`` when the tool is not a plan signal.
    """
    name = tool.get("name", "")
    if not isinstance(name, str) or not name:
        return None
    payload = _coerce_tool_input(tool.get("input", ""))
    raw_tuid = tool.get("tool_use_id")
    tool_use_id = raw_tuid if isinstance(raw_tuid, str) and raw_tuid else None

    if agent == "claude":
        if name == "ExitPlanMode":
            body = ""
            if isinstance(payload, dict):
                raw = payload.get("plan")
                if isinstance(raw, str):
                    body = raw
            title = _title_from_markdown_body(body) or "Plan"
            return _PlanSignal(
                title=title,
                agent_signal="claude:ExitPlanMode",
                body=body or None,
                message_index=message_index,
                tool_use_id=tool_use_id,
            )
        if name in ("Write", "write", "write_file", "create_file"):
            fpath = _path_from_payload(payload)
            if fpath and _CLAUDE_PLAN_WRITE_RE.search(fpath):
                body = ""
                if isinstance(payload, dict):
                    raw = payload.get("content")
                    if isinstance(raw, str):
                        body = raw
                title = _title_from_markdown_body(body) or fpath.rsplit("/", 1)[-1]
                return _PlanSignal(
                    title=title,
                    agent_signal="claude:Write(plans/*.md)",
                    path=fpath,
                    body=body or None,
                    message_index=message_index,
                    tool_use_id=tool_use_id,
                )
        return None

    if agent == "codex":
        if name == "update_plan":
            steps: Tuple[dict, ...] = ()
            status: Optional[str] = None
            title = "Plan"
            if isinstance(payload, dict):
                # Codex ``update_plan`` carries the step array under the
                # ``plan`` key (verified across the vault); ``steps`` is kept
                # only as a defensive fallback for any other shape.
                raw_steps = payload.get("plan")
                if not isinstance(raw_steps, list):
                    raw_steps = payload.get("steps")
                if isinstance(raw_steps, list):
                    steps = tuple(s for s in raw_steps if isinstance(s, dict))
                raw_title = payload.get("name") or payload.get("explanation")
                if isinstance(raw_title, str) and raw_title.strip():
                    title = raw_title.strip()
                elif steps:
                    # Fall back to the first step's text as a stable title so
                    # task grouping has something agent-neutral to key on.
                    first = steps[0]
                    step_text = first.get("step") or first.get("text") or ""
                    if isinstance(step_text, str) and step_text.strip():
                        title = step_text.strip()
                # Overall status: last non-completed step, else "completed".
                status = _codex_plan_status(steps)
            return _PlanSignal(
                title=title,
                agent_signal="codex:update_plan",
                steps=steps or None,
                status=status,
                message_index=message_index,
                tool_use_id=tool_use_id,
            )
        return None

    return None


def _codex_plan_status(steps: Sequence[dict]) -> Optional[str]:
    """Roll a Codex ``update_plan`` ``steps[]`` up to one overall status."""
    if not steps:
        return None
    statuses = [
        s.get("status") for s in steps if isinstance(s.get("status"), str)
    ]
    if not statuses:
        return None
    if all(st == "completed" for st in statuses):
        return "completed"
    if any(st == "in_progress" for st in statuses):
        return "in_progress"
    return "pending"


def _antigravity_plan_signal(session_path: str) -> Optional[_PlanSignal]:
    """Detect Antigravity's ``implementation_plan.md`` plan in a brain dir.

    Reuses the antigravity parser's knowledge that the plan lives as a
    markdown file inside the session's brain directory (``session.path``).
    Emitted once per session (file-based, not message-based).  Returns
    ``None`` when the file is absent/unreadable.
    """
    from pathlib import Path

    if not session_path:
        return None
    plan_file = Path(session_path) / "implementation_plan.md"
    if not plan_file.is_file():
        return None
    try:
        body = plan_file.read_text(encoding="utf-8")
    except OSError:
        return None
    title = _title_from_markdown_body(body) or "implementation_plan.md"
    return _PlanSignal(
        title=title,
        agent_signal="antigravity:implementation_plan.md",
        path=str(plan_file),
        body=body or None,
        message_index=-1,
    )


def _plan_signals_for_session(
    messages: Sequence[Any],
    *,
    agent: str,
    session_path: str,
) -> List[_PlanSignal]:
    """Return the ordered plan signals detected in one session.

    Message-level signals (Claude / Codex) are collected in message order;
    Antigravity's file-based signal is appended once.  Order matters for
    ``get_body`` (matched by seq) and for task grouping (last = final).

    Each returned signal carries a stable ``task_key`` (see
    :class:`_PlanSignal`).  For Claude the key is the ``plans/<slug>.md``
    slug — Write signals carry it directly; an ``ExitPlanMode`` (which has no
    path) *inherits the slug of the nearest preceding plan-file Write in the
    same session*, so a title that drifts within one plan-file iteration
    chain no longer splits a single task.  When no slug precedes an
    ``ExitPlanMode`` the key falls back to the normalized title.  For
    Antigravity the key is the plan-file path; for Codex (no file) it is the
    normalized title (contiguous ``update_plan`` runs group naturally).
    """
    from dataclasses import replace

    signals: List[_PlanSignal] = []
    if agent in ("claude", "codex"):
        last_slug: Optional[str] = None  # nearest preceding Claude plan slug
        for idx, msg in enumerate(messages):
            if getattr(msg, "role", None) != "assistant":
                continue
            for tool in getattr(msg, "tool_use", ()) or ():
                if not isinstance(tool, dict):
                    continue
                sig = _plan_signal_from_tool(
                    tool, agent=agent, message_index=idx
                )
                if sig is None:
                    continue
                if agent == "claude":
                    slug = _claude_plan_slug(sig.path)
                    if slug is not None:
                        # A plan-file Write: it defines the current slug and
                        # every following ExitPlanMode inherits it.
                        last_slug = slug
                        task_key = slug
                    elif last_slug is not None:
                        # ExitPlanMode after a Write: inherit its slug.
                        task_key = last_slug
                    else:
                        # No slug seen yet — fall back to normalized title.
                        task_key = _normalize_task_key(sig.title)
                    sig = replace(sig, task_key=task_key)
                else:  # codex — no file, key by normalized title
                    sig = replace(sig, task_key=_normalize_task_key(sig.title))
                signals.append(sig)
    elif agent == "antigravity":
        sig = _antigravity_plan_signal(session_path)
        if sig is not None:
            # File-based: the plan-file path IS the task key.
            signals.append(replace(sig, task_key=(sig.path or _normalize_task_key(sig.title))))
    # opencode / pi: no plan signal.
    return signals


# --- plan responses (F3.4, INTERNAL agent-specific normalization) ----------
# When an agent has an interactive plan-approval flow, the user's verdict on
# a proposed plan revision comes back as a tool_result answering the plan
# tool_use.  Today only Claude has that flow (``ExitPlanMode``); Codex
# ``update_plan`` is fire-and-forget and Antigravity's plan is a file — no
# response signal exists there, so those agents honestly yield nothing.
#
# The recognised Claude response formats (verified against real vaults):
#
# 1. rejection with selections — "The user doesn't want to proceed … the
#    user said:" followed by one or more "On selected text:" blocks, each a
#    ``> ``-quoted excerpt of the RENDERED plan plus the user's comment;
# 2. stay-in-plan-mode — "User chose to stay in plan mode …\nComments on
#    the plan:" followed by ``[Re: "<quote>"] <comment>`` entries;
# 3. free-text rejection — "… the user said:" with no selection (the whole
#    tail is one comment, quote = None);
# 4. approval — "User has approved your plan …", optionally carrying the
#    AUTHORITATIVE final text under "## Approved Plan (edited by user):"
#    (the plan file on disk can diverge from what the user approved).
#
# Technical failures ("Tool permission request failed…", stream errors) and
# bare no-text rejections (the user pressed reject without a word) are
# filtered out — they carry no user signal.

_TECH_FAILURE_PREFIX = "Tool permission request failed"
_APPROVED_PREFIX = "User has approved"
_APPROVED_EDITED_MARKER = "## Approved Plan (edited by user):"
_STAY_PREFIX = "User chose to stay in plan mode"
_STAY_COMMENTS_MARKER = "Comments on the plan:"
_REJECT_PREFIX = "The user doesn't want to proceed"
_USER_SAID_MARKER = "the user said:"
_SELECTED_MARKER = "On selected text:"
_RE_PAIR_RE = re.compile(r'\[Re: "(.*?)"\]', re.S)


@dataclass(frozen=True)
class _PlanResponse:
    """One user response to a plan revision (internal, pre-normalization).

    ``verdict`` ∈ ``rejected`` | ``stay_in_plan_mode`` | ``approved``.
    ``pairs`` are the extracted (quote, comment) tuples — quote is ``None``
    for a free-text comment that selected nothing.  ``edited_body`` is the
    authoritative user-edited plan text carried by an approval, when present.
    ``raw`` keeps the full tool_result content for on-demand ``get_body``.
    """

    tool_use_id: str
    verdict: str
    raw: str
    pairs: Tuple[Tuple[Optional[str], str], ...] = ()
    edited_body: Optional[str] = None
    message_index: int = -1
    ts: Optional[str] = None


def _parse_selected_pairs(
    text: str,
) -> List[Tuple[Optional[str], str]]:
    """Split an "On selected text:" rejection tail into (quote, comment) pairs.

    Free text before the first block becomes a ``(None, comment)`` pair.
    Within a block, leading ``> ``-prefixed lines are the quote (markup the
    UI rendered, stripped of the quote prefix); the rest is the comment.
    """
    pairs: List[Tuple[Optional[str], str]] = []
    chunks = text.split(_SELECTED_MARKER)
    preamble = chunks[0].strip()
    if preamble:
        pairs.append((None, preamble))
    for chunk in chunks[1:]:
        quote_lines: List[str] = []
        comment_lines: List[str] = []
        in_quote = True
        for line in chunk.lstrip("\n").splitlines():
            if in_quote and line.startswith(">"):
                quote_lines.append(
                    line[2:] if line.startswith("> ") else line[1:]
                )
            else:
                in_quote = False
                comment_lines.append(line)
        quote = "\n".join(quote_lines).strip()
        comment = "\n".join(comment_lines).strip()
        if quote or comment:
            pairs.append((quote or None, comment))
    return pairs


def _parse_re_pairs(text: str) -> List[Tuple[Optional[str], str]]:
    """Split a stay-in-plan-mode comments tail into (quote, comment) pairs.

    The format is ``[Re: "<quote>"] <comment>`` per entry; a comment runs
    until the next ``[Re:`` marker (multi-line comments are common).  Free
    text before the first marker becomes a ``(None, comment)`` pair.
    """
    pairs: List[Tuple[Optional[str], str]] = []
    matches = list(_RE_PAIR_RE.finditer(text))
    preamble = (text[: matches[0].start()] if matches else text).strip()
    if preamble:
        pairs.append((None, preamble))
    for k, m in enumerate(matches):
        end = matches[k + 1].start() if k + 1 < len(matches) else len(text)
        quote = m.group(1).strip()
        comment = text[m.end():end].strip()
        if quote or comment:
            pairs.append((quote or None, comment))
    return pairs


def _classify_plan_response(
    content: str,
) -> Optional[Tuple[str, Tuple[Tuple[Optional[str], str], ...], Optional[str]]]:
    """Classify one plan tool_result into ``(verdict, pairs, edited_body)``.

    Returns ``None`` for technical failures, bare no-text rejections and any
    unrecognised format — filtered, never guessed.
    """
    text = content or ""
    if not text.strip() or text.startswith(_TECH_FAILURE_PREFIX):
        return None
    if text.startswith(_APPROVED_PREFIX):
        edited: Optional[str] = None
        if _APPROVED_EDITED_MARKER in text:
            edited = text.split(_APPROVED_EDITED_MARKER, 1)[1].lstrip("\n")
        return ("approved", (), edited or None)
    if text.startswith(_STAY_PREFIX):
        if _STAY_COMMENTS_MARKER in text:
            tail = text.split(_STAY_COMMENTS_MARKER, 1)[1]
        else:
            tail = "\n".join(text.splitlines()[1:])
        pairs = tuple(_parse_re_pairs(tail))
        return ("stay_in_plan_mode", pairs, None) if pairs else None
    if text.startswith(_REJECT_PREFIX) or _SELECTED_MARKER in text:
        if _USER_SAID_MARKER in text:
            tail = text.split(_USER_SAID_MARKER, 1)[1]
        elif _SELECTED_MARKER in text:
            tail = text[text.index(_SELECTED_MARKER):]
        else:
            return None  # bare rejection — no user words, no signal
        pairs = tuple(_parse_selected_pairs(tail))
        return ("rejected", pairs, None) if pairs else None
    return None


def _plan_responses_for_session(
    messages: Sequence[Any],
    *,
    agent: str,
) -> List[_PlanResponse]:
    """Return the ordered user responses to plan revisions in one session.

    Only agents with an interactive plan-approval flow produce responses —
    today that is Claude.  Every other agent returns an empty list (honest
    absence, never fabricated).  Order is message order; the list index is
    the stable ``pf<N>`` ordinal that ``get_body`` resolves.

    The correlated call ids come from the plan-signal SSOT
    (:func:`_plan_signals_for_session`), so BOTH Claude plan signals are
    covered: an ``ExitPlanMode`` call and a ``Write plans/*.md`` call (v2 —
    a rejected plan-file Write with user words is plan feedback too; its
    result uses the same permission-denial format).  A successful Write's
    "File created…" result matches no recognised format and is filtered.
    """
    if agent != "claude":
        return []
    plan_call_ids = {
        sig.tool_use_id
        for sig in _plan_signals_for_session(
            messages, agent=agent, session_path=""
        )
        if sig.tool_use_id
    }
    if not plan_call_ids:
        return []
    responses: List[_PlanResponse] = []
    for idx, msg in enumerate(messages):
        msg_ts = to_utc_aware(getattr(msg, "timestamp", None))
        ts_iso = iso(msg_ts) if msg_ts is not None else None
        for tr in getattr(msg, "tool_result", ()) or ():
            if not isinstance(tr, dict):
                continue
            tid = tr.get("tool_use_id")
            if not (isinstance(tid, str) and tid in plan_call_ids):
                continue
            raw = tr.get("content")
            raw = raw if isinstance(raw, str) else str(raw or "")
            classified = _classify_plan_response(raw)
            if classified is None:
                continue
            verdict, pairs, edited = classified
            responses.append(_PlanResponse(
                tool_use_id=tid,
                verdict=verdict,
                raw=raw,
                pairs=pairs,
                edited_body=edited,
                message_index=idx,
                ts=ts_iso,
            ))
    return responses


# --- quote → plan-section anchoring (F3.4 v2) ------------------------------
# The user selects quotes from the RENDERED plan (the UI strips markdown
# markup before display), so anchoring a quote back to a section of the raw
# markdown source must compare both sides through the same markup-stripping
# normalization (audited on real vaults: with stripping the section match is
# ~99%).  A quote that matches no section — or more than one — gets an honest
# ``None`` anchor, never a nearest guess.

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# Leading rendered-away line prefixes: heading hashes, blockquote markers,
# bullet/numbered list markers (possibly nested, hence the ``+``).
_MD_LINE_PREFIX_RE = re.compile(
    r"^(?:\s*(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+))+"
)
_MD_CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s+")
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _normalize_rendered_text(text: str) -> str:
    """Normalize markdown source and rendered selections to one comparand.

    Strips the markup a terminal render removes (heading hashes, list and
    blockquote markers, checkboxes, emphasis asterisks, backticks, link
    targets) and collapses ALL whitespace to single spaces, so a quote
    selected from the rendered plan matches its markdown source.  Applied
    symmetrically to both sides, so a literal ``*``/`` ` `` inside code is
    dropped from quote and section alike and still matches.
    """
    lines: List[str] = []
    for line in text.splitlines():
        s = _MD_LINE_PREFIX_RE.sub("", line.strip())
        s = _MD_CHECKBOX_RE.sub("", s)
        lines.append(s)
    joined = "\n".join(lines)
    joined = _MD_LINK_RE.sub(r"\1", joined)
    joined = joined.replace("`", "").replace("*", "")
    return _WS_RE.sub(" ", joined).strip()


def _plan_body_sections(body: str) -> List[Tuple[Optional[str], str]]:
    """Split a markdown plan body into ``(heading, section_text)`` chunks.

    Flat split on heading lines (any level); the heading line itself belongs
    to its section, so a quote of the heading anchors there.  Text before
    the first heading forms a headingless ``(None, …)`` preamble.  Fenced
    code blocks are opaque — a ``# comment`` inside a fence never starts a
    section.
    """
    sections: List[Tuple[Optional[str], str]] = []
    heading: Optional[str] = None
    buf: List[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        match = None if in_fence else _HEADING_LINE_RE.match(line)
        if match:
            if buf or heading is not None:
                sections.append((heading, "\n".join(buf)))
            heading = match.group(2).strip()
            buf = [line]
        else:
            buf.append(line)
    sections.append((heading, "\n".join(buf)))
    return sections


def _anchor_quote_to_section(
    quote: Optional[str], body: Optional[str]
) -> Optional[str]:
    """Anchor a rendered plan quote to ONE section heading of the source.

    Returns the heading text of the single section whose normalized text
    contains the normalized quote.  Honest ``None`` when the quote is empty,
    the body is unknown, the quote matches NO section (a miss stays a miss —
    never the nearest guess) or matches MORE than one (ambiguity is not
    resolved by picking one).
    """
    if not quote or not isinstance(body, str) or not body:
        return None
    needle = _normalize_rendered_text(quote)
    if not needle:
        return None
    hits = [
        heading
        for heading, section_text in _plan_body_sections(body)
        if needle in _normalize_rendered_text(section_text)
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def _input_body_text(payload: Any) -> Optional[str]:
    """Serialize a (coerced) tool input into its full searchable body text.

    A string payload is the body itself (a raw shell command); a dict/list
    is JSON-serialized (so a ``text`` facet can match a pattern that lives
    inside a multi-line ``command`` field or any other nested value).  This
    is what the :class:`~ai_r.events._common.Event` ``body`` carries so the
    ``text`` facet reaches INSIDE a tool call whose ``text`` is only the raw
    tool name.  ``None`` when there is nothing (empty / unserializable).
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload or None
    try:
        return json.dumps(payload, ensure_ascii=False) or None
    except (TypeError, ValueError):
        return str(payload) or None


def _messages_to_events(
    messages: Sequence[Any],
    *,
    session_id: str,
    agent: str,
    session_ts: Optional[datetime],
    session_path: str = "",
) -> List[Event]:
    """Normalize one session's messages into an ordered Event list.

    A ``user`` message → one ``user_turn``.  An ``assistant`` message →
    one ``assistant_turn`` (when it has text) followed by one
    ``tool_call(<sub>)`` per ``tool_use`` entry.  ``tool`` role records
    carry no turn text of their own and are skipped as standalone events,
    but their ``tool_result`` entries are correlated back to the owning
    ``tool_call`` (by ``tool_use_id``) so the call event carries an
    ``is_error`` ref — the success/error outcome is thus visible on the
    existing ``tool_call`` events WITHOUT introducing a new event type
    (so ``type`` filters and event counts are unchanged).

    Every emitted event inherits the hosting message's ``model`` (a
    ``tool_call``/``plan_event`` carries the model of the assistant turn
    that produced it); a file-based plan signal (Antigravity) has no
    hosting message → ``model=None``, like any absent signal.
    """
    events: List[Event] = []
    seq = 0
    session_iso = iso(session_ts) if session_ts is not None else None

    # Correlate tool_result outcomes back to their calls by ``tool_use_id``.
    # Only ids that appear on a result are recorded; a call whose id is not
    # in this map simply carries no outcome (unknown / no error signal —
    # e.g. Codex/Pi/Antigravity, which expose no per-result flag).
    error_by_tool_id: dict[str, bool] = {}
    for _m in messages:
        for _tr in getattr(_m, "tool_result", ()) or ():
            if not isinstance(_tr, dict):
                continue
            _tid = _tr.get("tool_use_id")
            if isinstance(_tid, str) and _tid:
                error_by_tool_id[_tid] = bool(_tr.get("is_error"))

    # Detect plan signals once; index the message-level ones by their
    # triggering message so each ``plan_event`` is emitted inline (right
    # after the tool_call that produced it), keeping the stream chronological.
    plan_signals = _plan_signals_for_session(
        messages, agent=agent, session_path=session_path
    )
    signals_by_msg: dict[int, List[_PlanSignal]] = {}
    file_signals: List[_PlanSignal] = []
    for sig in plan_signals:
        if sig.message_index >= 0:
            signals_by_msg.setdefault(sig.message_index, []).append(sig)
        else:
            file_signals.append(sig)

    def _plan_refs(sig: _PlanSignal) -> List[dict]:
        refs: List[dict] = [{"title": sig.title}, {"agent_signal": sig.agent_signal}]
        if sig.path:
            refs.append({"path": sig.path})
        # ``task_key`` is the stable grouping key (plan-file slug when the
        # agent has one, normalized title otherwise) — carried in refs so
        # ``_assign_plan_kinds`` groups without re-deriving it.
        if sig.task_key:
            refs.append({"task_key": sig.task_key})
        return refs

    for idx, msg in enumerate(messages):
        role = getattr(msg, "role", None)
        text = getattr(msg, "text", "") or ""
        msg_ts = to_utc_aware(getattr(msg, "timestamp", None))
        ts_iso = iso(msg_ts) if msg_ts is not None else session_iso
        # Model dimension: every event inherits the producing message's
        # ``model`` (None where the format has no signal — never guessed).
        msg_model = getattr(msg, "model", None)
        msg_model = msg_model if isinstance(msg_model, str) and msg_model else None

        if role == "user":
            if isinstance(text, str) and text.strip():
                # User-attached references (Q1): structured parts the parser
                # captured (``msg.user_refs``) + links/paths/IDE tags pulled
                # from the prose, de-duplicated (structured beats text).  ai-r
                # only MARKS the external source here — never fetches it.
                raw_user_refs = list(getattr(msg, "user_refs", ()) or ())
                raw_user_refs.extend(extract_user_refs_from_text(text))
                user_refs = tuple(
                    {"user_ref": r} for r in dedup_user_refs(raw_user_refs)
                )
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=ts_iso,
                    event_type="user_turn", text=text, refs=user_refs,
                    message_index=idx, model=msg_model,
                ))
                seq += 1
            continue

        if role == "assistant":
            if isinstance(text, str) and text.strip():
                # ``has_thinking`` is a discovery hint (Q2): the reasoning text
                # stays out of the default output/search; a consumer opts in
                # via ``include_thinking`` only when it actually needs it.
                had_thinking = bool((getattr(msg, "thinking", "") or "").strip())
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=ts_iso,
                    event_type="assistant_turn", text=text, refs=(),
                    message_index=idx, model=msg_model,
                    has_thinking=had_thinking,
                ))
                seq += 1
            for tool in getattr(msg, "tool_use", ()) or ():
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name", "")
                if not isinstance(name, str) or not name:
                    continue
                sub = classify_tool(name)
                tool_ts = to_utc_aware(tool.get("timestamp"))
                tool_iso = iso(tool_ts) if tool_ts is not None else ts_iso
                payload = _coerce_tool_input(tool.get("input", ""))
                refs: List[dict] = [{"tool": name}]
                fpath = _path_from_payload(payload)
                if fpath:
                    refs.append({"file": fpath})
                # F3.1: classify the call (wrapper-aware) and surface the
                # real name under a Skill/Task/MCP wrapper.  ``tool_kind``
                # is always present; ``tool_resolved`` only when the input
                # actually carries the real name (honest — never guessed).
                # The event ``type`` keeps the classify_tool subtype for
                # backward-compat (a Task call stays ``tool_call(other)``).
                kind, resolved = resolve_tool(name, payload)
                refs.append({"tool_kind": kind})
                if resolved:
                    refs.append({"tool_resolved": resolved})
                # Surface the call's outcome when a correlated result exists:
                # ``{"is_error": True|False}``.  Absent when no matching
                # result id was seen (outcome unknown / agent has no signal).
                tu_id = tool.get("tool_use_id")
                if isinstance(tu_id, str) and tu_id in error_by_tool_id:
                    refs.append({"is_error": error_by_tool_id[tu_id]})
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=tool_iso,
                    event_type=f"tool_call({sub})", text=name, refs=refs,
                    message_index=idx, model=msg_model,
                    # Full input body so the ``text`` facet reaches inside a
                    # multi-line command (the name-only ``text`` cannot).
                    body=_input_body_text(payload),
                ))
                seq += 1
            # Emit any plan_event(s) triggered by this assistant message,
            # right after its tool_call(s) so the stream stays chronological.
            for sig in signals_by_msg.get(idx, ()):
                events.append(_mk_event(
                    session_id=session_id, agent=agent, seq=seq, ts=ts_iso,
                    event_type="plan_event",
                    text=sig.title,
                    refs=_plan_refs(sig),
                    message_index=idx, model=msg_model,
                ))
                seq += 1
            continue
        # ``tool`` role and anything else: not a first-class Phase-1 event.

    # File-based plan signals (Antigravity's ``implementation_plan.md``) have
    # no hosting message — append them once at the end of the stream.
    for sig in file_signals:
        events.append(_mk_event(
            session_id=session_id, agent=agent, seq=seq, ts=session_iso,
            event_type="plan_event",
            text=sig.title,
            refs=_plan_refs(sig),
            message_index=sig.message_index,
        ))
        seq += 1
    return events


def normalize_session_filter(
    session: Optional[Any],
) -> Optional[frozenset]:
    """Normalize a ``session`` facet value into a uuid set, or ``None``.

    F3.2: the ``session`` facet accepts a single uuid string OR a list of
    uuid strings (an explicit session batch — e.g. the ids picked from a
    ``search_sessions`` result).  Returns ``None`` for "no filter", else a
    ``frozenset`` of uuids to keep.

    Fail-loud, never a silent surprise:

    * an empty list raises :class:`ValueError` — ``[]`` is ambiguous
      ("no filter" vs "match nothing"), the caller must omit the facet
      to scan everything;
    * a non-string item (or a blank/empty string item) raises
      :class:`ValueError`;
    * any other type (int/dict/...) raises :class:`ValueError`.

    A bare string is passed through as a one-element set unchanged —
    including the (pre-existing) degenerate ``""``, which keeps the
    historical "matches nothing" behaviour for backward compatibility.
    """
    if session is None:
        return None
    if isinstance(session, str):
        return frozenset((session,))
    if isinstance(session, (list, tuple, set, frozenset)):
        items = list(session)
        if not items:
            raise ValueError(
                "session list must not be empty — omit the session "
                "facet to scan all sessions"
            )
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    "session list items must be non-empty session-uuid "
                    f"strings, got {item!r}"
                )
        return frozenset(items)
    raise ValueError(
        "session must be a session-uuid string or a list of them, "
        f"got {session!r}"
    )


def _descendant_uuids(sessions: Sequence[Any], root_uuid: str) -> "frozenset[str]":
    """All transitive descendants of ``root_uuid`` in a session's parent tree.

    Builds the ``uuid -> parent_uuid`` map from ``sessions`` (one agent's
    ``list_sessions()`` — ``parent_uuid`` only ever references a session of the
    same agent) and returns every uuid whose ``parent_uuid`` chain reaches
    ``root_uuid``: the ``parent`` facet's subtree.  ``root_uuid`` itself is NOT
    included (its own events are addressable via ``session=<root_uuid>``).

    Cycle-safe: a malformed parent cycle (a should-never-happen in real data)
    can't wedge the walk — each uuid is visited at most once.  An unknown
    ``root_uuid`` (absent from the corpus, or a leaf with no children) yields
    an empty set — an honest empty result, never an error.
    """
    children: dict[str, list[str]] = {}
    for sess in sessions:
        parent = getattr(sess, "parent_uuid", None)
        if parent:
            children.setdefault(parent, []).append(sess.uuid)
    descendants: set[str] = set()
    frontier = list(children.get(root_uuid, ()))
    while frontier:
        uuid = frontier.pop()
        if uuid in descendants or uuid == root_uuid:
            continue
        descendants.add(uuid)
        frontier.extend(children.get(uuid, ()))
    return frozenset(descendants)


def iter_events(
    agent: Optional[str] = None,
    *,
    session: Optional[Any] = None,
    noise: str = "include",
    project_dir: Optional[str] = None,
    parent: Optional[str] = None,
    scanned_sessions_out: Optional[dict[str, Any]] = None,
) -> Iterable[Event]:
    """Yield the normalized Event stream across sessions, cross-agent.

    Args:
        agent: Optional agent filter (``claude``/``codex``/...); ``None``
            = every agent.
        session: Optional session-uuid filter — a single uuid string or a
            list of uuid strings (F3.2); restrict the scan to those
            sessions (cheap fast-path for ``relative_to`` walks and
            explicit session batches).  Validation SSOT:
            :func:`normalize_session_filter` (fail-loud on an empty list
            or non-string items).
        noise: Session-level noise filter (see
            :mod:`ai_r.parsers._noise`): ``"include"`` (default, no
            filtering), ``"exclude"`` (drop subagent sessions), ``"only"``
            (keep only subagent sessions).  Applied *before* reading
            messages, so excluded sessions cost nothing.
        project_dir: Session-level project filter — keep only sessions
            whose ``Session.project_dir`` equals this path or is a
            descendant of it (path-boundary aware, see
            :func:`ai_r.parsers._common.project_dir_matches`); sessions
            without a ``project_dir`` signal never match.  Like ``noise``,
            applied *before* any message is read.
        parent: Session-level subtree filter — keep only sessions that are a
            **descendant** (transitively, any depth) of this session uuid in
            the ``parent_uuid`` tree: every spawned subagent below ``parent``,
            direct children plus nested.  ``parent`` itself is excluded (its
            own events are reachable via ``session=<parent>``).  The closure
            is built per-agent from ``list_sessions()`` (``parent_uuid`` only
            references a same-agent session).  An unknown uuid matches nothing
            (honest empty result).  Like ``noise``, applied *before* any
            message is read.
        scanned_sessions_out: Optional out-parameter — a dict the caller
            owns, filled with ``{agent_label: list_sessions() result}`` as
            each agent is scanned.  Lets the caller reuse the enumeration
            (e.g. for empty-result diagnostics) instead of paying for a
            second full corpus walk.  Complete only once the generator is
            exhausted.

    Yields:
        :class:`Event` records in per-session, chronological (parse)
        order.  Sessions that fail to read are skipped (an audit tool
        prefers a partial view to a crash), mirroring ``find_file_edits``.
    """
    validate_noise(noise)
    wanted_sessions = normalize_session_filter(session)
    for agent_name in target_agents(agent):
        parser = PARSERS[agent_name]
        agent_lc = agent_name.value.lower()
        sessions = parser.list_sessions()
        if scanned_sessions_out is not None:
            scanned_sessions_out[agent_lc] = sessions
        # ``parent`` subtree closure is per-agent (parent_uuid never crosses
        # agents), computed once from this agent's session list.
        parent_subtree = (
            _descendant_uuids(sessions, parent) if parent is not None else None
        )
        for sess in sessions:
            if wanted_sessions is not None and sess.uuid not in wanted_sessions:
                continue
            if parent_subtree is not None and sess.uuid not in parent_subtree:
                continue
            if not noise_allows(sess, noise):
                continue
            if project_dir is not None and not project_dir_matches(
                getattr(sess, "project_dir", None), project_dir
            ):
                continue
            try:
                messages = parser.read_messages(sess.uuid)
            except (FileNotFoundError, ValueError, OSError):
                continue
            session_ts = to_utc_aware(sess.date)
            yield from _messages_to_events(
                messages,
                session_id=sess.uuid,
                agent=agent_lc,
                session_ts=session_ts,
                session_path=getattr(sess, "path", "") or "",
            )


# ``Message`` is re-exported so downstream modules that build the enrichment
# message cache can import the parser type from here alongside the stream.
__all__ = ["Event", "Message", "iter_events", "normalize_session_filter"]
