"""CLI entry point for ai-r.

Thin command-line wrapper over the same parsers the MCP server
exposes: list, read, and search sessions for Claude, Codex,
OpenCode, Antigravity, and Pi.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Sequence

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ai_r import __version__  # noqa: E402
from ai_r.agents import _detect_agent_with_source  # noqa: E402
from ai_r.parsers import AgentName, Session  # noqa: E402
from ai_r.parsers import (  # noqa: E402
    PARSERS as _PARSERS,
    coerce_agent as _coerce_agent,
    iso as _iso,
    target_agents as _target_agents,
)
from ai_r.session import (  # noqa: E402
    AmbiguousSessionError,
    SessionCandidate,
    detect_session_candidates,
)

__all__ = ["main", "build_parser"]


_AGENT_CHOICES = tuple(a.value.lower() for a in _PARSERS.keys())


_UUID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


_TABLE_COLUMNS = ("uuid", "agent", "date", "title", "messages")


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


def _exit_with_error(message: str, code: int = 1) -> "int":
    print(f"ai-r: {message}", file=sys.stderr)
    return code


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


def _run_list(args: argparse.Namespace) -> int:
    try:
        targets = _target_agents(args.agent)
        _validate_date_args(args)
    except ValueError as exc:
        return _exit_with_error(str(exc))

    summaries: List[dict[str, Any]] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        for session in parser.list_sessions():
            if _passes_date_filters(session, args):
                summaries.append(_session_to_dict(session))

    limit = getattr(args, "limit", None)
    if limit:
        summaries = summaries[:limit]

    if args.json:
        json.dump(summaries, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not summaries:
        print("(no sessions found)", file=sys.stderr)
        return 0
    print(_format_table(summaries))
    return 0


def _run_read(args: argparse.Namespace) -> int:
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
        uuid = session.uuid
    except ValueError as exc:
        return _exit_with_error(str(exc))

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
                raw_messages = read_messages(uuid)
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


def _run_detect_agent(args: argparse.Namespace) -> int:
    agent, source = _detect_agent_with_source()
    if agent is None:
        return _exit_with_error(
            "could not detect current agent; set AGENT_NAME, AI_AGENT, "
            "CODING_AGENT, CODEX_HOME, CLAUDECODE or OPENCODE",
        )
    if args.quiet:
        print(agent.value.lower())
    else:
        print(f"agent:    {agent.value}")
        print(f"source:   {source}")
    return 0


def _format_candidate_line(cand: SessionCandidate) -> str:
    agent_str = cand.agent.value if cand.agent is not None else ""
    fp_str = cand.fingerprint if cand.fingerprint is not None else ""
    return (
        f"id={cand.session_id} agent={agent_str} source={cand.source} "
        f"verified={cand.verified} self={cand.is_self} fingerprint={fp_str}"
    )


def _candidate_to_dict(cand: SessionCandidate) -> dict[str, Any]:
    return {
        "id": cand.session_id,
        "agent": cand.agent.value if cand.agent is not None else "",
        "source": cand.source,
        "verified": cand.verified,
        "self": cand.is_self,
        "fingerprint": cand.fingerprint if cand.fingerprint is not None else "",
    }


def _pick_single(
    candidates: list[SessionCandidate], mode: str
) -> Optional[SessionCandidate]:
    if mode == "first":
        return candidates[0] if candidates else None
    if mode == "strict":
        if len(candidates) > 1:
            raise AmbiguousSessionError(
                f"ambiguous session_id: {len(candidates)} candidates"
            )
        return candidates[0] if candidates else None
    if mode == "self":
        for cand in candidates:
            if cand.is_self:
                return cand
        return None
    if mode.startswith("fingerprint:"):
        target = mode.split(":", 1)[1].strip()
        for cand in candidates:
            if cand.fingerprint == target:
                return cand
        return None
    raise ValueError(f"unknown AI_SESSION_OUTPUT mode {mode!r}")


def _resolve_output_mode() -> str:
    return (os.environ.get("AI_SESSION_OUTPUT", "list") or "list").strip().lower()


def _run_detect_session(args: argparse.Namespace) -> int:
    if getattr(args, "agent", None):
        try:
            _coerce_agent(args.agent)
        except ValueError as exc:
            return _exit_with_error(str(exc))
    candidates = detect_session_candidates()
    if getattr(args, "count", False):
        print(len(candidates))
        return 0
    if getattr(args, "json", False):
        json.dump(
            [_candidate_to_dict(c) for c in candidates],
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0
    mode = _resolve_output_mode()
    if mode == "list":
        if len(candidates) > 1:
            print(
                "ai-r: WARN: multiple session_id candidates; pass "
                "AI_SESSION_OUTPUT=strict|self|fingerprint:<hash> for "
                "disambiguation.",
                file=sys.stderr,
            )
        if not candidates:
            return _exit_with_error("could not detect current session id")
        for cand in candidates:
            print(_format_candidate_line(cand))
        return 0
    try:
        picked = _pick_single(candidates, mode)
    except AmbiguousSessionError as exc:
        return _exit_with_error(str(exc), code=2)
    except ValueError as exc:
        return _exit_with_error(str(exc))
    if picked is None:
        return _exit_with_error("could not detect current session id")
    print(f"session={picked.session_id}")
    return 0


def _run_search(args: argparse.Namespace) -> int:
    """Run the ``search`` subcommand.

    New behaviour: scope/operator delegation to ``mcp_server.search_sessions``
    is the single source of truth; this wrapper only adds CLI-side validation
    and date filtering on top.
    """
    query = (args.query or "").strip()
    if not query:
        return _exit_with_error("search query must be non-empty")

    scope = getattr(args, "scope", "title")
    if scope not in ("title", "body", "all"):
        return _exit_with_error(
            f"unknown --scope {scope!r}; expected title, body, or all"
        )

    operator_raw = (getattr(args, "operator", "and") or "and").lower()
    if operator_raw not in ("and", "or", "not"):
        return _exit_with_error(
            f"unknown --operator {operator_raw!r}; expected and, or, or not"
        )
    operator = operator_raw.upper()

    limit = getattr(args, "limit", None)
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        return _exit_with_error(
            f"--limit must be a non-negative integer, got {limit!r}"
        )

    try:
        _target_agents(args.agent)
        _validate_date_args(args)
    except ValueError as exc:
        return _exit_with_error(str(exc))

    from ai_r import mcp_server as _mcp

    # Delegate the actual search to mcp_server (single source of truth for
    # query parsing, scope matching, operator combination). We pass
    # limit=0 so we can apply date filters first and then trim ourselves.
    raw = _mcp.search_sessions(
        query=query,
        agent=args.agent,
        scope=scope,
        operator=operator,
        limit=0,
    )

    if raw and isinstance(raw[0], dict) and raw[0].get("error") == "invalid_argument":
        return _exit_with_error(raw[0].get("message", "invalid argument"))

    filtered: List[dict[str, Any]] = []
    for summary in raw:
        try:
            sess = Session(
                uuid=summary["uuid"],
                agent=AgentName(summary["agent"]),
                title=summary.get("title", ""),
                date=datetime.fromisoformat(summary["date"].rstrip("Z")),
                path="",
                message_count=summary.get("message_count", 0),
            )
        except (KeyError, ValueError):
            continue
        if _passes_date_filters(sess, args):
            filtered.append(summary)

    if limit:
        filtered = filtered[:limit]

    if args.json:
        json.dump(filtered, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not filtered:
        print(f"(no sessions match {query!r})", file=sys.stderr)
        return 0
    print(_format_table(filtered))
    return 0


def _run_find_file_edits(args: argparse.Namespace) -> int:
    """Run the ``find-file-edits`` subcommand.

    Delegates the actual scan to :mod:`ai_r.find_file_edits`
    (the same core the MCP tool uses) and renders either a
    human-readable summary or a JSON blob.
    """
    from ai_r.find_file_edits import find_file_edits as _ffe_core

    try:
        result = _ffe_core(
            path=args.path,
            agent=args.agent,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    records = result["records"]

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not records:
        print("(no edits found)", file=sys.stderr)
        return 0

    for r in records:
        ts = r.get("timestamp") or r.get("session_date") or "?"
        print(
            f"[{ts}] {r['agent']}/{r['session_uuid'][:8]} "
            f"{r['tool']} {r['file']}"
        )
        if r.get("intent"):
            first = r["intent"].splitlines()[0][:120]
            print(f"    intent: {first}")

    suffix = " (truncated)" if result["truncated"] else ""
    print(f"\n{result['count']} edit(s){suffix}.")
    return 0


def _run_export_rounds(args: argparse.Namespace) -> int:
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
        uuid = session.uuid
    except ValueError as exc:
        return _exit_with_error(str(exc))

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


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
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

    list_p = sub.add_parser("list", help="List discoverable sessions")
    list_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    _add_filter_group(list_p)
    list_p.set_defaults(func=_run_list)

    read_p = sub.add_parser("read", help="Read a single session by uuid")
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

    search_p = sub.add_parser("search", help="Case-insensitive title search")
    search_p.add_argument("query", help="Substring to search for in session titles.")
    search_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    search_p.add_argument(
        "--scope",
        default="title",
        help="Where to search: title (default, backward-compat), body (message text + tool calls), or all (title OR body).",
    )
    search_p.add_argument(
        "--operator",
        "--op",
        dest="operator",
        default="and",
        help="How to combine terms: and (default), or, or not. Negative prefix: '-term' is always excluded.",
    )
    search_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    _add_filter_group(search_p)
    search_p.set_defaults(func=_run_search)

    ffe_p = sub.add_parser(
        "find-file-edits",
        help="Find every file edit across sessions (cross-agent by default).",
    )
    ffe_p.add_argument(
        "path",
        help="Substring matched against file_path / notebook_path / path fields in tool input.",
    )
    ffe_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Restrict to a single agent (default: all).",
    )
    ffe_p.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 lower bound (inclusive) on edit timestamp.",
    )
    ffe_p.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 upper bound (inclusive) on edit timestamp.",
    )
    ffe_p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum records to return. 0 = no cap (default: 100).",
    )
    ffe_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table.",
    )
    ffe_p.set_defaults(func=_run_find_file_edits)

    detect_p = sub.add_parser(
        "detect-agent", help="Detect the current AI agent from env vars."
    )
    detect_p.add_argument(
        "--quiet",
        action="store_true",
        help="Print just the agent name (e.g. 'claude').",
    )
    detect_p.set_defaults(func=_run_detect_agent)

    detect_session_p = sub.add_parser(
        "detect-session",
        help="Detect the current AI session id from env vars and flag files.",
    )
    detect_session_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Deprecated hint; cascade scans all agents regardless.",
    )
    detect_session_p.add_argument(
        "--quiet",
        action="store_true",
        help="Deprecated; ignored in list mode (use AI_SESSION_OUTPUT=first).",
    )
    detect_session_p.add_argument(
        "--json",
        action="store_true",
        help="Emit all candidates as a JSON array.",
    )
    detect_session_p.add_argument(
        "--count",
        action="store_true",
        help="Emit just the integer candidate count.",
    )
    detect_session_p.set_defaults(func=_run_detect_session)

    export_p = sub.add_parser(
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
    rounds_p.set_defaults(func=_run_export_rounds)

    return parser


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 1
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
