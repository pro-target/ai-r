"""Runtime agent detection from environment variables.

Provides a small cascade for answering "which AI agent am I running
inside?" without requiring callers to plumb the answer through
explicit configuration.  Used by the ``ai-r detect-agent`` CLI
subcommand and as a default for tools that want to scope themselves
to a single host.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from ai_r.parsers.models import AgentName

__all__ = ["detect_agent", "detect_agent_strict"]


_NAMED_VARS: Tuple[Tuple[str, str], ...] = (
    ("AGENT_NAME", "AGENT_NAME"),
    ("AI_AGENT", "AI_AGENT"),
    ("CODING_AGENT", "CODING_AGENT"),
)


def _coerce_named(value: str) -> Optional[AgentName]:
    key = (value or "").strip().lower()
    if not key:
        return None
    for member in AgentName:
        if member.value.lower() == key:
            return member
    return None


def _detect_agent_with_source() -> Tuple[Optional[AgentName], Optional[str]]:
    for var, source in _NAMED_VARS:
        raw = os.environ.get(var)
        if not raw:
            continue
        agent = _coerce_named(raw)
        if agent is not None:
            return agent, source
    if os.environ.get("CODEX_HOME"):
        return AgentName.CODEX, "CODEX_HOME"
    if os.environ.get("CLAUDECODE"):
        return AgentName.CLAUDE, "CLAUDECODE"
    if os.environ.get("OPENCODE"):
        return AgentName.OPENCODE, "OPENCODE"
    return None, None


def detect_agent() -> Optional[AgentName]:
    """Return the agent detected from env vars, or None if no signal."""
    return _detect_agent_with_source()[0]


def detect_agent_strict() -> AgentName:
    """Like :func:`detect_agent` but raise if no signal is present."""
    agent = detect_agent()
    if agent is None:
        raise RuntimeError(
            "could not detect current agent; set AGENT_NAME, AI_AGENT, "
            "CODING_AGENT, CODEX_HOME, CLAUDECODE or OPENCODE"
        )
    return agent
