"""``ai-r stats`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys

from ai_r.cli.shared import _AGENT_CHOICES, _exit_with_error

# Kept in step with :data:`ai_r.session_stats.GROUP_BY` (a test guards the
# sync); an explicit tuple pins the ``--help`` ordering.
_GROUP_BY_CHOICES = ("agent", "dir", "date", "kind")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``stats`` subcommand on ``subparsers``."""
    st_p = subparsers.add_parser(
        "stats",
        help=(
            "Summarise sessions grouped by agent / dir / date / kind "
            "(cross-agent), optionally with request-time token usage."
        ),
    )
    st_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    st_p.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 lower bound (inclusive) on session date.",
    )
    st_p.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 upper bound (inclusive) on session date.",
    )
    st_p.add_argument(
        "--group-by",
        choices=_GROUP_BY_CHOICES,
        default="agent",
        help="Dimension to roll sessions up by (default: agent).",
    )
    st_p.add_argument(
        "--top",
        type=int,
        default=8,
        help="How many top groups to show. 0 = all (default: 8).",
    )
    st_p.add_argument(
        "--with-tokens",
        action="store_true",
        help=(
            "Read each matched session's token usage at request time and "
            "add a folded tokens block per group (exact where the format "
            "records usage, labeled estimate otherwise). An unscoped scan "
            "over a huge corpus is refused with a self-explaining message "
            "(narrow via --agent/--since/--until)."
        ),
    )
    st_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    st_p.set_defaults(func=_run_stats)


def _run_stats(args: argparse.Namespace) -> int:
    """Run the ``stats`` subcommand.

    Delegates to :func:`ai_r.session_stats.session_stats` (the same core
    the MCP ``session_stats`` preset uses) and renders either a
    human-readable table or a JSON blob.
    """
    from ai_r.session_stats import session_stats as _stats_core

    try:
        result = _stats_core(
            agent=args.agent,
            since=args.since,
            until=args.until,
            group_by=args.group_by,
            top=args.top,
            with_tokens=args.with_tokens,
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    if "error" in result:
        # Structured refusal (the unscoped ``with_tokens`` scan guard):
        # surface the core's self-explaining message, CLI error contract.
        return _exit_with_error(result["message"], code=2)

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if result.get("warning"):
        print(f"warning: {result['warning']}", file=sys.stderr)

    if not result["groups"]:
        print("(no sessions found)", file=sys.stderr)
        return 0

    totals = result["totals"]
    print(
        f"scanned: {totals['sessions']} sessions · {totals['agents']} agents "
        f"· {totals['edits']} edits (group by: {result['group_by']})"
    )
    print()
    header = (
        f"{'group':<40} {'sessions':>8} {'edits':>5} "
        f"{'intents':>7} {'messages':>8}"
    )
    if args.with_tokens:
        header += f" {'tokens':>12}"
    header += "  agents"
    print(header)
    print("-" * len(header))
    for row in result["groups"]:
        agents = ",".join(row["agents"])
        line = (
            f"{row['group'][:40]:<40} {row['sessions']:>8} {row['edits']:>5} "
            f"{row['intents']:>7} {row['messages']:>8}"
        )
        if args.with_tokens:
            total = (row.get("tokens") or {}).get("total")
            line += f" {total if total is not None else '?':>12}"
        line += f"  {agents}"
        print(line)
    if "note" in result:
        print(f"\nnote: {result['note']}")
    return 0
