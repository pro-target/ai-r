"""The ``incidents`` preset (F4.1) — dangerous command + regret reaction.

Answers "where did an agent run something destructive — and did it then
apologise?" in one call.  An *incident candidate* is a shell (``bash``-kind)
tool call whose command matches the danger dictionary; a candidate becomes
**confirmed** when a regret/apology marker fires in the next few messages
(the two-step check: dangerous command → observed regret reaction).

This is a preset over the existing core, NOT a second engine (project
preset rule):

1. **Step 1 — candidates** come from ONE :func:`ai_r.events.query` scan
   (``type="tool_call"``, ``tool_kind="bash"``) — session iteration,
   agent/session/date/noise/project_dir facets and event ids are all the
   query core's, nothing is re-implemented here.
2. **Deterministic selection** — the danger dictionary
   (:data:`DANGER_PATTERNS`) is applied to each candidate's raw command;
   the reaction window is scanned with the regret dictionary
   (:data:`REGRET_MARKERS`).  Zero LLM, zero guessing: no dictionary hit →
   no incident; no reaction hit → ``confirmed: false`` (honest, never
   inferred).
3. **Token budget** — emitted commands / reaction previews are char-capped
   fragments centred on the match, ``limit`` bounds the record count, and
   full context stays on-demand (the record's ``id`` is a query event id:
   walk neighbours via ``query(relative_to=...)`` or read the session).

Dictionary provenance: the danger patterns were harvested from public
agent-guardrail rule sets (``_docs/reference-6c18b957/``: git/database/
cloud/kubernetes/terraform guard hooks, pi-dangerous rules, safe-rm) and
distilled into the audit script ``audit_danger_patterns.py``; the regret
dictionary is the same script's bilingual (ru + en) APOLOGY set, restructured
into labeled markers (mirrors :mod:`ai_r.outcome`).  Both were calibrated on
this host's real history (2026-07-04, full cross-agent corpus: 297
candidates / 4 confirmed; claude 129, opencode 161, codex 7):

* matching runs against the extracted *command text* (not the whole
  serialized input), so a Bash ``description`` field that merely
  *describes* a dangerous command cannot fire on its own;
* ``--force-with-lease`` is explicitly NOT force-push (safe variant);
* ``db.truncate`` was tightened after calibration: the reference pattern
  fired on English prose ("Truncate the log so...") — it now requires the
  ``TABLE`` keyword or a semicolon-terminated identifier;
* the residual, accepted false-positive class: a command that *quotes* a
  dangerous string without running it (``echo``-ed reports, ``grep`` of
  rule files, SQL-injection *test* payloads).  A dictionary cannot tell
  mention from execution — documented trade-off, not hidden.

Honesty rules (same as the rest of the package): all agents are equal —
any parser that surfaces shell calls participates; a format without a
per-result error flag keeps ``is_error: null``; a command matched by
pattern can still be a false positive (e.g. ``echo "rm -rf /"``) — the
patterns are a deterministic dictionary, not an interpreter, and that
trade-off is documented rather than hidden.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ai_r.events._common import classify_tool
from ai_r.events.query import query as _query
from ai_r.parsers import PARSERS, Message, target_agents
from ai_r.redact import merge_redaction_counts, redact_text
from ai_r.security import coerce_tool_input as _coerce_input

__all__ = [
    "CONFIRMED_MODES",
    "DANGER_CATEGORIES",
    "DANGER_PATTERNS",
    "REGRET_MARKERS",
    "incidents",
    "match_danger",
    "match_regret",
]


# --- danger dictionary ------------------------------------------------------
# ``(pattern_id, compiled regex)`` pairs.  Ids are ``<category>.<name>`` —
# the category prefix is what the ``category`` filter validates against.
# Seeded from the web-harvested guardrail reference and kept verbatim from
# the calibration audit script (see module docstring for provenance).
DANGER_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("fs.rm_rf_any", re.compile(
        r"\brm\s+-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*\b")),
    ("fs.rm_rf_danger_path", re.compile(
        r"\brm\s+-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*\s+"
        r"(?:~|\\?\$HOME|/home\b|/etc\b|/usr\b|/var\b|\*)")),
    ("fs.rm_dotgit", re.compile(r"\brm\s+-[a-zA-Z]+\s+[^|;&]*\.git\b")),
    ("fs.dd_dev", re.compile(r"\bdd\s+[^|;&]*of=/dev/")),
    ("fs.mkfs", re.compile(r"\bmkfs\b")),
    ("fs.chmod_777", re.compile(r"chmod\s+(?:-R\s+)?777")),
    ("fs.find_delete", re.compile(r"\bfind\s+[^|;&]*-delete\b")),
    ("net.curl_pipe_sh", re.compile(
        r"(?:curl|wget)[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")),
    ("git.push_force", re.compile(
        r"git\s+push\s+[^|;&\n]*(?:--force\b(?!-with-lease)|\s-f\b)")),
    ("git.reset_hard", re.compile(r"git\s+reset\s+--hard")),
    ("git.clean_f", re.compile(r"git\s+clean\s+-[a-zA-Z]*f")),
    ("git.checkout_discard", re.compile(
        r"git\s+checkout\s+(?:--\s|\.(?:\s|$))")),
    ("git.branch_D", re.compile(r"git\s+branch\s+-D\b")),
    ("git.stash_drop", re.compile(r"git\s+stash\s+(?:drop|clear)")),
    ("git.filter_history", re.compile(r"git\s+filter-(?:branch|repo)")),
    ("git.reflog_expire", re.compile(r"git\s+reflog\s+expire")),
    ("db.drop", re.compile(r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b", re.I)),
    # Calibration 2026-07-04: the reference's ``TRUNCATE\s+(TABLE\s+)?\w``
    # fired on English prose ("Truncate the log so..."); require the TABLE
    # keyword or a semicolon-terminated identifier (SQL-statement shape).
    ("db.truncate", re.compile(
        r"\bTRUNCATE\s+TABLE\s+\w|\bTRUNCATE\s+\w+\s*;", re.I)),
    ("db.delete_nowhere", re.compile(
        r"\bDELETE\s+FROM\s+`?\w+`?\s*(?:;|\\n|$)", re.I)),
)

# The category vocabulary — the ``<category>.`` prefixes of the dictionary.
DANGER_CATEGORIES: frozenset[str] = frozenset(
    pid.split(".", 1)[0] for pid, _ in DANGER_PATTERNS
)

# --- regret dictionary ------------------------------------------------------
# Labeled bilingual (ru + en) apology/regret markers, matched against the
# messages FOLLOWING a dangerous call.  Labels (not raw session text) are
# what the result carries — same output-hygiene rule as ai_r.outcome.


def _marker(label: str, pattern: str) -> Tuple[str, "re.Pattern[str]"]:
    return label, re.compile(pattern, re.IGNORECASE)


REGRET_MARKERS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # -- Russian --
    _marker("извинение", r"извин|прошу прощ|виноват"),
    _marker("моя ошибка", r"моя ошибка|я ошиб|критическая ошибка"),
    _marker(
        "случайно удалил",
        r"случайно (?:удал|стёр|стер|затер|перезапис)"
        r"|зря удалил|не следовало",
    ),
    _marker("откат/восстановление", r"откат(?:ываю|ил)|восстанавлива"),
    _marker("запорол", r"запорол|испортил|накосячил"),
    # -- English --
    _marker("apology", r"apolog|\bsorry\b"),
    _marker("my mistake", r"my mistake|my fault|mistakenly"),
    _marker(
        "accidentally deleted",
        r"accidentally (?:deleted|removed|overwrote|wiped)"
        r"|i (?:deleted|removed|wiped) .{0,40}by mistake",
    ),
    _marker("oops", r"\boops\b"),
    _marker(
        "shouldn't have",
        r"shouldn'?t have (?:deleted|run|done)",
    ),
)

# --- knobs -------------------------------------------------------------------

# ``confirmed`` filter vocabulary (two-step check as a parameter):
# ``include`` = candidates + confirmed (default), ``only`` = confirmed only,
# ``exclude`` = unconfirmed candidates only.
CONFIRMED_MODES: frozenset[str] = frozenset({"include", "only", "exclude"})

# How many messages AFTER the call the regret scan covers by default —
# the window the calibration audit used (next 6 messages).
_DEFAULT_REACTION_WINDOW = 6

# Emitted-fragment caps (chars) — the preset's token budget.  Full context
# stays on-demand via the event id.
_COMMAND_CHARS_CAP = 500      # command fragment, centred on the first hit
_REACTION_CHARS_CAP = 240     # reaction fragment around the marker hit
_COMMAND_HEAD_CONTEXT = 100   # chars kept before the first danger hit
_REACTION_HEAD_CONTEXT = 80   # chars kept before the regret hit

_DEFAULT_LIMIT = 50

# Input keys that carry the actual shell command in a parsed tool input,
# by preference (Claude ``command``, codex exec variants, generic fallbacks).
_COMMAND_KEYS = ("command", "cmd", "script", "code")


# --- dictionary matching (exposed for tests + calibration reuse) ------------


def match_danger(command: str) -> List[str]:
    """Ids of every danger pattern that fires on ``command`` (dict order)."""
    if not isinstance(command, str) or not command:
        return []
    return [pid for pid, rx in DANGER_PATTERNS if rx.search(command)]


def match_regret(text: str) -> List[str]:
    """Labels of every regret marker that fires on ``text`` (dict order)."""
    if not isinstance(text, str) or not text:
        return []
    return [label for label, rx in REGRET_MARKERS if rx.search(text)]


# --- helpers -----------------------------------------------------------------


def _command_text(payload: Any) -> str:
    """Extract the shell command string from a (coerced) tool input.

    Prefers an explicit command key (``command``/``cmd``/``script``/
    ``code``) — string taken as-is, a list of strings joined with spaces
    (the codex exec shape) — so a Bash ``description`` field that merely
    *describes* a dangerous command cannot fire the dictionary.  A string
    payload is the command itself; anything else falls back to its JSON
    serialization (honest catch-all: better a rare description-shaped
    false positive than a silently invisible command).
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in _COMMAND_KEYS:
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, (list, tuple)) and val and all(
                isinstance(item, str) for item in val
            ):
                return " ".join(val)
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


