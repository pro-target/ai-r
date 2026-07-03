"""Phase-3a verb: ``detect_current`` — runtime identity from env/fs.

``detect_current`` is NOT a session-query — it answers "who am I RIGHT NOW?"
from the runtime environment (env vars + per-session flag files).  It is a
thin re-export/composition of the existing ``ai_r.agents.detect_agent`` +
``ai_r.session.detect_session_candidates`` cascade (the same logic behind the
``ai-r detect-agent`` / ``ai-r detect-session`` CLI subcommands), reshaped
into a single ``{session_id, agent, candidates, verified, self}`` dict.

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

from typing import Any, List, Optional


def detect_current(agent: Optional[str] = None) -> dict[str, Any]:
    """Return the current runtime identity (session + agent) from env/fs.

    NOT a session-query — this reads the runtime environment (env vars +
    per-session flag files), reusing the exact cascade behind the
    ``ai-r detect-agent`` / ``ai-r detect-session`` CLI subcommands:
    :func:`ai_r.agents.detect_agent` for the agent and
    :func:`ai_r.session.detect_session_candidates` for the session id(s).

    Args:
        agent: Optional hint; accepted for API symmetry with the CLI's
            deprecated ``--agent`` flag.  The cascade scans every agent
            regardless, so this only overrides the reported ``agent`` when the
            session cascade yields no agent context.

    Returns:
        ``{"session_id": str|None, "agent": str|None, "candidates": [...],
        "verified": bool, "self": bool}`` where:

        * ``session_id`` / ``agent`` describe the FIRST (highest-priority)
          candidate — the same one the CLI's default ``list`` mode returns.
        * ``candidates`` is the full cascade (each ``{id, agent, source,
          verified, self, fingerprint}``), so a caller can disambiguate.
        * ``verified`` / ``self`` mirror the first candidate's flags.
    """
    from ai_r.agents import detect_agent as _detect_agent
    from ai_r.parsers import coerce_agent as _coerce_agent
    from ai_r.session import detect_session_candidates as _detect_candidates

    hint: Optional[str] = None
    if agent:
        # Validate the hint the same way the CLI does; an unknown agent is a
        # caller error, surfaced as ValueError (MCP wrapper → error dict).
        hint = _coerce_agent(agent).value.lower()

    candidates = _detect_candidates()
    candidate_dicts: List[dict[str, Any]] = [
        {
            "id": c.session_id,
            "agent": c.agent.value.lower() if c.agent is not None else "",
            "source": c.source,
            "verified": c.verified,
            "self": c.is_self,
            "fingerprint": c.fingerprint if c.fingerprint is not None else "",
        }
        for c in candidates
    ]

    env_agent = _detect_agent()
    env_agent_str = env_agent.value.lower() if env_agent is not None else None

    first = candidates[0] if candidates else None
    session_id = first.session_id if first is not None else None
    # Agent of record: the first candidate's agent, else the env-detected
    # agent, else the caller's hint.
    if first is not None and first.agent is not None:
        agent_str: Optional[str] = first.agent.value.lower()
    elif env_agent_str is not None:
        agent_str = env_agent_str
    else:
        agent_str = hint

    return {
        "session_id": session_id,
        "agent": agent_str,
        "candidates": candidate_dicts,
        "verified": first.verified if first is not None else False,
        "self": first.is_self if first is not None else False,
    }
