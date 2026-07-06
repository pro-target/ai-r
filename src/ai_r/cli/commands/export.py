"""``ai-r export rounds`` subcommand handler."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional

from ai_r.cli.shared import (
    _add_redact_flag,
    _AGENT_CHOICES,
    _redact_str,
    resolve_session,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``export`` subcommand (and its ``rounds`` format)."""
    export_p = subparsers.add_parser(
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
    _add_redact_flag(rounds_p)
    rounds_p.set_defaults(func=_run_export_rounds)


def _run_export_rounds(args: argparse.Namespace) -> int:
    resolved = resolve_session(args)
    if isinstance(resolved, int):
        return resolved
    agent_name, parser, session = resolved
    uuid = session.uuid

    messages: Optional[List[Any]] = None
    if args.include_round:
        read_messages = getattr(parser, "read_messages", None)
        if read_messages is None:
            print(
                f"ai-r: read_messages unavailable for {agent_name.value}",
                file=sys.stderr,
            )
        else:
            try:
                messages = list(read_messages(uuid))
            except Exception as exc:  # noqa: BLE001
                print(
                    f"ai-r: failed to read messages: {exc}",
                    file=sys.stderr,
                )
                messages = []

    from ai_r.exporters.rounds import session_to_rounds

    markdown = session_to_rounds(session, messages=messages)
    # Emission-time redaction (F2.1): the rendered markdown carries the session
    # title and message-derived text (goal / open / next actions) — mask
    # secrets before it is written to stdout or a file.
    markdown = _redact_str(markdown, args)

    output = getattr(args, "output", None)
    if output:
        Path(output).write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0
