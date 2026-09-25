"""``ai-r get-body`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ai_r.cli.shared import _add_redact_flag, _exit_with_error, _UUID_PATTERN


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``get-body`` subcommand on ``subparsers``."""
    from ai_r.events.plan import _BODY_CHARS_CAP

    gb_p = subparsers.add_parser(
        "get-body",
        help=(
            "Print the on-demand body for an event/plan id "
            "(<uuid>:N message index or <uuid>:pfN plan feedback)."
        ),
    )
    gb_p.add_argument(
        "id",
        help=(
            "Event id ``<session-uuid>:<N>`` (e.g. the 8-hex head + index) "
            "or plan-feedback ref ``<session-uuid>:pf<N>`` — the same ids "
            "``query``/``find_file_edits``/``plan`` emit."
        ),
    )
    gb_p.add_argument(
        "--shallow",
        action="store_true",
        help=(
            "Plans only: return just the final plan of the id's task, "
            "dropping superseded draft bodies."
        ),
    )
    gb_p.add_argument(
        "--max-chars",
        dest="max_chars",
        type=int,
        default=_BODY_CHARS_CAP,
        metavar="N",
        help=(
            f"Cap the returned body/text, in characters (default "
            f"{_BODY_CHARS_CAP}; 0 = unlimited). A cut field is sliced "
            "with a \u2026[truncated] marker and body_truncated: true."
        ),
    )
    gb_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the full body dict as JSON instead of the body text.",
    )
    _add_redact_flag(gb_p)
    gb_p.set_defaults(func=_run_get_body)


def _validate_event_id(value: str) -> str:
    """Light validation: ``<uuid>:N`` or ``<uuid>:pfN`` (same ids as MCP)."""
    if not value or ":" not in value:
        raise ValueError(
            f"invalid event id {value!r}: expected <session-uuid>:N "
            "or <session-uuid>:pfN"
        )
    session_part, _, tail = value.rpartition(":")
    if not _UUID_PATTERN.match(session_part):
        raise ValueError(
            f"invalid event id {value!r}: session part {session_part!r} "
            "must be 1-128 chars of [A-Za-z0-9_.-]"
        )
    if tail.startswith("pf"):
        tail = tail[2:]
    if not tail.isdigit():
        raise ValueError(
            f"invalid event id {value!r}: index part must be N or pfN"
        )
    return value


def _render(body: dict[str, Any]) -> str:
    """Human-readable render: the primary body field, raw."""
    for field in ("text", "body"):
        val = body.get(field)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False, indent=2)
    steps = body.get("steps")
    if isinstance(steps, list) and steps:
        return "\n".join(
            f"{i}. {s.get('step', s) if isinstance(s, dict) else s}"
            for i, s in enumerate(steps, start=1)
        )
    return "(no body)"


def _run_get_body(args: argparse.Namespace) -> int:
    """Run the ``get-body`` subcommand.

    Delegates to :func:`ai_r.events.get_body` (the same resolver the MCP
    ``get_body`` tool uses) and prints the body — the CLI bridge auditor
    subagents call through Bash when no MCP client is available.
    """
    from ai_r.events import get_body as _core

    try:
        event_id = _validate_event_id(args.id)
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    body = _core(
        event_id,
        shallow=args.shallow,
        max_chars=args.max_chars,
        redact=args.redact,
    )
    if isinstance(body, dict) and body.get("error"):
        return _exit_with_error(
            f"{body.get('error')}: {body.get('message', event_id)}",
            code=3,
        )
    if args.json:
        json.dump(body, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    print(_render(body))
    return 0
