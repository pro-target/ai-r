"""``ai-r file-frequency`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys

from ai_r.cli.shared import _AGENT_CHOICES, _exit_with_error


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``file-frequency`` subcommand on ``subparsers``."""
    ff_p = subparsers.add_parser(
        "file-frequency",
        help=(
            "Rank edited files by how much attention they got "
            "(edits / sessions / distinct requests / agents), cross-agent."
        ),
    )
    ff_p.add_argument(
        "--path",
        default="/",
        help=(
            "Substring matched against edited file paths. Default '/' matches "
            "absolute paths; pass e.g. 'src/' to scope, or '.' to also catch "
            "relative paths."
        ),
    )
    ff_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    ff_p.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 lower bound (inclusive) on edit timestamp.",
    )
    ff_p.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 upper bound (inclusive) on edit timestamp.",
    )
    ff_p.add_argument(
        "--top",
        type=int,
        default=8,
        help="How many top files to show. 0 = all (default: 8).",
    )
    ff_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    ff_p.set_defaults(func=_run_file_frequency)


def _run_file_frequency(args: argparse.Namespace) -> int:
    """Run the ``file-frequency`` subcommand.

    Delegates the aggregation to :mod:`ai_r.file_frequency` (a thin
    group-by over the same ``find_file_edits`` core the MCP tool uses)
    and renders either a human-readable table or a JSON blob.
    """
    from ai_r.file_frequency import file_frequency as _ff_core

    try:
        result = _ff_core(
            path=args.path,
            agent=args.agent,
            since=args.since,
            until=args.until,
            top=args.top,
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not result["files"]:
        print("(no edits found)", file=sys.stderr)
        return 0

    print(
        f"scanned: {result['total_sessions']} sessions with edits "
        f"· {result['total_agents']} agents · {result['total_edits']} edits "
        f"· {result['total_files']} distinct files "
        f"(path filter: {args.path!r})"
    )
    print()
    header = f"{'file':<48} {'edits':>5} {'sessions':>8} {'intents':>7}  agents"
    print(header)
    print("-" * len(header))
    for row in result["files"]:
        agents = ",".join(row["agents"])
        print(
            f"{row['file'][:48]:<48} {row['edits']:>5} "
            f"{row['sessions']:>8} {row['intents']:>7}  {agents}"
        )
    return 0
