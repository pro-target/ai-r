"""Markdown Round/CHANGELOG emitter for ``work/CHANGELOG.md``.

Single public function :func:`session_to_rounds` — render a
:class:`Session` (optionally with its message stream) into a markdown
document that combines a per-session header, a one-line CHANGELOG
entry, and a structured Round block (Goal / Status / Files touched /
Decisions / Open / Next actions / Snapshot).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional, Sequence, Set

from ai_reader.parsers.models import Message, Session

__all__ = ["session_to_rounds"]


_FILE_TOOLS = frozenset(
    {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "read", "edit", "write"}
)


_PATH_KEYS = ("file_path", "notebook_path", "path")


_ROUND_TITLE_FALLBACK = "Round"


def _session_date(session: Session) -> str:
    date = session.date
    if isinstance(date, datetime):
        return date.strftime("%Y-%m-%d")
    return str(date)[:10]


def _has_tool_use(messages: Sequence[Message]) -> bool:
    return any(bool(msg.tool_use) for msg in messages)


def _extract_file_paths(messages: Sequence[Message]) -> List[str]:
    seen: Set[str] = set()
    paths: List[str] = []
    for msg in messages:
        for tool in msg.tool_use or ():
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "")
            if name not in _FILE_TOOLS:
                continue
            raw_input = tool.get("input", "")
            payload: object = raw_input
            if isinstance(raw_input, str) and raw_input.strip():
                try:
                    payload = json.loads(raw_input)
                except (ValueError, TypeError):
                    continue
            if not isinstance(payload, dict):
                continue
            for key in _PATH_KEYS:
                value = payload.get(key)
                if isinstance(value, str) and value and value not in seen:
                    seen.add(value)
                    paths.append(value)
                    break
    return paths


def _first_line(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    if len(line) > limit:
        line = line[: limit - 1] + "\u2026"
    return line


def _truncate(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        return text[: limit - 1] + "\u2026"
    return text


def _extract_decisions(messages: Sequence[Message]) -> List[str]:
    try:
        from ai_reader.parsers.claude_derive import extract_decisions  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        result = extract_decisions(messages)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(result, (list, tuple)):
        return [str(item) for item in result if item]
    return []


def _snapshot_line(session: Session) -> str:
    extra = session.extra or {}
    for key in ("tokens", "token_count", "total_tokens"):
        value = extra.get(key)
        if isinstance(value, (int, float)) and value:
            return f"tokens: {int(value)}"
    cost = extra.get("cost")
    if isinstance(cost, (int, float)) and cost:
        return f"cost: {cost}"
    return "n/a"


def _render_round(
    session: Session, messages: Sequence[Message]
) -> List[str]:
    first_user = next((m for m in messages if m.role == "user"), None)
    goal = _first_line(first_user.text) if first_user else ""

    last = messages[-1] if messages else None
    open_text = _truncate(last.text) if last is not None and last.role == "user" else ""

    last_assistant = next(
        (m for m in reversed(messages) if m.role == "assistant"), None
    )
    next_actions = _truncate(last_assistant.text) if last_assistant else ""

    status = "completed" if last is not None and last.role == "assistant" else "in-progress"

    file_paths = _extract_file_paths(messages)
    decisions = _extract_decisions(messages)
    snapshot = _snapshot_line(session)

    title = session.title or _ROUND_TITLE_FALLBACK
    lines: List[str] = [f"## Round: {title}", ""]
    lines.append("### Goal")
    lines.append(goal or "n/a")
    lines.append("")
    lines.append("### Status")
    lines.append(status)
    lines.append("")
    lines.append("### Files touched")
    if file_paths:
        for path in file_paths:
            lines.append(f"- `{path}`")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("### Decisions")
    if decisions:
        for item in decisions:
            lines.append(f"- {item}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("### Open")
    lines.append(open_text or "n/a")
    lines.append("")
    lines.append("### Next actions")
    lines.append(next_actions or "n/a")
    lines.append("")
    lines.append("### Snapshot")
    lines.append(snapshot)
    return lines


def session_to_rounds(
    session: Session, messages: Optional[Sequence[Message]] = None
) -> str:
    """Render a :class:`Session` as a ``work/CHANGELOG.md``-compatible doc."""
    date = _session_date(session)
    title = session.title or "(untitled)"
    agent = session.agent.value

    non_actionable = messages is None or (
        len(messages) < 5 or not _has_tool_use(messages)
    )
    marker = " [non-actionable]" if non_actionable else ""

    lines: List[str] = [
        f"# Session: {title}",
        "",
        f"**UUID**: {session.uuid} \u00b7 **Agent**: {agent} \u00b7 **Date**: {date}",
        "",
        f"- {date} \u2014 {agent} \u2014 {title}{marker}",
        "",
    ]

    if messages is not None:
        lines.extend(_render_round(session, messages))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
