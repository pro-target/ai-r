"""``ai-r audit-brief`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List

from ai_r.cli.shared import _AGENT_CHOICES, _add_redact_flag, _exit_with_error


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``audit-brief`` subcommand on ``subparsers``."""
    from ai_r.audit_brief import DEFAULT_BUDGET_CHARS

    ab_p = subparsers.add_parser(
        "audit-brief",
        help=(
            "Token-lean session digest for auditors: user turns VERBATIM + "
            "plans/decisions + tool & file footprint + token breakdown, "
            "inside a hard character budget."
        ),
    )
    ab_p.add_argument("uuid", help="Session uuid (any supported agent).")
    ab_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Optional agent hint (default: resolve across all agents).",
    )
    ab_p.add_argument(
        "--budget-chars",
        dest="budget_chars",
        type=int,
        default=DEFAULT_BUDGET_CHARS,
        metavar="N",
        help=(
            "Hard budget on the serialized digest, in characters "
            f"(default: {DEFAULT_BUDGET_CHARS}; 0 = unlimited). Detail is "
            "dropped in a fixed ladder (tool errors → file list → plan "
            "bodies); user turns are NEVER truncated."
        ),
    )
    ab_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the digest dict as JSON instead of markdown sections.",
    )
    _add_redact_flag(ab_p)
    ab_p.set_defaults(func=_run_audit_brief)


def _md(brief: dict[str, Any]) -> str:
    """Render the digest dict as structured markdown sections."""
    sess = brief.get("session") or {}
    lines: List[str] = [
        f"# Audit brief — {sess.get('uuid')} ({sess.get('agent')})",
        "",
        "## Session",
        f"- title: {sess.get('title')}",
        f"- date: {sess.get('date')}",
        f"- project_dir: {sess.get('project_dir')}",
        f"- messages: {sess.get('message_count')} · models: "
        f"{', '.join(sess.get('models') or []) or '(none)'}",
        f"- path: {sess.get('path')}",
        f"- resume: {sess.get('resume_command')}",
        "",
        f"## User turns ({brief.get('user_turns_count')}, verbatim)",
    ]
    for i, turn in enumerate(brief.get("user_turns") or [], start=1):
        lines.append("")
        lines.append(f"### [{i}] {turn.get('ts')} ({turn.get('id')})")
        lines.append(turn.get("text") or "(empty)")

    plans = brief.get("plans") or {}
    lines += ["", f"## Plans / decisions ({plans.get('count')} atoms, "
              f"{plans.get('feedback_count')} feedback pairs)"]
    for atom in plans.get("tasks") or []:
        lines.append(
            f"- [{atom.get('kind')} v{atom.get('version')}] "
            f"{atom.get('title')} ({atom.get('id')})"
        )
        if atom.get("body"):
            lines += ["", "```", str(atom["body"]), "```"]
        elif atom.get("body_dropped"):
            lines.append("  (body dropped for budget — get_body on the id)")
    for pair in plans.get("feedback") or []:
        lines.append(
            f"- feedback r{pair.get('round')} [{pair.get('verdict')}] "
            f"quote: {pair.get('quote')!r} → {pair.get('comment')!r}"
        )

    tools = brief.get("tools") or {}
    by_kind = tools.get("by_kind") or {}
    kinds = " · ".join(f"{k}: {v}" for k, v in by_kind.items()) or "(none)"
    lines += [
        "",
        f"## Tool footprint ({tools.get('total')} calls, "
        f"{tools.get('errors_count')} errors)",
        f"- by kind: {kinds}",
    ]
    for err in tools.get("errors") or []:
        lines.append(
            f"- ERROR {err.get('tool')} [{err.get('tool_kind')}] "
            f"at {err.get('ts')} ({err.get('id')})"
        )
    if tools.get("errors_dropped"):
        lines.append("- (error details dropped for budget)")

    files = brief.get("files") or {}
    lines += ["", f"## Files touched ({files.get('count')})"]
    for rec in files.get("edited") or []:
        lines.append(f"- {rec.get('file')} ({rec.get('edits')} edits)")
    if files.get("edited_dropped"):
        lines.append("- (file list dropped for budget)")

    tokens = brief.get("tokens") or {}
    lines += [
        "",
        "## Tokens",
        f"- total: {tokens.get('total')} (source: {tokens.get('source')})",
    ]
    comp = brief.get("component_tokens") or {}
    if comp:
        scalars = " · ".join(
            f"{k}: {comp.get(k)}"
            for k in ("user_turn", "assistant_turn", "thinking", "plan")
            if isinstance(comp.get(k), int)
        )
        if scalars:
            lines.append(f"- components: {scalars}")

    budget = brief.get("budget") or {}
    lines += [
        "",
        "## Budget",
        f"- used {budget.get('used_chars')} / {budget.get('budget_chars')} "
        f"chars · dropped: {', '.join(budget.get('dropped') or []) or '(nothing)'}"
        f" · over_budget: {budget.get('over_budget')}",
    ]
    if budget.get("note"):
        lines.append(f"- note: {budget['note']}")
    return "\n".join(lines)


def _run_audit_brief(args: argparse.Namespace) -> int:
    """Run the ``audit-brief`` subcommand.

    Delegates to :func:`ai_r.audit_brief.audit_brief` (the same core the MCP
    ``audit_brief`` preset uses) and renders markdown sections or (``--json``)
    the digest dict.
    """
    from ai_r.audit_brief import audit_brief as _core

    try:
        brief = _core(
            args.uuid,
            agent=args.agent,
            budget_chars=args.budget_chars,
            redact=args.redact,
        )
    except FileNotFoundError as exc:
        return _exit_with_error(str(exc), code=3)
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    if args.json:
        json.dump(brief, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    print(_md(brief))
    return 0
