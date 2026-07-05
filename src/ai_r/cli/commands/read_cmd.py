"""``ai-r read`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from ai_r.cli.shared import (
    _AGENT_CHOICES,
    _format_session_detail,
    _format_token_table,
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
    read_p.add_argument(
        "--with-tokens",
        action="store_true",
        help=(
            "Attach the per-component token estimate (user_turn / "
            "assistant_turn / thinking / plan / tool_call:<kind>)."
        ),
    )
    read_p.add_argument(
        "--include-subagents",
        action="store_true",
        help=(
            "With --with-tokens, also roll up spawned subagent children "
            "(parent + children + folded total)."
        ),
    )
    read_p.set_defaults(func=_run_read)


def _run_read(args: argparse.Namespace) -> int:
    resolved = resolve_session(args)
    if isinstance(resolved, int):
        return resolved
    agent_name, parser, session = resolved

    want_messages = bool(getattr(args, "messages", False))
    want_tokens = bool(getattr(args, "with_tokens", False))
    want_subagents = bool(getattr(args, "include_subagents", False))
    message_dicts: Optional[List[dict[str, Any]]] = None
    raw_messages: Optional[List[Any]] = None
    # ``--with-tokens`` needs the raw messages too, so read them whenever
    # either flag is set (a single parse feeds both surfaces).
    if want_messages or want_tokens:
        read_messages = getattr(parser, "read_messages", None)
        if read_messages is None:
            print(
                f"ai-r: read_messages unavailable for {agent_name.value}",
                file=sys.stderr,
            )
        else:
            try:
                raw_messages = read_messages(session.uuid)
                if want_messages:
                    message_dicts = _messages_to_dicts(raw_messages)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"ai-r: failed to read messages: {exc}",
                    file=sys.stderr,
                )

    # Per-component token estimate — ONLY via the core; the CLI never counts.
    token_block: Optional[dict[str, Any]] = None
    subagent_rollup: Optional[dict[str, Any]] = None
    if want_tokens:
        from ai_r.tokens import component_tokens

        token_block = component_tokens(raw_messages or [], agent=agent_name)
        if want_subagents:
            subagent_rollup = _subagent_rollup(session.uuid, token_block)

    if args.json:
        payload = _session_to_dict(session)
        if want_messages and message_dicts is not None:
            payload["messages"] = message_dicts
        if want_tokens:
            payload["tokens"] = token_block
            if want_subagents:
                payload["subagent_rollup"] = subagent_rollup
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(_format_session_detail(session, messages=message_dicts))
    if want_tokens:
        print()
        print(_format_token_table(token_block))
    return 0


def _subagent_rollup(
    parent_uuid: str, parent_block: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Roll up the parent + spawned children component_tokens (CLI parity).

    Reuses the same core the MCP ``read_session(include_subagents=True)``
    uses — :func:`ai_r.session_stats.children_of` + the ``aggregate``
    ``component_tokens`` fold — so the CLI never re-implements the rollup.
    """
    from ai_r.events.aggregate import aggregate as _aggregate
    from ai_r.parsers import PARSERS
    from ai_r.session_stats import children_of
    from ai_r.tokens import component_tokens

    rows: List[dict[str, Any]] = [{"component_tokens": parent_block}]
    children_out: List[dict[str, Any]] = []
    for child in children_of(parent_uuid):
        child_parser = PARSERS.get(child.agent)
        child_msgs: List[Any] = []
        if child_parser is not None:
            try:
                child_msgs = child_parser.read_messages(child.uuid)
            except Exception:  # noqa: BLE001
                child_msgs = []
        child_block = component_tokens(child_msgs, agent=child.agent)
        children_out.append({
            "uuid": child.uuid,
            "agent": child.agent.value.lower(),
            "component_tokens": child_block,
        })
        rows.append({"component_tokens": child_block})
    folded = _aggregate(rows, group_by=lambda _r: "all",
                        metrics=["component_tokens"])
    return {
        "parent": parent_block,
        "children": children_out,
        "total": folded["totals"]["component_tokens"],
    }
