"""Empty-result diagnostics — *why* did a scan return zero results?

A bare empty list is ambiguous: "the corpus genuinely has no match" looks
identical to "the filter is wrong" or "the source directory is missing", so
a consumer over-trusts a false-empty result instead of fixing the call.
(The idea mirrors cass's zero-result diagnosis; the implementation is
ai-r-native: it walks the same :data:`~ai_r.parsers.PARSERS` registry the
scanning methods use.)

:func:`empty_result_diagnostics` builds a ``diagnostics`` dict the caller
attaches NEXT TO an empty result (``{"records": [], "count": 0,
"diagnostics": {...}}``).  It is only computed on the empty path — a
non-empty response never carries (or pays for) it.

A caller that already enumerated sessions during its own scan passes them
via ``scanned_sessions`` so the diagnostics are aggregated from that scan
and the corpus is never walked twice; a fresh ``list_sessions()`` re-scan
is only the fallback for agents the caller did not provide.

Shape::

    {
      "scanned": [            # one entry per scanned agent
        {"agent": "claude", "sessions": 42, "date_min": "...",
         "date_max": "...", "source_found": true},
        {"agent": "pi", "sessions": 0, "date_min": null, "date_max": null,
         "source_found": false,
         "hint": "source not found: /home/u/.pi/agent/sessions"},
      ],
      "corpus": {"sessions": 42, "date_min": "...", "date_max": "..."},
      "filters": {"agent": null, "since": null, "until": null, ...},
      "hints": ["42 session(s) scanned across 5 agent(s); ..."],
    }

Diagnostics must never crash the (already empty) response: every parser
access is wrapped and failures degrade to a per-agent ``hint``.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from ai_r.parsers import PARSERS, AgentName, iso, target_agents
from ai_r.find_file_edits import parse_iso_bound, to_utc_aware
from ai_r.redact import REDACTION_MARKER_PREFIX, secret_like_types

__all__ = ["empty_result_diagnostics"]


def _append_redaction_hints(
    active_filters: Mapping[str, Any], hints: List[str]
) -> None:
    """Add redaction-awareness hints for secret-looking filter values (F2.1).

    Redaction is emission-time only — filters always match the RAW stored
    text — so two honest cases exist for an empty result:

    * the filter value contains a ``[REDACTED_*]`` placeholder (copied from
      previously redacted output): placeholders never exist in stored
      session text, so the search can never match;
    * the filter value itself *looks like a secret* (trips the redaction
      patterns): matching ran on raw text, so emptiness means the literal
      value is genuinely absent — and on a hit the output would have shown
      ``[REDACTED_*]``, which is worth saying out loud.

    Cheap by construction: one combined-pattern pass per (short) filter
    string, computed only on the already-empty path.
    """
    for key, val in active_filters.items():
        if not isinstance(val, str) or not val:
            continue
        if REDACTION_MARKER_PREFIX in val:
            hints.append(
                f"filter {key} contains a [REDACTED_*] placeholder — "
                "redaction is applied on output only, placeholders never "
                "exist in stored session text and can never match; search "
                "for the raw secret value instead (redact=false shows raw "
                "output)"
            )
            continue
        types = secret_like_types(val)
        if types:
            hints.append(
                f"redaction is enabled and filter {key} looks like a "
                f"secret ({', '.join(types)}) — matching runs on RAW "
                "stored text, so this empty result means the literal "
                "value is absent from the scanned corpus; on a match the "
                "output would show [REDACTED_*] — retry with redact=false "
                "to see raw values"
            )


def _scan_agent(
    agent_name: AgentName,
    sessions: Optional[Sequence[Any]] = None,
) -> Tuple[dict[str, Any], Optional[datetime], Optional[datetime]]:
    """Return ``(entry, date_min, date_max)`` for one agent.

    ``entry`` is the per-agent ``scanned`` element; the datetimes feed the
    corpus-wide bounds.  Never raises — an unreadable source degrades to a
    zero-session entry with a ``hint``.

    When ``sessions`` is given (the caller already enumerated this agent
    during its own scan), it is used as-is and ``parser.list_sessions()``
    is NOT called again — only the cheap source-dir probe runs.  ``None``
    means "not provided" and falls back to a fresh listing.
    """
    parser = PARSERS[agent_name]
    label = agent_name.value.lower()

    roots: List[str] = []
    try:
        roots = list(parser.source_roots())
    except (FileNotFoundError, ValueError, OSError):  # pragma: no cover
        roots = []
    source_found = any(os.path.exists(r) for r in roots)

    entry: dict[str, Any] = {
        "agent": label,
        "sessions": 0,
        "date_min": None,
        "date_max": None,
        "source_found": source_found,
    }

    if sessions is None:
        try:
            sessions = parser.list_sessions()
        except (FileNotFoundError, ValueError, OSError) as exc:
            entry["hint"] = f"listing sessions failed: {exc}"
            return entry, None, None

    entry["sessions"] = len(sessions)
    dates = [
        dt
        for dt in (to_utc_aware(getattr(s, "date", None)) for s in sessions)
        if dt is not None
    ]
    date_min = min(dates) if dates else None
    date_max = max(dates) if dates else None
    if date_min is not None:
        entry["date_min"] = iso(date_min)
    if date_max is not None:
        entry["date_max"] = iso(date_max)

    if not sessions:
        if not source_found:
            where = ", ".join(roots) if roots else "(no known location)"
            entry["hint"] = f"source not found: {where}"
        else:
            entry["hint"] = "source present but contains no sessions"
    return entry, date_min, date_max


def empty_result_diagnostics(
    *,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    filters: Optional[dict[str, Any]] = None,
    scanned_sessions: Optional[Mapping[Any, Sequence[Any]]] = None,
    redact_active: bool = False,
) -> dict[str, Any]:
    """Explain an empty scan result: what was scanned and why nothing matched.

    Args:
        agent: The ``agent`` filter the failing call used (``None`` = all).
        since / until: The ISO date bounds the failing call used, if any.
            Assumed already validated upstream (an unparseable bound is
            simply skipped here — diagnostics never raise).
        filters: The *other* active filter values of the failing call
            (e.g. ``{"path": "...", "text": "..."}``).  ``None`` values
            are dropped from the echo.
        scanned_sessions: Per-agent session lists the caller ALREADY
            enumerated during its own scan (key: :class:`AgentName` or its
            lowercase label; value: the ``parser.list_sessions()`` result).
            Agents present here are aggregated from the provided list and
            are NOT re-listed — on a large corpus a second full
            ``list_sessions()`` walk would double the scan cost.  Agents in
            the target set but absent from the mapping fall back to a
            fresh ``parser.list_sessions()`` (fallback only, for callers
            that did not pass their scan).
        redact_active: ``True`` when the failing call ran with output
            redaction enabled (the default).  Adds a redaction hint when a
            string filter value contains a ``[REDACTED_*]`` placeholder or
            itself looks like a secret (see :func:`_append_redaction_hints`).

    Returns:
        The ``diagnostics`` dict (see the module docstring).  JSON-safe.
    """
    try:
        targets = target_agents(agent)
    except ValueError:
        # An unknown agent is rejected upstream before any scan; if it
        # somehow reaches here, fall back to the full registry.
        targets = list(PARSERS)

    # Normalize provided per-agent lists to lowercase-label keys; presence
    # in this dict (even with an empty list) means "do not rescan".
    provided: dict[str, Sequence[Any]] = {}
    for key, sessions in (scanned_sessions or {}).items():
        label = getattr(key, "value", key)
        provided[str(label).lower()] = sessions

    scanned: List[dict[str, Any]] = []
    corpus_min: Optional[datetime] = None
    corpus_max: Optional[datetime] = None
    for agent_name in targets:
        label = agent_name.value.lower()
        entry, dt_min, dt_max = _scan_agent(
            agent_name, sessions=provided.get(label)
        )
        scanned.append(entry)
        if dt_min is not None and (corpus_min is None or dt_min < corpus_min):
            corpus_min = dt_min
        if dt_max is not None and (corpus_max is None or dt_max > corpus_max):
            corpus_max = dt_max
    total = sum(e["sessions"] for e in scanned)

    active_filters: dict[str, Any] = {"agent": agent, "since": since, "until": until}
    for key, val in (filters or {}).items():
        if val is not None:
            active_filters[key] = val

    hints: List[str] = []
    if total == 0:
        if agent:
            hints.append(
                f"agent '{agent}': no sessions found — try omitting the "
                "agent filter to scan all supported agents"
            )
        else:
            hints.append(
                "no sessions found for any supported agent — the vault "
                "appears empty (check the source paths under 'scanned')"
            )
    else:
        # The corpus is non-empty, so a filter excluded everything.  Call
        # out an all-excluding date window explicitly; else name the
        # remaining filters.
        try:
            since_dt = parse_iso_bound(since, "since")
        except ValueError:
            since_dt = None
        try:
            until_dt = parse_iso_bound(until, "until")
        except ValueError:
            until_dt = None
        date_excluded = False
        if since_dt is not None and corpus_max is not None and since_dt > corpus_max:
            hints.append(
                f"since={since!r} is after the newest session "
                f"({iso(corpus_max)}) — the date filter excludes the "
                "entire corpus"
            )
            date_excluded = True
        if until_dt is not None and corpus_min is not None and until_dt < corpus_min:
            hints.append(
                f"until={until!r} is before the oldest session "
                f"({iso(corpus_min)}) — the date filter excludes the "
                "entire corpus"
            )
            date_excluded = True
        if not date_excluded:
            named = ", ".join(
                f"{k}={v!r}"
                for k, v in active_filters.items()
                if v is not None
            )
            if named:
                hints.append(
                    f"{total} session(s) scanned across {len(scanned)} "
                    f"agent(s); nothing matched the filters: {named}"
                )
            else:
                hints.append(
                    f"{total} session(s) scanned across {len(scanned)} "
                    "agent(s); the corpus genuinely has no match"
                )

    if redact_active:
        _append_redaction_hints(active_filters, hints)

    return {
        "scanned": scanned,
        "corpus": {
            "sessions": total,
            "date_min": iso(corpus_min) if corpus_min is not None else None,
            "date_max": iso(corpus_max) if corpus_max is not None else None,
        },
        "filters": active_filters,
        "hints": hints,
    }