def _first_hit_span(command: str, hits: Sequence[str]) -> int:
    """Start offset of the earliest danger-pattern match in ``command``."""
    starts = []
    hit_set = set(hits)
    for pid, rx in DANGER_PATTERNS:
        if pid not in hit_set:
            continue
        m = rx.search(command)
        if m:
            starts.append(m.start())
    return min(starts) if starts else 0


def _fragment(
    text: str, anchor: int, cap: int, head_context: int
) -> Tuple[str, bool]:
    """A ``cap``-char fragment of ``text`` around ``anchor``.

    Keeps up to ``head_context`` chars before the anchor and fills the rest
    of the budget forward.  Returns ``(fragment, truncated)``; a cut edge
    is marked with ``…``.
    """
    if len(text) <= cap:
        return text, False
    start = max(0, anchor - head_context)
    end = min(len(text), start + cap)
    frag = text[start:end]
    if start > 0:
        frag = "…" + frag
    if end < len(text):
        frag = frag + "…"
    return frag, True


def _ref_value(refs: Sequence[dict], key: str) -> Optional[Any]:
    for r in refs or ():
        if isinstance(r, dict) and key in r:
            return r[key]
    return None


def _scan_reaction(
    messages: Sequence[Any], call_index: int, window: int
) -> Optional[dict[str, Any]]:
    """Scan the ``window`` messages after ``call_index`` for regret markers.

    Returns the FIRST message (any role — the agent apologises, the user
    scolds) where a marker fires: ``{message_index, offset, role, markers,
    preview}``.  ``None`` when nothing fires in the window — the incident
    stays an unconfirmed candidate, never guessed into a confirmation.
    """
    upper = min(call_index + window, len(messages) - 1)
    for j in range(call_index + 1, upper + 1):
        msg = messages[j]
        text = getattr(msg, "text", "") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        labels = match_regret(text)
        if not labels:
            continue
        # Preview anchored on the first firing marker's match position.
        anchor = len(text)
        for label, rx in REGRET_MARKERS:
            if label not in labels:
                continue
            m = rx.search(text)
            if m:
                anchor = min(anchor, m.start())
        preview, _ = _fragment(
            text.replace("\n", " "),
            anchor,
            _REACTION_CHARS_CAP,
            _REACTION_HEAD_CONTEXT,
        )
        return {
            "message_index": j,
            "offset": j - call_index,
            "role": getattr(msg, "role", None),
            "markers": labels,
            "preview": preview,
        }
    return None


