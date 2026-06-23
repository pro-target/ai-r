"""Shared data models for session parsers.

All parser modules return :class:`Session` instances conforming to this
schema.  Adding a new agent is a three-step operation:

1. Add a value to :class:`AgentName`.
2. Implement a parser module under this package exporting the four
   standard functions (``list_sessions``, ``read_session``,
   ``search``, ``session_exists``).
3. Re-export the new module from :mod:`ai_reader.parsers`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple


class AgentName(str, Enum):
    """Identifier of which AI agent produced a session file."""

    CLAUDE = "CLAUDE"
    CODEX = "CODEX"
    OPENCODE = "OPENCODE"
    ANTIGRAVITY = "ANTIGRAVITY"
    PI = "PI"


@dataclass(frozen=True)
class Session:
    """A discoverable AI agent session.

    Attributes:
        uuid: Unique identifier of the session.  For Claude this is the
            ``<session-uuid>`` portion of the JSONL filename; for Codex
            this is the ``payload.id`` from ``session_meta``; for
            OpenCode this is the ``session.id`` primary key; for
            Antigravity this is the brain directory name; for Pi this
            is the ``session.id`` header field.
        agent: Which agent owns the session.
        title: Human-readable title, truncated to 100 characters and
            with newlines collapsed to spaces.
        date: Last activity timestamp.  Prefer an in-file timestamp
            when one is available, otherwise file mtime (Claude, Codex)
            or DB ``time_updated`` (OpenCode).
        path: Absolute path to the source of truth.  For JSONL parsers
            this is the file path; for OpenCode this is the SQLite
            database path; for Antigravity this is the brain directory.
        message_count: Number of conversation messages.  For Claude and
            Codex this is the number of ``user``/``assistant`` records
            read; for OpenCode this is ``SELECT COUNT(*) FROM message``;
            for Antigravity this is the number of records in the
            overview.txt / transcript.jsonl; for Pi this is the number
            of user/assistant message entries.
        parent_uuid: For OpenCode sub-sessions (``session.parent_id``).
            ``None`` for top-level sessions and for other agents.
        extra: Free-form metadata bag (project slug for Claude, cwd
            for Codex, etc.).  Optional and not part of the equality
            contract.
    """

    uuid: str
    agent: AgentName
    title: str
    date: datetime
    path: str
    message_count: int
    parent_uuid: Optional[str] = None
    extra: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Message:
    """A single conversation message extracted from a session file.

    Unlike the flat ``{role, content}`` dicts produced for MCP clients,
    :class:`Message` preserves the structured tool-call surface so audit
    consumers can answer questions like "did the agent actually run the
    tests?" by scanning ``tool_use`` entries.

    Attributes:
        role: One of ``"user"``, ``"assistant"`` or ``"tool"``.  Tool
            results emitted by some agents as standalone records use
            ``"tool"``; for agents that embed tool results inside user
            records (Claude) the role stays ``"user"`` and the result
            is exposed via :attr:`tool_result`.
        text: Concatenated plain-text content (may be ``""`` when the
            message is purely a tool call/result).
        tool_use: Tuple of ``{"name": str, "input": str}`` dicts for
            assistant tool invocations.  ``input`` is the raw tool
            input serialized to a string (JSON for structured inputs).
        tool_result: Tuple of ``{"content": str}`` dicts for tool
            return values.
    """

    role: str
    text: str
    tool_use: Tuple[dict, ...] = ()
    tool_result: Tuple[dict, ...] = ()
    timestamp: Optional[datetime] = None


__all__ = ["AgentName", "Message", "Session"]
