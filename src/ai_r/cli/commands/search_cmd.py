"""``ai-r search`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, List

from ai_r.cli.shared import (
    _add_filter_group,
    _AGENT_CHOICES,
    _exit_with_error,
    _format_table,
    _passes_date_filters,
    _validate_date_args,
)
from ai_r.parsers import AgentName, Session, target_agents as _target_agents


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``search`` subcommand on ``subparsers``."""
    search_p = subparsers.add_parser("search", help="Case-insensitive title search")
    search_p.add_argument("query", help="Substring to search for in session titles.")
    search_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    search_p.add_argument(
        "--scope",
        default="title",
        help="Where to search: title (default, backward-compat), body (message text + tool calls), or all (title OR body).",
    )
    search_p.add_argument(
        "--operator",
        "--op",
        dest="operator",
        default="and",
        help="How to combine terms: and (default), or, or not. Negative prefix: '-term' is always excluded.",
    )
    search_p.add_argument(
        "--sort",
        choices=("relevance", "date"),
        default="relevance",
        help="Result ordering: relevance (BM25, default) or date (newest-first).",
    )
    search_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    _add_filter_group(search_p)
    search_p.set_defaults(func=_run_search)


def _run_search(args: argparse.Namespace) -> int:
    """Run the ``search`` subcommand.

    New behaviour: scope/operator delegation to ``mcp_server.search_sessions``
    is the single source of truth; this wrapper only adds CLI-side validation
    and date filtering on top.
    """
    query = (args.query or "").strip()
    if not query:
        return _exit_with_error("search query must be non-empty")

    scope = getattr(args, "scope", "title")
    if scope not in ("title", "body", "all"):
        return _exit_with_error(
            f"unknown --scope {scope!r}; expected title, body, or all"
        )

    operator_raw = (getattr(args, "operator", "and") or "and").lower()
    if operator_raw not in ("and", "or", "not"):
        return _exit_with_error(
            f"unknown --operator {operator_raw!r}; expected and, or, or not"
        )
    operator = operator_raw.upper()

    limit = getattr(args, "limit", None)
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        return _exit_with_error(
            f"--limit must be a non-negative integer, got {limit!r}"
        )

    try:
        _target_agents(args.agent)
        _validate_date_args(args)
    except ValueError as exc:
        return _exit_with_error(str(exc))

    sort = getattr(args, "sort", "relevance")

    from ai_r import mcp_server as _mcp

    # Delegate the actual search to mcp_server (single source of truth for
    # query parsing, scope matching, operator combination, and relevance
    # ranking). We pass limit=0 so the full ranked result set comes back;
    # we then apply date filters (order-preserving) and trim ourselves.
    raw = _mcp.search_sessions(
        query=query,
        agent=args.agent,
        scope=scope,
        operator=operator,
        limit=0,
        sort=sort,
    )

    if isinstance(raw, dict) and raw.get("error") == "invalid_argument":
        return _exit_with_error(raw.get("message", "invalid argument"))

    results = raw["results"]
    filtered: List[dict[str, Any]] = []
    for summary in results:
        try:
            sess = Session(
                uuid=summary["uuid"],
                agent=AgentName(summary["agent"]),
                title=summary.get("title", ""),
                date=datetime.fromisoformat(summary["date"].rstrip("Z")),
                path="",
                message_count=summary.get("message_count", 0),
            )
        except (KeyError, ValueError):
            continue
        if _passes_date_filters(sess, args):
            filtered.append(summary)

    if limit:
        filtered = filtered[:limit]

    if args.json:
        json.dump(filtered, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not filtered:
        print(f"(no sessions match {query!r})", file=sys.stderr)
        return 0
    print(_format_table(filtered))
    return 0
