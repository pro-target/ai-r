"""Shared helpers and constants for the CLI subcommands.

Everything here is module-private (leading underscore) and meant for
intra-package use only.  Kept in one module so the command modules stay
small and the resolution/formatting logic has a single home.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

from ai_r.parsers import AgentName, Session
from ai_r.parsers import (
    PARSERS as _PARSERS,
    iso as _iso,
    target_agents as _target_agents,
)


_AGENT_CHOICES = tuple(a.value.lower() for a in _PARSERS.keys())


_UUID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


_TABLE_COLUMNS = ("uuid", "agent", "date", "title", "messages")


# ---------------------------------------------------------------------------
# Exit helper
# ---------------------------------------------------------------------------


def _exit_with_error(message: str, code: int = 1) -> int:
    print(f"ai-r: {message}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_uuid(value: str) -> str:
    if not value or not _UUID_PATTERN.match(value):
        raise ValueError(
            f"invalid uuid {value!r}: must be 1-128 chars of [A-Za-z0-9_.-]"
        )
    return value


def _parse_date(value: str, field: str) -> datetime:
    """Parse a YYYY-MM-DD string to a naive datetime at 00:00."""
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"invalid {field} {value!r}: expected YYYY-MM-DD") from exc


def _validate_date_args(args: argparse.Namespace) -> None:
    """Eagerly validate ``--from-date``/``--to-date`` strings.

    Raises ``ValueError`` (with a field-tagged message) on bad input so
    the caller can route it through :func:`_exit_with_error`.
    """
    from_raw = getattr(args, "from_date", None)
    if from_raw:
        _parse_date(from_raw, "--from-date")
    to_raw = getattr(args, "to_date", None)
    if to_raw:
        _parse_date(to_raw, "--to-date")


def _passes_date_filters(session: Session, args: argparse.Namespace) -> bool:
    """Return True if ``session.date`` survives the date flags on ``args``.

    ``--days``, ``--from-date`` and ``--to-date`` combine with AND
    semantics.  Sessions whose ``date`` is timezone-aware are compared
    against naive filters by dropping the tzinfo (parsers store naive
    timestamps in practice).
    """
    date = session.date
    if date.tzinfo is not None:
        date = date.replace(tzinfo=None)

    days = getattr(args, "days", None)
    if days:
        cutoff = datetime.now() - timedelta(days=days)
        if date < cutoff:
            return False

    from_raw = getattr(args, "from_date", None)
    if from_raw:
        if date < _parse_date(from_raw, "--from-date"):
            return False

    to_raw = getattr(args, "to_date", None)
    if to_raw:
        if date > _parse_date(to_raw, "--to-date").replace(
            hour=23, minute=59, second=59
        ):
            return False

    return True


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_table(rows: Sequence[dict[str, Any]]) -> str:
    """Render a list of session summaries as a fixed-width table."""
    headers = {
        "uuid": "UUID",
        "agent": "AGENT",
        "date": "DATE",
        "title": "TITLE",
        "messages": "MSGS",
    }
    widths = {
        "uuid": 36,
        "agent": 10,
        "date": 20,
        "title": 0,
        "messages": 5,
    }
    lines: List[str] = []
    header_line = (
        f"{headers['uuid']:<{widths['uuid']}} "
        f"{headers['agent']:<{widths['agent']}} "
        f"{headers['date']:<{widths['date']}} "
        f"{headers['title']} "
        f"{headers['messages']:>{widths['messages']}}"
    )
    lines.append(header_line)
    lines.append("-" * len(header_line))
    for row in rows:
        title = row.get("title", "") or ""
        if len(title) > 80:
            title = title[:77] + "..."
        lines.append(
            f"{row.get('uuid', ''):<{widths['uuid']}} "
            f"{row.get('agent', ''):<{widths['agent']}} "
            f"{row.get('date', ''):<{widths['date']}} "
            f"{title} "
            f"{int(row.get('message_count', 0)):>{widths['messages']}d}"
        )
    return "\n".join(lines)


def _format_token_table(block: Optional[dict[str, Any]]) -> str:
    """Render a :func:`ai_r.tokens.component_tokens` block as a fixed table.

    Columns ``COMPONENT | TOKENS | SOURCE``.  Rows: the scalar components
    (``user_turn`` / ``assistant_turn`` / ``thinking`` / ``plan``), then one
    ``tool_call:<kind>`` row per kind (sorted), then a ``total`` row.  The
    ``SOURCE`` column echoes ``estimate (<estimator>)``.  A ``None`` block
    (empty transcript, nothing to measure) renders ``no token data``.
    """
    if not isinstance(block, dict):
        return "no token data"
    source = block.get("source") or "?"
    estimator = block.get("estimator")
    src_label = f"{source} ({estimator})" if estimator else str(source)

    rows: List[Tuple[str, int, str]] = []
    for field in ("user_turn", "assistant_turn", "thinking", "plan"):
        val = block.get(field)
        if isinstance(val, int) and not isinstance(val, bool):
            rows.append((field, val, src_label))
    tool_call = block.get("tool_call")
    if isinstance(tool_call, dict):
        for kind in sorted(tool_call):
            val = tool_call[kind]
            if isinstance(val, int) and not isinstance(val, bool):
                rows.append((f"tool_call:{kind}", val, src_label))
    total = block.get("total")
    rows.append(("total", int(total) if isinstance(total, int) else 0, src_label))

    comp_w = max([len("COMPONENT")] + [len(r[0]) for r in rows])
    tok_w = max([len("TOKENS")] + [len(str(r[1])) for r in rows])
    header = (
        f"{'COMPONENT':<{comp_w}}  {'TOKENS':>{tok_w}}  {'SOURCE'}"
    )
    lines: List[str] = [header, "-" * len(header)]
    for name, count, src in rows:
        lines.append(f"{name:<{comp_w}}  {count:>{tok_w}d}  {src}")
    return "\n".join(lines)


def _format_session_detail(
    session: Session, messages: Optional[List[dict[str, Any]]] = None
) -> str:
    """Render a single session in human-readable form."""
    lines: List[str] = []
    lines.append(f"UUID:      {session.uuid}")
    lines.append(f"Agent:     {session.agent.value}")
    lines.append(f"Title:     {session.title}")
    lines.append(f"Date:      {_iso(session.date)}")
    lines.append(f"Path:      {session.path}")
    lines.append(f"Messages:  {session.message_count}")
    if messages is not None:
        lines.append("")
        if not messages:
            lines.append("(no messages extracted)")
        else:
            for idx, msg in enumerate(messages, start=1):
                role = msg.get("role", "?")
                content = msg.get("text", msg.get("content", "")) or ""
                if len(content) > 400:
                    content = content[:397] + "..."
                lines.append(f"--- [{idx}] {role} ---")
                lines.append(content)
                tool_names = msg.get("tool_use") or []
                if tool_names:
                    lines.append(f"[tool_use: {', '.join(tool_names)}]")
    return "\n".join(lines)


def _session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "uuid": session.uuid,
        "agent": session.agent.value,
        "title": session.title,
        "date": _iso(session.date),
        "message_count": session.message_count,
    }


def _messages_to_dicts(messages: Sequence[Any]) -> List[dict[str, Any]]:
    """Flatten :class:`Message` objects to plain dicts for display/JSON."""
    out: List[dict[str, Any]] = []
    for msg in messages:
        tool_use = msg.tool_use or ()
        names = [t.get("name", "?") for t in tool_use if isinstance(t, dict)]
        out.append(
            {
                "role": msg.role,
                "text": msg.text or "",
                "tool_use": names,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Session lookup helpers (prefix / exact match resolution)
# ---------------------------------------------------------------------------


def _session_prefix_keys(session: Session) -> set[str]:
    keys = {session.uuid}
    path_stem = Path(session.path).stem
    if path_stem:
        keys.add(path_stem)
    return keys


def _find_prefix_matches(
    agent_name: AgentName, parser: Any, uuid: str
) -> List[tuple[AgentName, Any, Session]]:
    matches: List[tuple[AgentName, Any, Session]] = []
    for session in parser.list_sessions():
        if any(key.startswith(uuid) for key in _session_prefix_keys(session)):
            matches.append((agent_name, parser, session))
    return matches


def _format_candidate(session: Session) -> str:
    return f"{session.uuid} ({session.agent.value}, {session.path})"


def _read_exact_matches(
    targets: Sequence[AgentName], uuid: str
) -> List[tuple[AgentName, Any, Session]]:
    matches: List[tuple[AgentName, Any, Session]] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        try:
            matches.append((agent_name, parser, parser.read_session(uuid)))
        except FileNotFoundError:
            continue
    return matches


def _add_filter_group(parser: argparse.ArgumentParser) -> None:
    """Attach the shared result-limiting/date filter flags to ``parser``."""
    grp = parser.add_argument_group("filtering")
    grp.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Truncate the result table to N rows (after filtering).",
    )
    grp.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Keep sessions within the last N days (vs. now).",
    )
    grp.add_argument(
        "--from-date",
        dest="from_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Keep sessions dated on/after this date.",
    )
    grp.add_argument(
        "--to-date",
        dest="to_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Keep sessions dated on/before this date (end of day).",
    )
    grp.add_argument(
        "--all",
        action="store_true",
        help="No-op: listing already defaults to all agents.",
    )


def resolve_session(
    args: argparse.Namespace,
) -> Union[Tuple[AgentName, Any, Session], int]:
    """Resolve a uuid (possibly a unique prefix) to a single session.

    Shared by ``read`` and ``export rounds``.  Returns either a
    ``(agent_name, parser, session)`` tuple on success or an integer
    exit code on failure (caller returns it directly).
    """
    try:
        uuid = _validate_uuid(args.uuid)
        targets = _target_agents(args.agent)
    except ValueError as exc:
        return _exit_with_error(str(exc))

    try:
        matches = _read_exact_matches(targets, uuid)
        if not matches:
            for agent_name in targets:
                parser = _PARSERS[agent_name]
                matches.extend(_find_prefix_matches(agent_name, parser, uuid))
        if not matches:
            scope = args.agent or "any supported agent"
            return _exit_with_error(
                f"not found: session {uuid!r} under {scope}",
                code=3,
            )
        if len(matches) > 1:
            candidates = "\n".join(
                f"  - {_format_candidate(match[2])}" for match in matches[:20]
            )
            more = (
                ""
                if len(matches) <= 20
                else f"\n  ... and {len(matches) - 20} more"
            )
            return _exit_with_error(
                f"ambiguous session prefix {uuid!r}; candidates:\n{candidates}{more}",
                code=2,
            )
        agent_name, parser, session = matches[0]
        return agent_name, parser, session
    except ValueError as exc:
        return _exit_with_error(str(exc))
