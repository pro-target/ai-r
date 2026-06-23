"""Session parsers for Claude, Codex, OpenCode, Antigravity, and Pi.

Each parser module exports the same four-function interface:

* :func:`list_sessions` — enumerate every session visible to the agent.
* :func:`read_session`  — load a single session by uuid; raises
  :class:`FileNotFoundError` if it is missing.
* :func:`search`        — case-insensitive title substring search.
* :func:`session_exists` — boolean existence check.

All public data flows through :class:`ai_r.parsers.models.Session`
and :class:`ai_r.parsers.models.AgentName`.

Path resolution
---------------

Every parser accepts an optional ``base_dir`` argument for tests and
also honours the ``AI_R_HOME`` environment variable, which is
treated as the user's ``$HOME`` for the duration of the call.  This
is the *only* testing hook — do not add other side effects.  When
``AI_R_HOME`` is unset, parsers fall back to ``~``.

Cross-agent helpers
-------------------

The package-level :func:`find_sessions` and :func:`read_session` are
cross-agent dispatchers layered on top of the per-agent modules.  They
are convenience entry points only; the per-agent functions remain the
authoritative API.

Modules:
    claude:       ``~/.claude/projects/<project-slug>/<uuid>.jsonl``
    codex:        ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
    opencode:     SQLite at ``~/.local/share/opencode/opencode.db`` and
                  snap variants under ``~/snap/code/*/...`` /
                  ``~/snap/opencode/*/...``.
    antigravity:  brain directories under
                  ``~/.gemini/antigravity/brain`` and
                  ``~/.gemini/antigravity-cli/brain``.
    pi:           ``~/.pi/agent/sessions/<encoded-cwd>/*.jsonl``.
"""

from datetime import datetime
from typing import Dict, List, Optional, Protocol, Union

from . import antigravity, claude, codex, opencode, pi
from .models import AgentName, Message, Session


class ParserModule(Protocol):
    def list_sessions(self, base_dir: str | None = None) -> list[Session]: ...

    def read_session(
        self, uuid: str, base_dir: str | None = None
    ) -> Session: ...

    def read_messages(
        self, uuid: str, base_dir: str | None = None
    ) -> list[Message]: ...

    def search(self, query: str, base_dir: str | None = None) -> list[Session]: ...

    def session_exists(self, uuid: str, base_dir: str | None = None) -> bool: ...

__all__ = [
    "AgentName",
    "Message",
    "Session",
    "ParserModule",
    "antigravity",
    "claude",
    "codex",
    "opencode",
    "pi",
    "find_sessions",
    "read_session",
    # Canonical cross-agent registry + helpers (re-exported by
    # ``ai_r.find_file_edits`` and used by ``ai_r.cli`` /
    # ``ai_r.mcp_server``).  These are the SINGLE source of truth;
    # the legacy private aliases below delegate to them.
    "PARSERS",
    "coerce_agent",
    "target_agents",
    "iso",
]


# Canonical agent registry — the single source of truth for the mapping
# between :class:`AgentName` values and their parser modules.  Both
# ``ai_r.find_file_edits`` and ``ai_r.cli`` import this dict
# (and the helpers below) rather than keeping their own copies.
PARSERS: Dict[AgentName, ParserModule] = {
    AgentName.CLAUDE: claude,
    AgentName.CODEX: codex,
    AgentName.OPENCODE: opencode,
    AgentName.ANTIGRAVITY: antigravity,
    AgentName.PI: pi,
}


def coerce_agent(name: str) -> AgentName:
    """Map a lowercase agent name to :class:`AgentName`."""
    key = (name or "").strip().lower()
    for agent_name in PARSERS:
        if agent_name.value.lower() == key:
            return agent_name
    raise ValueError(
        f"unknown agent {name!r}; expected one of "
        f"{sorted(a.value.lower() for a in PARSERS)}"
    )


def target_agents(agent: Optional[str]) -> List[AgentName]:
    """Resolve the optional ``agent`` filter to a list of :class:`AgentName`."""
    if agent is None or not str(agent).strip():
        return list(PARSERS.keys())
    return [coerce_agent(agent)]


def iso(date: datetime) -> str:
    """Format a datetime as ISO-8601 with UTC fallback."""
    if date.tzinfo is None:
        return date.isoformat() + "Z"
    return date.isoformat()


# Legacy private aliases kept so the in-package dispatchers below can keep
# their historical names without churn; they delegate to the canonical
# implementations above.
_PARSERS = PARSERS
_iso = iso


def _candidate(session: Session) -> dict:
    return {
        "uuid": session.uuid,
        "agent": session.agent.value,
        "title": session.title,
        "mtime": _iso(session.date),
        "path": session.path,
    }


_target_agents = target_agents


def find_sessions(query: str, agent: Optional[str] = None) -> List[dict]:
    """Cross-agent title-substring search returning candidate dicts.

    Each candidate is a ``dict`` with keys ``uuid``, ``agent``, ``title``,
    ``mtime`` (ISO-8601) and ``path`` so the caller can disambiguate
    between agents.  An empty or whitespace-only ``query`` returns
    ``[]``.  When ``agent`` is omitted every supported agent is
    queried; pass a lowercase name (``"claude"``, ``"codex"``,
    ``"opencode"``, ``"antigravity"``, ``"pi"``) to restrict the scan.
    Unknown agent names raise :class:`ValueError`.
    """
    needle = (query or "").strip()
    if not needle:
        return []
    targets = _target_agents(agent)
    results: List[dict] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        for session in parser.search(query):
            results.append(_candidate(session))
    return results


def read_session(
    query: str, agent: Optional[str] = None
) -> Union[Session, List[dict]]:
    """Resolve ``query`` to a :class:`Session` or a candidate list.

    Cross-agent dispatcher that mirrors the per-agent
    :func:`read_session` signature while accepting either a uuid or a
    title substring.  Resolution order:

    1. Try an exact-uuid lookup across the targeted agents.  If
       exactly one session matches, return it.  If the same uuid is
       claimed by multiple agents, return a candidate list so the
       caller can disambiguate.
    2. Otherwise fall back to :func:`find_sessions` (title substring).
       Zero matches raises :class:`FileNotFoundError`.  One match
       returns the resolved :class:`Session`.  More than one match
       returns the candidate list.

    The return type is therefore :class:`Session` | ``list[dict]``;
    callers detect ambiguity with ``isinstance(result, list)``.
    """
    needle = (query or "").strip()
    if not needle:
        raise FileNotFoundError(f"empty query: {query!r}")

    targets = _target_agents(agent)

    exact: List[Session] = []
    for agent_name in targets:
        parser = _PARSERS[agent_name]
        try:
            exact.append(parser.read_session(query))
        except (FileNotFoundError, ValueError):
            continue
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return [_candidate(s) for s in exact]

    candidates = find_sessions(query, agent=agent)
    if not candidates:
        raise FileNotFoundError(f"session {query!r} not found")
    if len(candidates) == 1:
        match = candidates[0]
        parser = _PARSERS[AgentName(match["agent"])]
        return parser.read_session(match["uuid"])
    return candidates
