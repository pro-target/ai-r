"""``ai-r locate`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List

from ai_r.cli.shared import _AGENT_CHOICES, _add_redact_flag, _exit_with_error


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``locate`` subcommand on ``subparsers``."""
    from ai_r.locate import DEFAULT_LIMIT

    lc_p = subparsers.add_parser(
        "locate",
        help=(
            "Find a session across all agents by uuid, id-prefix or "
            "case-insensitive title substring: where it lives + the "
            "ready-to-run read/resume commands."
        ),
    )
    lc_p.add_argument(
        "needle",
        help="Full uuid, id prefix (e.g. the 8-hex head), or title substring.",
    )
    lc_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    lc_p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"Max matches shown, mtime desc (0 = all; default: {DEFAULT_LIMIT}).",
    )
    lc_p.add_argument(
        "--web",
        action="store_true",
        help=(
            "Also list web sessions KNOWN LOCALLY (v1 honest scope): "
            "hook-export files under $SW_HOME/web-sessions and "
            "~/.claude.json teleport stubs (id known, content NOT local)."
        ),
    )
    lc_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable list.",
    )
    _add_redact_flag(lc_p)
    lc_p.set_defaults(func=_run_locate)


def _human(result: dict[str, Any]) -> str:
    lines: List[str] = []
    count = result.get("count", 0)
    matches = result.get("matches") or []
    lines.append(
        f"{count} match(es)"
        + (f", showing {len(matches)}" if result.get("truncated") else "")
    )
    for rec in matches:
        lines += [
            "",
            f"{rec.get('uuid')}  [{rec.get('agent')}]  {rec.get('date')}",
            f"  title:    {rec.get('title')}",
            f"  path:     {rec.get('path')} "
            f"({rec.get('size_bytes')} bytes, {rec.get('message_count')} msgs, "
            f"{'readable' if rec.get('readable') else 'NOT readable locally'})",
            f"  dir:      {rec.get('project_dir')}",
            f"  read:     {rec.get('read_command')}",
            f"  resume:   {rec.get('resume_command')}",
        ]
    if count == 0:
        suggestions = result.get("suggestions") or []
        if suggestions:
            lines.append("closest titles:")
            for title in suggestions:
                lines.append(f"  - {title}")
        else:
            lines.append("(no close titles either)")
    web = result.get("web")
    if isinstance(web, dict):
        exports = web.get("exports") or []
        stubs = web.get("stubs") or []
        lines += ["", f"web (local traces only): {len(exports)} export(s), "
                  f"{len(stubs)} teleport stub(s)"]
        for rec in exports:
            lines.append(
                f"  export: {rec.get('path')} ({rec.get('size_bytes')} bytes, "
                f"mtime {rec.get('mtime')})"
            )
        for rec in stubs:
            lines.append(
                f"  stub:   {rec.get('uuid')} in {rec.get('project_dir')} "
                f"(content NOT local)"
            )
        lines.append(f"  note: {web.get('scope_note')}")
    return "\n".join(lines)


def _run_locate(args: argparse.Namespace) -> int:
    """Run the ``locate`` subcommand.

    Delegates to :func:`ai_r.locate.locate` (the same core the MCP ``locate``
    preset uses) and renders a human-readable list or (``--json``) the dict.
    """
    from ai_r.locate import locate as _core

    try:
        result = _core(
            args.needle,
            agent=args.agent,
            limit=args.limit,
            web=args.web,
            redact=args.redact,
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    print(_human(result))
    return 0
