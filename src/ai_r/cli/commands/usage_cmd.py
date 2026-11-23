"""``ai-r usage`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys

from ai_r.cli.shared import _AGENT_CHOICES, _exit_with_error


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``usage`` subcommand on ``subparsers``."""
    u_p = subparsers.add_parser(
        "usage",
        help=(
            "Count sessions mentioning a needle vs tool calls using it "
            "(cross-agent by default)."
        ),
    )
    u_p.add_argument(
        "needle",
        help=(
            "Entity to measure: a skill name, a path fragment, a term. "
            "Mentions match the ordinary search syntax (AND words, "
            '"quoted phrases", -exclusions); uses match the needle as a '
            "literal substring of the tool input."
        ),
    )
    u_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    u_p.add_argument(
        "--project",
        default=None,
        metavar="SUBSTR",
        help=(
            "Keep sessions whose project directory contains SUBSTR "
            "(case-insensitive)."
        ),
    )
    u_p.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Keep sessions dated within the last N days (vs. now).",
    )
    u_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable summary.",
    )
    u_p.set_defaults(func=_run_usage)


def _run_usage(args: argparse.Namespace) -> int:
    """Run the ``usage`` subcommand.

    Delegates the rollup to :mod:`ai_r.usage` (the composition of the
    ``search_sessions`` and ``find_tool_calls`` cores) and renders either
    a human-readable summary or a JSON blob.
    """
    from ai_r.usage import usage as _usage_core

    try:
        result = _usage_core(
            args.needle,
            agent=args.agent,
            project=args.project,
            days=args.days,
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    print(
        f"{result['needle']} | {result['mentions']} mention(s) | "
        f"{result['uses']} use(s)"
    )
    if args.agent is None:
        for agent in sorted(result["by_agent"]):
            row = result["by_agent"][agent]
            print(
                f"  {agent:<12} {row['sessions']} session(s)  "
                f"{row['mentions']} mention(s)  {row['uses']} use(s)"
            )
    return 0
