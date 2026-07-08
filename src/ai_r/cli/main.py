"""Top-level CLI: ``build_parser`` and ``main``.

``build_parser`` wires the top-level parser and delegates each subcommand's
argparse setup to the matching module in :mod:`ai_r.cli.commands`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from ai_r import __version__
from ai_r.cli.commands import (
    detect_agent,
    detect_session,
    export,
    file_frequency,
    find_file_edits,
    find_tool_calls,
    list_cmd,
    read_cmd,
    search_cmd,
    stats_cmd,
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
    file_frequency.register(sub)
    stats_cmd.register(sub)
    detect_agent.register(sub)
    detect_session.register(sub)
    export.register(sub)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Error contract: the CLI never leaks a Python traceback to a consumer
    script.  Expected failures (bad arguments, missing sessions, …) are
    handled inside each subcommand (``ai-r: <message>`` on stderr +
    non-zero exit).  Anything *unexpected* that escapes a handler is
    caught HERE and emitted as a single structured JSON line on stderr
    (``{"error": "internal_error", "type": ..., "message": ...}``) with
    exit code 1 — so a consumer script sees a parseable error and a
    non-zero status instead of a stack dump.  Set ``AI_R_DEBUG=1`` to
    re-raise and see the full traceback while debugging.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 1
    try:
        return func(args)
    except KeyboardInterrupt:
        print("ai-r: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Downstream pipe (e.g. ``| head``) closed early — not our error.
        # 128+SIGPIPE, the conventional shell status for a broken pipe.
        return 141
    except Exception as exc:  # noqa: BLE001 — last-resort traceback guard
        if os.environ.get("AI_R_DEBUG"):
            raise
        payload = {
            "error": "internal_error",
            "type": type(exc).__name__,
            "message": str(exc),
            "hint": "set AI_R_DEBUG=1 to see the full traceback",
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
