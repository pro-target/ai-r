"""Top-level CLI: ``build_parser``, ``main`` and the shared filter group."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from ai_r import __version__
from ai_r.cli.commands.detect_agent import _run_detect_agent
from ai_r.cli.commands.detect_session import _run_detect_session
from ai_r.cli.commands.export import _run_export_rounds
from ai_r.cli.commands.find_file_edits import _run_find_file_edits
from ai_r.cli.commands.list_cmd import _run_list
from ai_r.cli.commands.read_cmd import _run_read
from ai_r.cli.commands.search_cmd import _run_search
from ai_r.cli.shared import _AGENT_CHOICES


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="ai-r",
        description=(
            "Inspect Claude, Codex, OpenCode, Antigravity and Pi sessions."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ai-r {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", help="List discoverable sessions")
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
    list_p.set_defaults(func=_run_list)

    read_p = sub.add_parser("read", help="Read a single session by uuid")
    read_p.add_argument("uuid", help="Session uuid (validated against [A-Za-z0-9_.-]).")
    read_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Which agent owns the session (default: try all).",
    )
    read_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable dump.",
    )
    read_p.add_argument(
        "--messages",
        action="store_true",
        help="Also dump the session's messages (truncated text + tool names).",
    )
    read_p.set_defaults(func=_run_read)

    search_p = sub.add_parser("search", help="Case-insensitive title search")
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
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    _add_filter_group(search_p)
    search_p.set_defaults(func=_run_search)

    ffe_p = sub.add_parser(
        "find-file-edits",
        help="Find every file edit across sessions (cross-agent by default).",
    )
    ffe_p.add_argument(
        "path",
        help="Substring matched against file_path / notebook_path / path fields in tool input.",
    )
    ffe_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    ffe_p.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 lower bound (inclusive) on edit timestamp.",
    )
    ffe_p.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 upper bound (inclusive) on edit timestamp.",
    )
    ffe_p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum records to return. 0 = no cap (default: 100).",
    )
    ffe_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    ffe_p.set_defaults(func=_run_find_file_edits)

    detect_p = sub.add_parser(
        "detect-agent", help="Detect the current AI agent from env vars."
    )
    detect_p.add_argument(
        "--quiet",
        action="store_true",
        help="Print just the agent name (e.g. 'claude').",
    )
    detect_p.set_defaults(func=_run_detect_agent)

    detect_session_p = sub.add_parser(
        "detect-session",
        help="Detect the current AI session id from env vars and flag files.",
    )
    detect_session_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Deprecated hint; cascade scans all agents regardless.",
    )
    detect_session_p.add_argument(
        "--quiet",
        action="store_true",
        help="Deprecated; ignored in list mode (use AI_SESSION_OUTPUT=first).",
    )
    detect_session_p.add_argument(
        "--json",
        action="store_true",
        help="Emit all candidates as a JSON array.",
    )
    detect_session_p.add_argument(
        "--count",
        action="store_true",
        help="Emit just the integer candidate count.",
    )
    detect_session_p.set_defaults(func=_run_detect_session)

    export_p = sub.add_parser(
        "export", help="Render a session into an external format."
    )
    export_sub = export_p.add_subparsers(dest="export_format", required=True)

    rounds_p = export_sub.add_parser(
        "rounds",
        help="Emit work/CHANGELOG.md-compatible markdown from a session.",
    )
    rounds_p.add_argument(
        "uuid", help="Session uuid (validated against [A-Za-z0-9_.-])."
    )
    rounds_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Which agent owns the session (default: try all).",
    )
    rounds_p.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Write markdown to PATH instead of stdout.",
    )
    rounds_p.add_argument(
        "--include-round",
        action="store_true",
        help="Include the structured Round block (requires read_messages).",
    )
    rounds_p.set_defaults(func=_run_export_rounds)

    return parser


def _add_filter_group(parser: argparse.ArgumentParser) -> None:
    """Attach the shared result-limiting/date filter flags to ``parser``."""
    grp = parser.add_argument_group("filtering")
    grp.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Truncate the result table to N rows (after filtering).",
    )
    grp.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Keep sessions within the last N days (vs. now).",
    )
    grp.add_argument(
        "--from-date",
        dest="from_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Keep sessions dated on/after this date.",
    )
    grp.add_argument(
        "--to-date",
        dest="to_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Keep sessions dated on/before this date (end of day).",
    )
    grp.add_argument(
        "--all",
        action="store_true",
        help="No-op: listing already defaults to all agents.",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 1
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
