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
        ``{"session_id": str|None, "agent": str|None, "model": str|None,
        "resume_command": str|None, "candidates": [...], "verified": bool,
        "self": bool}`` where:

        * ``session_id`` / ``agent`` describe the FIRST (highest-priority)
          candidate — the same one the CLI's default ``list`` mode returns.
        * ``model`` is the model of the current session — the LAST
          assistant ``model`` recorded in its transcript (the runtime
          environment itself carries no model signal, so this is a
          transcript read of the detected session).  ``None`` when no
          session/agent was detected, the transcript is unreadable, or
          the format records no model — absence is honest.
        * ``resume_command`` is the ready-to-run shell one-liner that
          reopens the detected session in its agent's CLI (F2.2, SSOT
          :mod:`ai_r.resume`) — ``None`` when identity is incomplete,
          the session is not in the store, or no real command exists;
          text only, never executed.
        * ``liveness`` is the detected session's *process* verdict, layered on
          top of A3 recency (SSOT :mod:`ai_r.liveness`): ``fresh`` / ``paused``
          / ``zombie`` / ``dead``, or ``None`` when there is no pid signal.
          Only Claude exposes a pid registry (``claude agents --json``), so
          this is ``None`` for every other agent and whenever identity is
          incomplete — honest absence, never guessed.
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
        "model": _current_session_model(session_id, agent_str),
        "resume_command": _current_session_resume(session_id, agent_str),
        "liveness": _current_session_liveness(session_id, agent_str),
        "candidates": candidate_dicts,
        "verified": first.verified if first is not None else False,
        "self": first.is_self if first is not None else False,
    }


def _current_session_model(
    session_id: Optional[str], agent: Optional[str]
) -> Optional[str]:
    """The LAST assistant ``model`` of the detected session's transcript.

    The runtime environment (env vars / flag files) records no model, so
    the current model is read from the detected session's own transcript —
    the most recent assistant message that carries one.  Honest ``None``
    when identity is incomplete, the session is unreadable, or the format
    records no model signal (never guessed).
    """
    if not session_id or not agent:
        return None
    from ai_r.parsers import PARSERS, coerce_agent

    try:
        parser = PARSERS[coerce_agent(agent)]
        messages = parser.read_messages(session_id)
    except (FileNotFoundError, ValueError, OSError, KeyError):
        return None
    for msg in reversed(messages):
        model = getattr(msg, "model", None)
        if getattr(msg, "role", None) == "assistant" \
                and isinstance(model, str) and model:
            return model
    return None


def _current_session_resume(
    session_id: Optional[str], agent: Optional[str]
) -> Optional[str]:
    """The detected session's ``resume_command`` (F2.2), or ``None``.

    Reuses the single SSOT :func:`ai_r.resume.resume_command` on the
    session summary the agent's parser already builds — no second
    command-construction mechanism.  Honest ``None`` when identity is
    incomplete, the session is not in the store, or no real resume
    command exists for the agent (never guessed).
    """
    if not session_id or not agent:
        return None
    from ai_r.parsers import PARSERS, coerce_agent
    from ai_r.resume import resume_command

    try:
        parser = PARSERS[coerce_agent(agent)]
        sessions = parser.list_sessions()
    except (FileNotFoundError, ValueError, OSError, KeyError):
        return None
    for session in sessions:
        if session.uuid == session_id:
            return resume_command(session)
    return None


def _current_session_liveness(
    session_id: Optional[str], agent: Optional[str]
) -> Optional[str]:
    """Process-liveness of the detected current session (SSOT :mod:`ai_r.liveness`).

    Only Claude exposes a pid registry (``claude agents --json``), so this is
    ``None`` for every other agent and whenever identity is incomplete.  The
    pid snapshot is consulted first: a session absent from the live registry
    has no pid signal, so we short-circuit to ``None`` without paying for the
    recency scan.  Otherwise the session's A3 recency splits ``fresh`` vs.
    ``paused`` — honest ``None`` when the signal is missing, never guessed.
    """
    if not session_id or agent != "claude":
        return None
    from ai_r.liveness import claude_agents_pid_index, resolve_session_liveness

    pid_index = claude_agents_pid_index()
    if session_id not in pid_index:
        return None
    activity = _current_session_activity(session_id, agent)
    return resolve_session_liveness(session_id, pid_index, activity)


def _current_session_activity(
    session_id: str, agent: str
) -> Optional[str]:
    """The A3 recency label (``fresh`` / ``stale``) of the detected session.

    Reads the session's last-activity date from its parser and classifies it
    against the current clock (SSOT :mod:`ai_r.activity`).  ``None`` when the
    session cannot be located or read — absence is honest.
    """
    from datetime import datetime, timezone

    from ai_r.activity import session_activity, stall_seconds
    from ai_r.parsers import PARSERS, coerce_agent

    try:
        parser = PARSERS[coerce_agent(agent)]
        sessions = parser.list_sessions()
    except (ValueError, OSError, KeyError):
        return None
    for session in sessions:
        if session.uuid == session_id:
            now = datetime.now(timezone.utc)
            return session_activity(session.date, now, stall_seconds())[
                "activity"
            ]
    return None
