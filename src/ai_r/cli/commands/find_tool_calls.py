"""``ai-r find-tool-calls`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys

from ai_r.cli.shared import (
    _add_redact_flag,
    _AGENT_CHOICES,
    _exit_with_error,
    _want_redact,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``find-tool-calls`` subcommand on ``subparsers``."""
    ftc_p = subparsers.add_parser(
        "find-tool-calls",
        help="Find every tool call across sessions (cross-agent by default).",
    )
    ftc_mutex = ftc_p.add_mutually_exclusive_group()
    ftc_mutex.add_argument(
        "tool_name",
        nargs="?",
        default=None,
        help=(
            "Exact tool name to match (case-insensitive). "
            "Mutually exclusive with --pattern."
        ),
    )
    ftc_mutex.add_argument(
        "--pattern",
        dest="tool_name_pattern",
        default=None,
        metavar="TOOL_NAME_PATTERN",
        help=(
            "Substring to match against tool names (case-insensitive). "
            "Mutually exclusive with the positional tool_name argument."
        ),
    )
    ftc_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    ftc_p.add_argument(
        "--session",
        default=None,
        metavar="UUID",
        help=(
            "Restrict to a single session uuid (default: all sessions). "
            "Pin this when auditing one conversation so a wide "
            "--since/--until does not pull in unrelated sessions."
        ),
    )
    ftc_p.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 lower bound (inclusive) on call timestamp.",
    )
    ftc_p.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 upper bound (inclusive) on call timestamp.",
    )
    ftc_p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum records to return. 0 = no cap (default: 100).",
    )
    ftc_p.add_argument(
        "--input-contains",
        dest="input_contains",
        default=None,
        metavar="SUBSTR",
        help="Keep only calls whose serialized input contains SUBSTR (ci).",
    )
    ftc_p.add_argument(
        "--output-contains",
        dest="output_contains",
        default=None,
        metavar="SUBSTR",
        help="Keep only calls whose output contains SUBSTR (ci).",
    )
    ftc_p.add_argument(
        "--output-excludes",
        dest="output_excludes",
        default=None,
        metavar="SUBSTR",
        help="Drop calls whose output contains SUBSTR (ci).",
    )
    ftc_err_mutex = ftc_p.add_mutually_exclusive_group()
    ftc_err_mutex.add_argument(
        "--errors-only",
        dest="errors_only",
        action="store_true",
        help="Keep only failed calls (is_error=True).",
    )
    ftc_err_mutex.add_argument(
        "--success-only",
        dest="success_only",
        action="store_true",
        help="Keep only succeeding calls (is_error=False).",
    )
    ftc_p.add_argument(
        "--output-mode",
        dest="output_mode",
        choices=("head", "tail", "smart"),
        default=None,
        help=(
            "Output truncation strategy (default: adaptive — smart on "
            "errors, head otherwise)."
        ),
    )
    ftc_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    _add_redact_flag(ftc_p)
    ftc_p.set_defaults(func=_run_find_tool_calls)


def _run_find_tool_calls(args: argparse.Namespace) -> int:
    """Run the ``find-tool-calls`` subcommand.

    Delegates the actual scan to :mod:`ai_r.find_tool_calls` (the same
    core the MCP tool uses) and renders either a human-readable summary
    or a JSON blob.
    """
    from ai_r.find_tool_calls import find_tool_calls as _ftc_core

    if args.errors_only:
        is_error = True
    elif args.success_only:
        is_error = False
    else:
        is_error = None

    try:
        result = _ftc_core(
            tool_name=args.tool_name,
            tool_name_pattern=args.tool_name_pattern,
            agent=args.agent,
            session=args.session,
            since=args.since,
            until=args.until,
            limit=args.limit,
            input_contains=args.input_contains,
            output_contains=args.output_contains,
            output_excludes=args.output_excludes,
            is_error=is_error,
            output_mode=args.output_mode,
            redact=_want_redact(args),
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    records = result["records"]

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not records:
        print("(no tool calls found)", file=sys.stderr)
        return 0

    for r in records:
        ts = r.get("timestamp") or r.get("session_date") or "?"
        print(
            f"[{ts}] {r['agent']}/{r['session_uuid'][:8]} "
            f"{r['tool']}"
        )
        if r.get("intent"):
            first = r["intent"].splitlines()[0][:120]
            print(f"    intent: {first}")
        if r.get("assistant"):
            first_assist = r["assistant"].splitlines()[0][:120]
            print(f"    assistant: {first_assist}")

    suffix = " (truncated)" if result["truncated"] else ""
    print(f"\n{result['count']} tool call(s){suffix}.")
    return 0
