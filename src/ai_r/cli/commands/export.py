"""``ai-r export rounds`` subcommand handler."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional

from ai_r.cli.shared import resolve_session


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

    output = getattr(args, "output", None)
    if output:
        Path(output).write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0
