"""``ai-r search`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, List

from ai_r.cli.shared import (
    _exit_with_error,
    _format_table,
    _passes_date_filters,
    _validate_date_args,
)
from ai_r.parsers import AgentName, Session, target_agents as _target_agents


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

    from ai_r import mcp_server as _mcp

    # Delegate the actual search to mcp_server (single source of truth for
    # query parsing, scope matching, operator combination). We pass
    # limit=0 so we can apply date filters first and then trim ourselves.
    raw = _mcp.search_sessions(
        query=query,
        agent=args.agent,
        scope=scope,
        operator=operator,
        limit=0,
    )

    if raw and isinstance(raw[0], dict) and raw[0].get("error") == "invalid_argument":
        return _exit_with_error(raw[0].get("message", "invalid argument"))

    filtered: List[dict[str, Any]] = []
    for summary in raw:
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
