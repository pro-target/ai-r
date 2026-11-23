"""Cross-agent ``usage`` core: mentions vs uses for an arbitrary needle.

"Usage" in the orderer's sense has two distinct signals, and conflating
them is the mistake this module exists to prevent:

* **mentions** — how many sessions TALK about the needle (found by the
  ordinary content search, ``search_sessions(scope="all")``);
* **uses** — how many tool calls actually PASSED the needle in their
  input (``find_tool_calls(input_contains=...)``): a fact of use, not a
  conversation about using it.

The composition reuses both existing cores unchanged: the session-scope
filters (``agent`` / ``project`` / ``days``) are resolved ONCE into a
per-agent uuid set (list_sessions + the session's ``project_dir`` label
and date), then handed to each core — ``search_sessions`` per agent and
``find_tool_calls`` via its ``session`` facet — so both counts always
agree on which sessions were in scope.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ai_r.parsers import (
    PARSERS,
    cached_list_sessions,
    target_agents,
)
# Private intra-package import: the SSOT project-label fallback chain
# (``project_dir`` → ``extra.cwd`` → ``extra.project_slug``), shared with
# the ``stats --group-by dir`` rollup instead of being re-derived here.
from ai_r.session_stats import _session_dir

__all__ = [
    "usage",
]


def _in_scope_uuids(
    agent_name: Any,
    *,
    project: Optional[str],
    cutoff: Optional[datetime],
) -> set[str]:
    """Uuids of ``agent_name``'s sessions surviving the scope filters.

    ``project`` is a case-insensitive substring on the session's project
    label (:func:`ai_r.session_stats._session_dir`); ``cutoff`` drops
    sessions dated before it (tz-aware dates compared tz-dropped, the
    same convention as the CLI date filters).
    """
    parser = PARSERS[agent_name]
    keep: set[str] = set()
    for sess in cached_list_sessions(agent_name, parser):
        if cutoff is not None:
            date = sess.date
            if date.tzinfo is not None:
                date = date.replace(tzinfo=None)
            if date < cutoff:
                continue
        if project is not None and project.lower() not in _session_dir(
            sess
        ).lower():
            continue
        keep.add(sess.uuid)
    return keep


def usage(
    needle: str,
    *,
    agent: Optional[str] = None,
    project: Optional[str] = None,
    days: Optional[int] = None,
) -> dict[str, Any]:
    """Count session mentions vs tool-call uses of an arbitrary needle.

    Args:
        needle: The entity to measure — a skill name, a path fragment, a
            term.  For **mentions** it is passed to
            :func:`ai_r.mcp_server.search_sessions` verbatim as the query
            (``scope="all"``), so the ordinary search syntax applies:
            bare words AND together, ``"quoted phrases"`` match exactly,
            a ``-prefix`` excludes.  For **uses** it is a plain
            case-insensitive substring on the serialized tool input (the
            ``input_contains`` contract of
            :func:`ai_r.find_tool_calls.find_tool_calls`) — no term
            parsing, one literal fragment.
        agent: Optional agent filter (``"claude"``, ``"codex"``, ...);
            ``None`` = all agents.
        project: Optional case-insensitive substring on the session's
            project label (``project_dir`` / ``cwd`` / ``project_slug``
            — the same fallback chain the ``stats --group-by dir``
            rollup uses).  ``None`` = every project.
        days: Optional recency window: keep sessions dated within the
            last N days (vs now).  ``None`` = no bound.

    Returns:
        A dict ``{"needle": str, "mentions": int, "uses": int,
        "by_agent": {...}, "filters": {...}}``.  ``mentions`` counts
        in-scope SESSIONS whose title/body matches the needle;
        ``uses`` counts in-scope tool CALLS whose input mentions it (a
        session with three matching calls contributes 1 mention and 3
        uses).  ``by_agent`` maps each scanned agent to
        ``{"sessions": in-scope session count, "mentions": ...,
        "uses": ...}``; ``filters`` echoes the applied filters.

    Raises:
        ValueError: blank ``needle``, blank ``project``, non-positive
            ``days``, or an unknown ``agent``.
    """
    if not isinstance(needle, str) or not needle.strip():
        raise ValueError(f"needle must be a non-empty string, got {needle!r}")
    if project is not None and (
        not isinstance(project, str) or not project.strip()
    ):
        raise ValueError(f"project must be a non-empty string, got {project!r}")
    if days is not None and (
        not isinstance(days, int) or isinstance(days, bool) or days <= 0
    ):
        raise ValueError(f"days must be a positive integer, got {days!r}")
    # Unknown agent fails loud here (ValueError from target_agents).
    targets = target_agents(agent)
    needle = needle.strip()
    project = project.strip() if project is not None else None
    cutoff = (
        datetime.now() - timedelta(days=days) if days is not None else None
    )

    # Resolve the session scope ONCE so both cores agree on it.
    scope: dict[str, set[str]] = {}
    for agent_name in targets:
        label = agent_name.value.lower()
        scope[label] = _in_scope_uuids(
            agent_name, project=project, cutoff=cutoff
        )

    # --- Mentions: ordinary content search, per agent -------------------
    from ai_r.mcp_server import search_sessions

    mentions_by_agent: dict[str, int] = {}
    for label, uuids in scope.items():
        res = search_sessions(needle, agent=label, scope="all", limit=0)
        if "error" in res:
            raise ValueError(str(res.get("message") or res["error"]))
        found = {r.get("uuid") for r in res.get("results") or ()}
        mentions_by_agent[label] = len(found & uuids)

    # --- Uses: tool calls whose input mentions the needle ----------------
    # find_tool_calls raises ValueError on an EMPTY session list (a
    # fail-loud guard against silent unfiltered scans), so an agent whose
    # in-scope set is empty is simply 0 here rather than a call.
    from ai_r.find_tool_calls import find_tool_calls

    uses_by_agent: dict[str, int] = {}
    for label, uuids in scope.items():
        if not uuids:
            uses_by_agent[label] = 0
            continue
        res = find_tool_calls(
            input_contains=needle,
            agent=label,
            session=sorted(uuids),
            # count is computed BEFORE the limit slice, so limit=1 keeps
            # the emission cheap while the count stays exact.
            limit=1,
        )
        uses_by_agent[label] = int(res.get("count") or 0)

    by_agent = {
        label: {
            "sessions": len(scope[label]),
            "mentions": mentions_by_agent[label],
            "uses": uses_by_agent[label],
        }
        for label in scope
    }
    return {
        "needle": needle,
        "mentions": sum(mentions_by_agent.values()),
        "uses": sum(uses_by_agent.values()),
        "by_agent": by_agent,
        "filters": {"agent": agent, "project": project, "days": days},
    }