def _bash_entries(msg: Any) -> List[dict]:
    """The message's ``bash``-kind tool_use entries, in stream order.

    Mirrors the event-construction filter of
    :func:`ai_r.events.model._messages_to_events` exactly (dict entries
    with a non-empty string name), so the k-th entry here corresponds to
    the k-th ``tool_kind="bash"`` event of the same ``message_index``.
    """
    out: List[dict] = []
    for tool in getattr(msg, "tool_use", ()) or ():
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "")
        if not isinstance(name, str) or not name:
            continue
        if classify_tool(name) == "bash":
            out.append(tool)
    return out


# --- the preset ---------------------------------------------------------------


def incidents(
    *,
    agent: Optional[str] = None,
    session: Optional[Any] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    category: Optional[str] = None,
    confirmed: str = "include",
    reaction_window: int = _DEFAULT_REACTION_WINDOW,
    limit: int = _DEFAULT_LIMIT,
    noise: str = "include",
    project_dir: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Find dangerous shell commands and their regret reactions (F4.1).

    The baked chain (see module docstring): ONE ``query`` scan for
    ``bash``-kind tool calls → danger-dictionary selection on each call's
    raw command → regret-dictionary scan over the following
    ``reaction_window`` messages → the two-step ``confirmed`` verdict.

    Args:
        agent: Optional agent filter (``claude``/``codex``/...); ``None``
            = all agents (every parser that surfaces shell calls
            participates).
        session: Optional session scope — a single uuid or a list of uuids
            (same semantics/validation as the ``query`` facet).
        since / until: ISO-8601 bounds (inclusive) on the call timestamp.
        category: Optional danger-category filter — one of
            :data:`DANGER_CATEGORIES` (``fs``/``git``/``db``/``net``).
            Keeps only incidents with at least one hit in that category.
            Unknown values fail loud.
        confirmed: Two-step-check filter — ``"include"`` (default: both
            confirmed and unconfirmed candidates), ``"only"`` (confirmed
            only), ``"exclude"`` (unconfirmed candidates only).
        reaction_window: How many messages after the call the regret scan
            covers (default ``6``, the calibration window).  ``0`` disables
            the reaction step: every incident stays ``confirmed: false``.
        limit: Max incident records returned (``0`` = no cap, default
            ``50``).  ``count``/``confirmed_count``/``by_pattern`` always
            reflect the FULL match set.
        noise / project_dir: Session-level filters, forwarded verbatim to
            the ``query`` scan (subagent noise, project scoping).
        redact: ``True`` (default) masks secrets in the emitted
            ``session_title``/``command``/``reaction.preview`` fields as
            ``[REDACTED_<TYPE>]`` and adds a ``redactions`` type→count
            dict when anything was masked; ``False`` returns raw.
            Dictionary matching always runs on the RAW stored command.
            The command window is cut AFTER redacting the full command
            (same order as ``query``), so a secret sliced by the window
            edge can never leak partially.

    Returns:
        A dict::

            {
              "incidents": [
                {
                  "id": "<session>:<seq>",     # query event id (on-demand
                                               # context via relative_to)
                  "agent", "session_id", "session_title", "ts",
                  "message_index": int,
                  "tool": "<raw tool name>",
                  "patterns": ["git.reset_hard", ...],   # dict order
                  "categories": ["git"],                  # sorted
                  "command": "<capped fragment>",
                  "command_truncated": true,               # only when cut
                  "is_error": true | false | null,  # null = no correlated
                                                    # outcome signal (honest)
                  "confirmed": bool,
                  "reaction": {                     # null when unconfirmed
                    "message_index": int,
                    "offset": int,                  # messages after the call
                    "role": "assistant" | "user",
                    "markers": ["извинение", ...],  # labels, not raw text
                    "preview": "<capped fragment>"
                  } | null
                }, ...
              ],
              "count": N,               # full match set (post filters)
              "confirmed_count": M,
              "by_pattern": {"git.reset_hard": 3, ...},
              "truncated": bool,        # limit tripped
              "reaction_window": int,
              "redactions": {...},      # only when something was masked
              "diagnostics": {...}      # only when count == 0
            }

        Records are ordered chronologically (ts ascending, undated last).

    Raises:
        ValueError: on invalid arguments (unknown ``category``/
            ``confirmed``/``agent``/``noise``, malformed ``session`` /
            ``since``/``until``, negative ``reaction_window``/``limit``,
            non-bool ``redact``).
    """
    if confirmed not in CONFIRMED_MODES:
        raise ValueError(
            f"confirmed must be one of {sorted(CONFIRMED_MODES)}, "
            f"got {confirmed!r}"
        )
    if category is not None and category not in DANGER_CATEGORIES:
        raise ValueError(
            f"category must be one of {sorted(DANGER_CATEGORIES)}, "
            f"got {category!r}"
        )
    if (
        not isinstance(reaction_window, int)
        or isinstance(reaction_window, bool)
        or reaction_window < 0
    ):
        raise ValueError(
            "reaction_window must be a non-negative integer, "
            f"got {reaction_window!r}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(
            f"limit must be a non-negative integer, got {limit!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")

    # --- Step 1: candidates from ONE query scan --------------------------
    # redact=False: internal call — dictionary matching must see the RAW
    # command; emission-time redaction below covers everything we output.
    scanned_sessions: dict[str, Any] = {}
    events = _query(
        type="tool_call",
        tool_kind="bash",
        agent=agent,
        session=session,
        since=since,
        until=until,
        limit=0,
        noise=noise,
        project_dir=project_dir,
        scanned_sessions_out=scanned_sessions,
        redact=False,
    )

    # Session title map, reused from the scan (no second corpus walk).
    title_by_uuid: dict[str, Optional[str]] = {}
    for sessions in scanned_sessions.values():
        for sess in sessions or ():
            title_by_uuid[sess.uuid] = getattr(sess, "title", None)

    parser_by_agent = {
        name.value.lower(): PARSERS[name] for name in target_agents(None)
    }

    # Group candidate events by session, then by message_index, so each
    # session's messages are read ONCE and paired with its events.
    by_session: dict[str, dict[int, List[dict[str, Any]]]] = {}
    agent_by_session: dict[str, str] = {}
    for ev in events:
        sid = ev.get("session_id") or ""
        idx = ev.get("message_index", -1)
        if not sid or not isinstance(idx, int) or idx < 0:
            continue
        by_session.setdefault(sid, {}).setdefault(idx, []).append(ev)
        agent_by_session[sid] = ev.get("agent") or ""

    records: List[dict[str, Any]] = []
    for sid, by_msg in by_session.items():
        parser = parser_by_agent.get(agent_by_session.get(sid, ""))
        if parser is None:  # pragma: no cover — agents come from the scan
            continue
        try:
            messages: Sequence[Message] = parser.read_messages(sid)
        except (FileNotFoundError, ValueError, OSError):
            continue
        for idx, msg_events in by_msg.items():
            if not (0 <= idx < len(messages)):
                continue
            entries = _bash_entries(messages[idx])
            # Pair the k-th bash event of this message with the k-th bash
            # tool_use entry — both lists are built with the same filter in
            # the same order (see _bash_entries).
            msg_events.sort(
                key=lambda e: int(str(e.get("id", "")).rsplit(":", 1)[-1] or 0)
            )
            for ev, entry in zip(msg_events, entries):
                payload = _coerce_input(entry.get("input", ""))
                command = _command_text(payload)
                hits = match_danger(command)
                if not hits:
                    continue
                if category is not None and not any(
                    pid.split(".", 1)[0] == category for pid in hits
                ):
                    continue
                reaction = (
                    _scan_reaction(messages, idx, reaction_window)
                    if reaction_window
                    else None
                )
                is_confirmed = reaction is not None
                if confirmed == "only" and not is_confirmed:
                    continue
                if confirmed == "exclude" and is_confirmed:
                    continue
                record: dict[str, Any] = {
                    "id": ev.get("id"),
                    "agent": ev.get("agent"),
                    "session_id": sid,
                    "session_title": title_by_uuid.get(sid),
                    "ts": ev.get("ts"),
                    "message_index": idx,
                    "tool": _ref_value(ev.get("refs") or (), "tool")
                    or ev.get("text"),
                    "patterns": hits,
                    "categories": sorted(
                        {pid.split(".", 1)[0] for pid in hits}
                    ),
                    # Placeholder — the emitted fragment is produced at
                    # emission time below (redact the FULL command first,
                    # THEN cut the window), only for records that survive
                    # the limit slice.
                    "command": None,
                    # Honest tri-state: absent ref = no correlated outcome
                    # signal for this agent/call → null, never False.
                    "is_error": _ref_value(ev.get("refs") or (), "is_error"),
                    "confirmed": is_confirmed,
                    "reaction": reaction,
                    "_raw_command": command,
                }
                records.append(record)

    # Chronological order (ts ascending, undated last) — deterministic.
    records.sort(key=lambda r: (r["ts"] is None, r["ts"] or "", r["id"] or ""))

    total = len(records)
    confirmed_count = sum(1 for r in records if r["confirmed"])
    by_pattern: Dict[str, int] = {}
    for r in records:
        for pid in r["patterns"]:
            by_pattern[pid] = by_pattern.get(pid, 0) + 1

    truncated = False
    if limit and total > limit:
        records = records[:limit]
        truncated = True

    # Emission-time redaction + command windowing (F2.1): only records that
    # survived the limit slice pay for it; dictionary matching above ran on
    # the RAW stored text.  ORDER MATTERS (same rule as ``query``: the cut
    # is applied AFTER redaction): the FULL raw command is redacted first,
    # THEN the window fragment is cut — a secret sliced by the window edge
    # can therefore never leak partially (a truncated secret tail would be
    # too short to trip the redaction patterns on its own).  The window
    # anchor is re-derived on the emitted (possibly redacted) string, since
    # masking shifts offsets; a hit swallowed by a mask falls back to the
    # string head (see :func:`_first_hit_span`).
    redactions: dict[str, int] = {}
    for r in records:
        raw_command = r.pop("_raw_command")
        emit_command = raw_command
        if redact:
            emit_command, counts = redact_text(raw_command)
            if counts:
                merge_redaction_counts(redactions, counts)
        anchor = _first_hit_span(emit_command, r["patterns"])
        cmd_frag, cmd_cut = _fragment(
            emit_command, anchor, _COMMAND_CHARS_CAP, _COMMAND_HEAD_CONTEXT
        )
        r["command"] = cmd_frag
        if cmd_cut:
            r["command_truncated"] = True
        if redact:
            new_val, counts = redact_text(r.get("session_title"))
            if counts:
                r["session_title"] = new_val
                merge_redaction_counts(redactions, counts)
            reaction = r.get("reaction")
            if isinstance(reaction, dict):
                new_val, counts = redact_text(reaction.get("preview"))
                if counts:
                    reaction["preview"] = new_val
                    merge_redaction_counts(redactions, counts)

    response: dict[str, Any] = {
        "incidents": records,
        "count": total,
        "confirmed_count": confirmed_count,
        "by_pattern": by_pattern,
        "truncated": truncated,
        "reaction_window": reaction_window,
    }
    if redactions:
        response["redactions"] = redactions
    if total == 0:
        # Zero incidents: attach corpus diagnostics (missing source dir vs
        # all-excluding filter vs a genuinely clean history).  Lazy import
        # mirrors find_tool_calls.
        from ai_r.diagnostics import empty_result_diagnostics

        response["diagnostics"] = empty_result_diagnostics(
            agent=agent,
            since=since,
            until=until,
            filters={
                "session": session,
                "category": category,
                # defaults are never the cause of emptiness — echo only
                # the non-default values.
                "confirmed": None if confirmed == "include" else confirmed,
                "noise": None if noise == "include" else noise,
                "project_dir": project_dir,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return response
