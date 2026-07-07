"""``ai-r list`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List

from ai_r.cli.shared import (
    _add_filter_group,
    _add_redact_flag,
    _AGENT_CHOICES,
    _exit_with_error,
    _format_table,
    _passes_date_filters,
    _redact_obj,
    _session_to_dict,
    _validate_date_args,
)
from ai_r.parsers import PARSERS as _PARSERS, target_agents as _target_agents


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``list`` subcommand on ``subparsers``."""
    list_p = subparsers.add_parser("list", help="List discoverable sessions")
    list_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    _add_filter_group(list_p)
    _add_redact_flag(list_p)
    list_p.set_defaults(func=_run_list)


def _run_list(args: argparse.Namespace) -> int:
    try:
        targets = _target_agents(args.agent)
        _validate_date_args(args)
    except ValueError as exc:
        return _exit_with_error(str(exc))

    summaries: List[dict[str, Any]] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        for session in parser.list_sessions():
            if _passes_date_filters(session, args):
                summaries.append(_session_to_dict(session))

    limit = getattr(args, "limit", None)
    if limit:
        summaries = summaries[:limit]

    # Emission-time redaction (F2.1): titles are session-derived text and may
    # carry pasted secrets — mask before display (mirrors MCP read_session /
    # search_sessions, which redact the emitted title by default).
    summaries = _redact_obj(summaries, args)

    if args.json:
        json.dump(summaries, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not summaries:
        print("(no sessions found)", file=sys.stderr)
        return 0
    print(_format_table(summaries))
    return 0
