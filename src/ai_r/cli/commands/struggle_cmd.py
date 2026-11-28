"""``ai-r struggle`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Optional

from ai_r.cli.shared import _AGENT_CHOICES, _exit_with_error
from ai_r.struggle import STRUGGLE_KINDS, struggle


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``struggle`` subcommand on ``subparsers``."""
    s_p = subparsers.add_parser(
        "struggle",
        help=(
            "Detect agent struggle signatures: silent deaths (sd) and "
            "quota deaths (qd), cross-agent by default."
        ),
    )
    s_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        default="zcode",
        help="Agent store to scan (v1 adapter: zcode only).",
    )
    s_p.add_argument(
        "--session",
        default=None,
        metavar="UUID",
        help="Pin a single session uuid.",
    )
    s_p.add_argument(
        "--since",
        default=None,
        metavar="ISO",
        help="Only turns started at/after this ISO timestamp (UTC).",
    )
    s_p.add_argument(
        "--until",
        default=None,
        metavar="ISO",
        help="Only turns started before this ISO timestamp (UTC).",
    )
    s_p.add_argument(
        "--kinds",
        default=",".join(STRUGGLE_KINDS),
        metavar="LIST",
        help=(
            "Comma-separated struggle kinds to detect "
            f"(default: all of {','.join(STRUGGLE_KINDS)})."
        ),
    )
    s_p.add_argument(
        "--top",
        type=int,
        default=20,
        metavar="N",
        help="Cap records per kind (default: 20; 0 = unlimited).",
    )
    s_p.add_argument(
        "--loose-markers",
        action="store_true",
        help=(
            "Admit loose quota markers (substrings on tool outputs); "
            "known to catch skill/docs quotes."
        ),
    )
    s_p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Override the ZCode sqlite DB path (tests, offline copies).",
    )
    s_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    s_p.set_defaults(func=_run_struggle)


def _parse_iso(value: str, option: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(_exit_with_error(
            f"invalid {option} timestamp {value!r}: expected ISO-8601", code=2
        )) from exc


def _run_struggle(args: argparse.Namespace) -> int:
    """Run the ``struggle`` subcommand.

    Delegates detection to :func:`ai_r.struggle.struggle` and renders a
    ``session | kind | severity | evidence | ts`` table, or a JSON array
    with the alert-ready record shape.
    """
    since: Optional[datetime] = (
        _parse_iso(args.since, "--since") if args.since else None
    )
    until: Optional[datetime] = (
        _parse_iso(args.until, "--until") if args.until else None
    )
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]

    try:
        records = struggle(
            agent=args.agent,
            db=args.db,
            session=args.session,
            since=since,
            until=until,
            kinds=kinds,
            top=args.top,
            loose_markers=args.loose_markers,
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    if args.json:
        json.dump(records, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not records:
        print("no struggle signatures found")
        return 0
    for rec in records:
        print(
            f"{rec['session_id']} | {rec['kind']} | {rec['severity']} | "
            f"{rec['evidence']} | {rec['ts']}"
        )
    return 0
