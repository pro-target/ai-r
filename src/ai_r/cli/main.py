"""Top-level CLI: ``build_parser`` and ``main``.

``build_parser`` wires the top-level parser and delegates each subcommand's
argparse setup to the matching module in :mod:`ai_r.cli.commands`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from ai_r import __version__
from ai_r.cli.commands import (
    detect_agent,
    detect_session,
    export,
    find_file_edits,
    find_tool_calls,
    list_cmd,
    read_cmd,
    search_cmd,
)


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser.

    The top-level parser and the shared ``--version`` flag live here; each
    subcommand's argparse wiring is delegated to that subcommand's module
    via its ``register(subparsers)`` function. Ordering is significant
    (it drives ``--help`` output) and is kept identical to the previous
    monolithic implementation.
    """
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

    list_cmd.register(sub)
    read_cmd.register(sub)
    search_cmd.register(sub)
    find_file_edits.register(sub)
    find_tool_calls.register(sub)
    detect_agent.register(sub)
    detect_session.register(sub)
    export.register(sub)

    return parser


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
