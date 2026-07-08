"""Cross-agent ``file_frequency`` core, shared by the CLI and the audit script.

Mirrors the layout of :mod:`ai_r.find_file_edits` / :mod:`ai_r.find_tool_calls`:
the pure-Python aggregation logic lives here so the CLI handler
(:mod:`ai_r.cli.commands.file_frequency`) and the example audit script
(``examples/audits/file_frequency.py``) both delegate to a single
implementation.

This is the aggregation half of the "WHY-audit": :func:`ai_r.find_file_edits`
already carries the *request behind each edit* (``intent``); this module rolls
that flat record stream up by file so "what got the most attention, and from
how many agents / sessions / distinct requests?" becomes one row per file.

Invariants (kept identical to the rest of the package): zero-LLM,
deterministic, read-only, pure-stdlib (no new dependencies).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from ai_r.find_file_edits import find_file_edits

__all__ = [
    "aggregate",
    "rank",
    "file_frequency",
]


def aggregate(records: List[dict]) -> Dict[str, dict]:
    """Roll a ``find_file_edits`` record stream up by file path.

    Returns ``{file: {"edits": int, "sessions": set, "agents": set,
    "intents": set}}``.  ``sessions`` / ``agents`` / ``intents`` are sets so
    the caller reads distinct counts off ``len(...)``; ``None`` intents are
    skipped so they do not inflate the distinct-intent count.
    """
    by_file: Dict[str, dict] = defaultdict(
        lambda: {
            "edits": 0,
            "sessions": set(),
            "agents": set(),
            "intents": set(),
        }
    )
    for r in records:
        path = r.get("file")
        if not path:
            continue
        bucket = by_file[path]
        bucket["edits"] += 1
        if r.get("session_uuid"):
            bucket["sessions"].add(r["session_uuid"])
        if r.get("agent"):
            bucket["agents"].add(r["agent"])
        intent = r.get("intent")
        if intent:
            # De-dup on the exact request text: one request can drive several
            # edits to the same file, and we only want *distinct* intents.
            bucket["intents"].add(intent.strip())
    return by_file


def rank(by_file: Dict[str, dict], top: int) -> List[tuple]:
    """Return the ``top`` files as ``(file, stats)`` ordered by edits.

    Ordering key: edits desc, then distinct-sessions desc, then path asc
    (a stable, deterministic tie-break so repeated runs print identically).
    ``top == 0`` returns every file.
    """
    items = sorted(
        by_file.items(),
        key=lambda kv: (-kv[1]["edits"], -len(kv[1]["sessions"]), kv[0]),
    )
    return items[:top] if top else items


def file_frequency(
    *,
    path: str = "/",
    agent: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    top: int = 8,
) -> dict[str, Any]:
    """Rank edited files by how much attention they got, cross-agent.

    A thin group-by over the public :func:`ai_r.find_file_edits` core (the
    same core the CLI ``find-file-edits`` and the MCP tool use) called with
    ``limit=0`` (no cap), rolled up by file path.

    Args:
        path: Substring matched against edited file paths.
            :func:`ai_r.find_file_edits` requires a non-empty path; ``"/"``
            (the default) matches absolute paths.  Pass e.g. ``"src/"`` to
            scope, or ``"."`` to also catch relative paths.
        agent: Optional filter, one of ``"claude"``, ``"codex"``,
            ``"opencode"``, ``"antigravity"``, ``"pi"``.  ``None`` = all
            agents.
        since: Optional ISO 8601 lower bound (inclusive) on edit timestamp.
        until: Optional ISO 8601 upper bound (inclusive) on edit timestamp.
        top: How many top files to include in ``files``.  ``0`` = all.
            Default ``8``.

    Returns:
        A dict::

            {
                "files": [
                    {
                        "file": str,
                        "edits": int,
                        "sessions": int,   # distinct
                        "intents": int,    # distinct
                        "agents": [str],   # sorted, distinct
                    },
                    ...
                ],
                "total_edits": int,        # across all matching files
                "total_files": int,        # distinct files matched
                "total_sessions": int,     # distinct sessions with edits
                "total_agents": int,       # distinct agents
                "agents": [str],           # sorted, distinct, all matches
            }

        ``files`` is truncated to ``top`` rows; the ``total_*`` counters
        always reflect the full (untruncated) match set.

    Raises:
        ValueError: on invalid arguments, propagated from
            :func:`ai_r.find_file_edits` (empty ``path``, negative bound,
            unparseable ``since``/``until``, unknown ``agent``) or for a
            negative ``top``.
    """
    if not isinstance(top, int) or isinstance(top, bool) or top < 0:
        raise ValueError(f"top must be a non-negative integer, got {top!r}")

    result = find_file_edits(
        path=path,
        agent=agent,
        since=since,
        until=until,
        limit=0,
        # Internal rollup: distinct-intent counting must fold on RAW intent
        # text (a char cap could merge two long intents) and a byte budget
        # must never drop records from a count.
        size_caps=False,
    )

    by_file = aggregate(result["records"])

    all_agents: set[str] = set()
    all_sessions: set[str] = set()
    for stats in by_file.values():
        all_agents |= stats["agents"]
        all_sessions |= stats["sessions"]

    files = [
        {
            "file": path_,
            "edits": stats["edits"],
            "sessions": len(stats["sessions"]),
            "intents": len(stats["intents"]),
            "agents": sorted(stats["agents"]),
        }
        for path_, stats in rank(by_file, top)
    ]

    return {
        "files": files,
        "total_edits": result["count"],
        "total_files": len(by_file),
        "total_sessions": len(all_sessions),
        "total_agents": len(all_agents),
        "agents": sorted(all_agents),
    }
