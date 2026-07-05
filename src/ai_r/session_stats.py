"""Cross-agent ``session_stats`` core — the *summary* half of the WHY-audit.

Where :mod:`ai_r.file_frequency` rolls the edit stream up *by file*, this
module rolls *sessions themselves* up by a chosen dimension so a reader can
answer "how is the work distributed?" in one call:

* ``group_by="agent"`` — claude vs codex vs opencode vs ...
* ``group_by="dir"``   — by working directory / project (``Session.extra``
  ``cwd`` for codex/pi, ``project_slug`` for claude).
* ``group_by="date"``  — by calendar day (``YYYY-MM-DD``).
* ``group_by="kind"``  — top-level *agent* sessions vs spawned *subagent*
  sessions (relies on the parser-provided :attr:`ai_r.parsers.Session.kind`).

Data sources are *reused*, never re-parsed:

* :func:`ai_r.parsers` ``list_sessions`` gives the session inventory
  (uuid, agent, date, kind, cwd) — this is the spine: every group counts
  *sessions*.
* :func:`ai_r.find_file_edits.find_file_edits` enriches each group with the
  number of file edits and the count of distinct *intents* (the requests
  behind those edits) — the same record stream ``file_frequency`` consumes.

Output mirrors :func:`ai_r.file_frequency.file_frequency`: a dict with a
ranked ``groups`` list plus a ``totals`` block.

RISK-4 (degenerate kind split): if the matched sessions contain no
subagents at all, a ``group_by="kind"`` result would show a single
``agent`` row and an auditor could mistake that for "subagents were
checked and there were none". To stay honest the result always carries a
``kind_split_available`` boolean and, when ``False``, a human-readable
``note`` saying the split is degenerate because no subagent sessions were
in scope (subagent detection is Claude-only today).

Invariants (kept identical to the rest of the package): zero-LLM,
deterministic, read-only, pure-stdlib (no new dependencies).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from ai_r.events.aggregate import aggregate as _aggregate
from ai_r.find_file_edits import find_file_edits
from ai_r.parsers import PARSERS, Session, iso, target_agents
from ai_r.tokens import session_tokens as _session_tokens

__all__ = [
    "GROUP_BY",
    "children_of",
    "group_key",
    "session_stats",
]


def children_of(
    parent_uuid: str, *, agent: Optional[str] = None
) -> List[Session]:
    """Return the spawned subagent sessions whose parent is ``parent_uuid``.

    Scans the session inventory of every in-scope agent (``target_agents``
    resolves the optional ``agent`` filter) and keeps the sessions whose
    :attr:`ai_r.parsers.Session.parent_uuid` equals ``parent_uuid``.  Reuses
    the same ``list_sessions`` spine the rollups consume — no re-parse of
    transcripts, purely inventory metadata.

    Note: Antigravity never records a ``parent_uuid``, so a parent under that
    agent (or any childless parent) yields an empty list — an honest "no
    children found", not an error.
    """
    if not parent_uuid or not str(parent_uuid).strip():
        return []
    out: List[Session] = []
    for agent_name in target_agents(agent):
        parser = PARSERS[agent_name]
        try:
            sessions = parser.list_sessions()
        except (FileNotFoundError, ValueError, OSError):
            continue
        for session in sessions:
            if session.parent_uuid == parent_uuid:
                out.append(session)
    return out


# The dimensions a caller may roll up by.  Kept as an explicit, validated
# set so an unknown ``group_by`` fails fast with a clear message instead of
# silently bucketing everything under ``None``.
GROUP_BY: frozenset[str] = frozenset({"agent", "dir", "date", "kind"})


def _session_dir(session: Any) -> str:
    """Best-effort working-directory / project label for a session.

    Codex and Pi carry an absolute ``cwd`` in ``Session.extra``; Claude
    carries a ``project_slug`` (the encoded project path).  Agents that
    expose neither (OpenCode, Antigravity) fall back to the literal
    ``"(unknown)"`` so they still form a single, honest bucket rather than
    vanishing from the rollup.
    """
    extra = getattr(session, "extra", None) or {}
    cwd = extra.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    slug = extra.get("project_slug")
    if isinstance(slug, str) and slug:
        return slug
    return "(unknown)"


def group_key(session: Any, group_by: str) -> str:
    """Return the bucket label for ``session`` under dimension ``group_by``.

    * ``agent`` → the lowercase agent name (``"claude"``, ``"codex"``, ...).
    * ``dir``   → ``cwd`` / ``project_slug`` (see :func:`_session_dir`).
    * ``date``  → the ``YYYY-MM-DD`` calendar day of ``session.date``
      (``"(undated)"`` when the session has no usable timestamp).
    * ``kind``  → ``session.kind`` (``"agent"`` / ``"subagent"``).
    """
    if group_by == "agent":
        return session.agent.value.lower()
    if group_by == "kind":
        # Defensive: an unexpected/empty kind folds into "agent" so the
        # split stays binary and never grows a stray bucket.
        return session.kind if session.kind in ("agent", "subagent") else "agent"
    if group_by == "dir":
        return _session_dir(session)
    # date
    stamp = iso(session.date) if session.date is not None else None
    if not stamp:
        return "(undated)"
    return stamp[:10]


def session_stats(
    *,
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    group_by: str = "agent",
    top: int = 8,
    edit_path: str = "/",
    with_tokens: bool = False,
) -> dict[str, Any]:
    """Summarise sessions, grouped by ``group_by`` and ranked by session count.

    The session inventory comes from each parser's ``list_sessions`` (the
    spine: every group counts *sessions*), enriched per group with file-edit
    and distinct-intent counts from the shared
    :func:`ai_r.find_file_edits.find_file_edits` core (``limit=0``, no cap).

    Args:
        agent: Optional filter, one of ``"claude"``, ``"codex"``,
            ``"opencode"``, ``"antigravity"``, ``"pi"``.  ``None`` = all
            agents.
        since: Optional ISO 8601 lower bound (inclusive).  Applied to the
            *session date* for the inventory and forwarded to
            :func:`find_file_edits` for the edit/intent enrichment.
        until: Optional ISO 8601 upper bound (inclusive), same semantics.
        group_by: One of :data:`GROUP_BY` — ``"agent"`` (default), ``"dir"``,
            ``"date"`` or ``"kind"``.
        top: How many top groups to include in ``groups``.  ``0`` = all.
            Default ``8``.
        edit_path: Substring matched against edited file paths when counting
            edits (forwarded to :func:`find_file_edits`, which requires a
            non-empty path).  Defaults to ``"/"`` (matches absolute paths).
        with_tokens: When ``True``, every matched session's token usage is
            read **at request time** (F3.3, SSOT :mod:`ai_r.tokens` —
            exact where the format records usage, a labeled estimate
            otherwise, honest ``unknown`` without any signal) and each
            group / ``totals`` carries a folded ``tokens`` block
            (``{input, output, reasoning, cache_read, cache_write, total,
            exact, estimated, unknown}`` — sums are ``None`` when no row
            carried the field; the three counters say how many sessions
            were exact / estimated / unknown).  Default ``False`` keeps
            the historical output byte-identical and pays no read cost.

    Returns:
        A dict::

            {
                "group_by": str,
                "groups": [
                    {
                        "group": str,        # the bucket label
                        "sessions": int,     # distinct sessions in bucket
                        "edits": int,        # file edits attributed to bucket
                        "intents": int,      # distinct edit-intents in bucket
                        "agents": [str],     # sorted, distinct
                        "messages": int,     # sum of message_count
                    },
                    ...
                ],
                "totals": {
                    "sessions": int,
                    "edits": int,
                    "agents": int,
                    "agents_list": [str],
                },
                "kind_split_available": bool,  # see RISK-4 note below
                "note": str,                   # present only when degenerate
            }

        ``groups`` is truncated to ``top`` rows; ``totals`` always reflects
        the full (untruncated) match set.

        ``kind_split_available`` is ``True`` when at least one subagent
        session is in scope.  When ``False`` a ``note`` field explains that a
        ``group_by="kind"`` split is degenerate (no subagents matched) so an
        auditor does not read an all-``agent`` result as a verified
        "subagents: none".

    Raises:
        ValueError: on an unknown ``group_by``, a negative/boolean ``top``,
            or any argument rejected by :func:`find_file_edits`
            (unparseable ``since``/``until``, unknown ``agent``).
    """
    if group_by not in GROUP_BY:
        raise ValueError(
            f"group_by must be one of {sorted(GROUP_BY)}, got {group_by!r}"
        )
    if not isinstance(top, int) or isinstance(top, bool) or top < 0:
        raise ValueError(f"top must be a non-negative integer, got {top!r}")
    if not isinstance(with_tokens, bool):
        raise ValueError(f"with_tokens must be a boolean, got {with_tokens!r}")

    # ``find_file_edits`` is the canonical validator for agent/since/until.
    # Run it first so an invalid bound or agent raises the same ValueError
    # the rest of the package surfaces — and so we reuse (not re-parse) the
    # edit stream for enrichment.
    edits = find_file_edits(
        path=edit_path,
        agent=agent,
        since=since,
        until=until,
        limit=0,
        # Internal call — only *counts* are derived from the records (no
        # text is emitted), and redaction could merge two distinct raw
        # intents under one masked string (an intents-count drift): keep
        # the fold on raw data.
        redact=False,
    )

    # Edit enrichment, keyed by session uuid: edit count + distinct intents.
    edits_by_session: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"edits": 0, "intents": set()}
    )
    for r in edits["records"]:
        uuid = r.get("session_uuid")
        if not uuid:
            continue
        bucket = edits_by_session[uuid]
        bucket["edits"] += 1
        intent = r.get("intent")
        if intent and isinstance(intent, str) and intent.strip():
            bucket["intents"].add(intent.strip())

    # Session inventory spine.  ``since``/``until`` were already validated by
    # find_file_edits above; reuse its parsed bounds via re-parse here is
    # unnecessary — we compare ISO strings lexicographically on the date
    # prefix, which is correct for the inclusive day-level filtering callers
    # expect and keeps this module dependency-light.
    since_s = (since or "").strip() or None
    until_s = (until or "").strip() or None

    # Build one row per inventoried session (after the since/until filter),
    # carrying the exact fields the rollup folds: group label under EVERY
    # dimension, plus the enrichment (edits / intents / agents / messages).
    # ``aggregate`` then does the grouping, ranking and totals — this tool is
    # a thin preset over it (``rank_by="stats"`` reproduces the historical
    # sessions-first rank; ``kind_split=True`` adds the RISK-4 fields).
    session_rows: list[dict[str, Any]] = []
    for agent_name in target_agents(agent):
        parser = PARSERS[agent_name]
        for session in parser.list_sessions():
            stamp = iso(session.date) if session.date is not None else None
            # Inclusive ISO-string bound filter on the session date.  Bounds
            # may be a full timestamp or a bare date; lexicographic compare on
            # the ISO string is monotonic so it is safe for either form.
            if since_s is not None and (stamp is None or stamp < since_s):
                continue
            if until_s is not None and (stamp is None or stamp[: len(until_s)] > until_s):
                continue

            enrich = edits_by_session.get(session.uuid)
            row: dict[str, Any] = {
                "session_uuid": session.uuid,
                "agent": group_key(session, "agent"),
                "dir": group_key(session, "dir"),
                "date": group_key(session, "date"),
                "kind": group_key(session, "kind"),
                "edits": enrich["edits"] if enrich is not None else 0,
                "intents": sorted(enrich["intents"]) if enrich is not None else [],
                "messages": int(getattr(session, "message_count", 0) or 0),
            }
            if with_tokens:
                # F3.3: read the session's own files NOW (request-time,
                # nothing background) — exact where recorded, labeled
                # estimate otherwise, honest unknown without signal.
                row["tokens"] = _session_tokens(session)
            session_rows.append(row)

    metrics = ["sessions", "edits", "intents", "agents", "messages"]
    if with_tokens:
        metrics.append("tokens")
    rolled = _aggregate(
        session_rows,
        group_by=group_by,
        metrics=metrics,
        rank_by="stats",
        kind_split=True,
    )
    if top:
        rolled = {**rolled, "groups": rolled["groups"][:top]}

    # Project the aggregate result onto the historical session_stats shape:
    # totals carry only the four legacy keys (sessions/edits/agents/
    # agents_list), and the RISK-4 flag/note ride along from ``kind_split``.
    result: dict[str, Any] = {
        "group_by": rolled["group_by"],
        "groups": rolled["groups"],
        "totals": {
            "sessions": rolled["totals"]["sessions"],
            "edits": rolled["totals"]["edits"],
            "agents": rolled["totals"]["agents"],
            "agents_list": rolled["totals"]["agents_list"],
        },
        "kind_split_available": rolled["kind_split_available"],
    }
    if with_tokens:
        result["totals"]["tokens"] = rolled["totals"]["tokens"]
    if "note" in rolled:
        result["note"] = rolled["note"]
    return result
