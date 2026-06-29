"""``ai-r read`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from ai_r.cli.shared import (
    _AGENT_CHOICES,
    _format_session_detail,
    _messages_to_dicts,
    _session_to_dict,
    resolve_session,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``read`` subcommand on ``subparsers``."""
    read_p = subparsers.add_parser("read", help="Read a single session by uuid")
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


def _run_read(args: argparse.Namespace) -> int:
    resolved = resolve_session(args)
    if isinstance(resolved, int):
        return resolved
    agent_name, parser, session = resolved

    want_messages = bool(getattr(args, "messages", False))
    message_dicts: Optional[List[dict[str, Any]]] = None
    if want_messages:
        read_messages = getattr(parser, "read_messages", None)
        if read_messages is None:
            print(
                f"ai-r: read_messages unavailable for {agent_name.value}",
                file=sys.stderr,
            )
        else:
            try:
                raw_messages = read_messages(session.uuid)
                message_dicts = _messages_to_dicts(raw_messages)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"ai-r: failed to read messages: {exc}",
                    file=sys.stderr,
                )

    if args.json:
        payload = _session_to_dict(session)
        if want_messages and message_dicts is not None:
            payload["messages"] = message_dicts
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(_format_session_detail(session, messages=message_dicts))
    return 0
