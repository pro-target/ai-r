"""Shared data models for session parsers.

All parser modules return :class:`Session` instances conforming to this
schema.  Adding a new agent is a three-step operation:

1. Add a value to :class:`AgentName`.
2. Implement a parser module under this package exporting the four
   standard functions (``list_sessions``, ``read_session``,
   ``search``, ``session_exists``).
3. Re-export the new module from :mod:`ai_r.parsers`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple


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
        parent_uuid: Parent (spawner) session uuid for spawned
            sub-sessions:
            Claude — the spawner session, taken from the ``subagents/``
            wrapper folder name, else the ``sessionId`` of the inline
            sidechain records (the message-level ``parentUuid`` is a
            message uuid, not a session, and is NOT used),
            OpenCode (``session.parent_id``), Codex
            (``session_meta.payload.parent_thread_id`` or the nested
            ``source.subagent.thread_spawn.parent_thread_id``), Pi
            (the ``parentSession`` header field).  ``None`` for top-level
            sessions and for Antigravity (no parent signal in the format).
        kind: ``"agent"`` for a normal top-level session, ``"subagent"``
            for a spawned subagent (sidechain) session.  Defaults to
            ``"agent"``.  Detected for Claude, OpenCode, Codex and Pi;
            Antigravity has no subagent signal and always reports
            ``"agent"``.  Kept consistent with ``parent_uuid`` (a session
            with a parent is a subagent); the noise criterion lives in
            :mod:`ai_r.parsers._noise`.
        project_dir: Absolute path of the project directory the session
            ran in, when the format records one; ``None`` when the
            format carries no signal (never fabricated).  Sources:
            Claude — the record-level ``cwd`` of the CLI transcript
            (fallback: Desktop metadata ``cwd``/``originCwd``, then a
            filesystem-verified decode of the ``projects/<slug>``
            storage encoding); Codex — ``session_meta.payload.cwd``;
            OpenCode — the ``session.directory`` column (absent on old
            schemas → ``None``); Pi — the session-header ``cwd``.
            Antigravity has no per-session directory signal → always
            ``None``.
        launch_surface: The surface the session was driven from, when
            the format makes it distinguishable; ``None`` when it does
            not (never fabricated).  Values: Claude —
            ``"claude-cli"`` | ``"claude-desktop"`` (from the F1.3
            Desktop-overlay ``source_root`` signal); Codex — the raw
            ``session_meta.payload.originator`` string (e.g.
            ``"codex_vscode"``, ``"Codex Desktop"``), passed through
            verbatim, no invented taxonomy; Antigravity —
            ``"antigravity-ide"`` | ``"antigravity-cli"`` (from which
            brain root holds the session).  OpenCode and Pi carry no
            launch-surface signal → always ``None``.
        models: Unique model identifiers observed in the session, in
            order of first appearance — the session-level rollup of the
            per-message :attr:`Message.model` signal.  Sources: Claude —
            assistant-record ``message.model`` (the ``<synthetic>``
            placeholder is not a model and is skipped); Codex — the
            ``turn_context`` records' ``model``; OpenCode — assistant
            ``message.data.modelID``; Pi — assistant ``message.model``.
            Antigravity records no structured model signal → always
            empty.  Empty when the format carries no signal (never
            fabricated).
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
    kind: str = "agent"
    project_dir: Optional[str] = None
    launch_surface: Optional[str] = None
    models: Tuple[str, ...] = ()
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
            May additionally carry ``"tool_use_id": str`` when the source
            format exposes a stable call id (Claude ``tool_use.id``,
            OpenCode ``callID``) so the event layer can correlate the
            call with its result; absent otherwise.
        tool_result: Tuple of ``{"content": str, "is_error": bool}`` dicts
            for tool return values.  ``is_error`` is ``True`` when the
            agent flagged the call as failed.  It is a *real* signal for
            Claude (the ``tool_result.is_error`` flag, else derived from a
            ``<tool_use_error>`` content prefix or a record-level
            ``toolUseResult: "Error: …"``) and OpenCode
            (``state.status == "error"``); for Codex, Antigravity and Pi
            no per-result error flag exists in the source records, so it
            is best-effort and defaults to ``False`` there.  May also carry
            ``"tool_use_id": str`` mirroring the ``tool_use`` id when the
            format exposes one, enabling call↔result correlation.
        qa: Tuple of ``{"question": str, "options": tuple[str, ...],
            "answer": str}`` dicts capturing the user's reply to an
            interactive agent question (Claude ``AskUserQuestion``,
            Codex ``request_user_input``, OpenCode ``question``).  Each
            entry pairs the *question text* with the *answer the user
            chose* so a downstream reader never sees a bare "option B"
            without the question it answered.  ``options`` lists the
            offered choices (may be empty when the format omits them);
            ``answer`` is the chosen label(s) joined by ``" | "`` for
            multi-select.  Empty when the message carries no answered
            question, so existing consumers are unaffected.
        thinking: Model reasoning text, where the format marks it as
            such: Claude ``thinking`` content blocks (``redacted_thinking``
            carries no plaintext and is skipped), Codex ``reasoning``
            response items (the plaintext ``summary`` — the
            ``encrypted_content`` blob is opaque), OpenCode ``reasoning``
            parts, Pi ``thinking`` blocks.  Antigravity has no marked
            reasoning signal → always ``""``.  Kept out of :attr:`text`
            so narrative and reasoning are never conflated (nor
            double-counted by token estimates).
        tokens: The format's own per-assistant-message usage, normalized
            to ``{"input", "output", "reasoning", "cache_read",
            "cache_write", "total"}`` (same shape as the session-level
            ``read_token_usage`` blocks; a counter the format does not
            record is ``None``).  ``None`` where the format records no
            per-message usage — Codex (cumulative session counter only),
            Antigravity (no usage at all), user messages — absence is
            honest, never fabricated.  Not part of the equality contract.
        model: The model identifier that produced this message, where the
            format records one on assistant output: Claude — the assistant
            record's ``message.model`` (the ``<synthetic>`` placeholder is
            NOT a model and maps to ``None``); Codex — the ``model`` of the
            most recent preceding ``turn_context`` record; OpenCode — the
            assistant ``message.data.modelID``; Pi — the assistant
            ``message.model``.  ``None`` for user/tool messages and where
            the format carries no signal (Antigravity) — absence is
            honest, never fabricated.
    """

    role: str
    text: str
    tool_use: Tuple[dict, ...] = ()
    tool_result: Tuple[dict, ...] = ()
    timestamp: Optional[datetime] = None
    qa: Tuple[dict, ...] = ()
    thinking: str = ""
    tokens: Optional[dict] = field(default=None, compare=False)
    model: Optional[str] = None


__all__ = ["AgentName", "Message", "Session"]
