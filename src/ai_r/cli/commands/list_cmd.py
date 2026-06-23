"""``ai-r list`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List

from ai_r.cli.shared import (
    _exit_with_error,
    _format_table,
    _passes_date_filters,
    _session_to_dict,
    _validate_date_args,
)
from ai_r.parsers import PARSERS as _PARSERS, target_agents as _target_agents


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

    if args.json:
        json.dump(summaries, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not summaries:
        print("(no sessions found)", file=sys.stderr)
        return 0
    print(_format_table(summaries))
    return 0
