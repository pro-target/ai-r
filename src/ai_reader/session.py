"""Runtime session_id detection from env vars and per-session flag files.

Mirrors the cascade in the agents sh layer's ``lib/detect-session.sh``.
Used by the ``ai-reader detect-session`` CLI subcommand and by the
``session_note`` validator to re-derive the id and FAIL on mismatch.
"""
from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ai_reader.agents import detect_agent
from ai_reader.parsers.models import AgentName

__all__ = [
    "AmbiguousSessionError",
    "SessionCandidate",
    "detect_session_candidates",
    "detect_session_id",
    "detect_session_id_with_source",
    "_is_valid_session_id",
    "_session_id_matches_agent",
]


_AGENT_BY_ENV: "dict[AgentName, str]" = {
    AgentName.CLAUDE: "CLAUDE_CODE_SESSION_ID",
    AgentName.CODEX: "CODEX_THREAD_ID",
    AgentName.OPENCODE: "OPENCODE_SESSION_ID",
}


_AGENT_FLAG_NAMES: "tuple[AgentName, ...]" = (
    AgentName.CLAUDE,
    AgentName.CODEX,
    AgentName.OPENCODE,
    AgentName.ANTIGRAVITY,
    AgentName.PI,
)


_AGENT_SESSION_REGEX: "dict[AgentName, str]" = {
    AgentName.OPENCODE: r"^ses_[A-Za-z0-9]{6,}$",
    AgentName.CLAUDE: r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    AgentName.CODEX: r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
}


_SESSION_ID_CHARSET = re.compile(r"^[A-Za-z0-9_-]+$")
_MIN_SESSION_ID_LEN = 6


class AmbiguousSessionError(RuntimeError):
    """Raised by ``detect_session_id(..., mode='strict')`` on >1 candidate."""


def _is_valid_session_id(value: str) -> bool:
    """Return True when ``value`` matches the sh-layer session_id shape."""
    if not value:
        return False
    if len(value) < _MIN_SESSION_ID_LEN:
        return False
    return _SESSION_ID_CHARSET.match(value) is not None


def _session_id_matches_agent(value: str, agent: AgentName) -> bool:
    """Return True when ``value`` fits ``agent``'s known session-id shape.

    Agents without a registered pattern accept any syntactically valid id.
    """
    pattern = _AGENT_SESSION_REGEX.get(agent)
    if pattern is None:
        return True
    return re.match(pattern, value) is not None


def _identity_base() -> Path:
    """Resolve the directory holding per-agent flag files.

    Honours ``AI_READER_SESSION_IDENTITY_DIR`` (empty string is treated
    as unset and falls back to ``$HOME/.agents/.session-identity``).
    """
    override = os.environ.get("AI_READER_SESSION_IDENTITY_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".agents" / ".session-identity"


def _agent_dir(agent: AgentName) -> Path:
    return _identity_base() / agent.value.lower()


def _agent_current_flag(agent: AgentName) -> Path:
    return _agent_dir(agent) / "current"


def _read_flag(path: Path) -> Optional[str]:
    """Symlink-safe read of a flag file; returns the trimmed body or None."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    value = text.strip()
    return value or None


def _per_session_flags(agent: AgentName) -> list[str]:
    """Return ``session_id`` filenames for ``agent``'s per-session files.

    Skips ``current`` and any ``.<dotfile>`` (sidecar data).  Each
    returned name has passed :func:`_is_valid_session_id` so callers do
    not need to re-validate.  Symlinks are excluded (defense-in-depth).
    """
    base = _agent_dir(agent)
    if not base.is_dir():
        return []
    out: list[str] = []
    for entry in base.iterdir():
        if entry.name == "current":
            continue
        if entry.name.startswith("."):
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        if not _is_valid_session_id(entry.name):
            continue
        out.append(entry.name)
    return out


def _read_self(flag_dir: Path, session_id: str) -> Optional[Tuple[int, int]]:
    """Parse the ``.self`` sidecar; return ``(opencode_pid, opencode_ppid)``.

    File format (5 tab-separated fields, one line)::

        <agent>\\t<session_id>\\t<process_name>\\t<opencode_pid>\\t<opencode_ppid>

    Returns ``None`` on missing file, symlink, malformed content, or
    non-integer PIDs.
    """
    path = flag_dir / f"{session_id}.self"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    line = text.splitlines()[0] if text else ""
    parts = line.split("\t")
    if len(parts) < 5:
        return None
    try:
        pid = int(parts[3].strip())
        ppid = int(parts[4].strip())
    except ValueError:
        return None
    return pid, ppid


def _read_fingerprint(flag_dir: Path, session_id: str) -> Optional[str]:
    """Parse the ``.fingerprint`` sidecar; return sha256 prefix (8 chars).

    File format (4 tab-separated fields, one line)::

        <agent>\\t<session_id>\\t<sha256_hash>\\t<timestamp>

    Returns ``None`` on missing file, symlink, or malformed content.
    """
    path = flag_dir / f"{session_id}.fingerprint"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    line = text.splitlines()[0] if text else ""
    parts = line.split("\t")
    if len(parts) < 3:
        return None
    hash_value = parts[2].strip()
    if not hash_value:
        return None
    return hash_value[:8]


def _pid_comm_starts_with(pid: int, prefix: str) -> bool:
    """Return True when ``/proc/<pid>/comm`` exists and starts with ``prefix``.

    Linux-only; returns ``False`` on any OSError (covers non-Linux
    platforms and dead PIDs whose ``/proc`` entry has been reaped).
    """
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as fh:
            return fh.read().strip().startswith(prefix)
    except (OSError, UnicodeDecodeError):
        return False


def _is_self_session(agent: AgentName, flag_dir: Path, session_id: str) -> bool:
    """Return True when ``session_id``'s ``.self`` sidecar names a live
    opencode process in our tree.

    Only meaningful for opencode — non-opencode agents never write
    ``.self`` files, so this returns ``False`` for them.  Reads the
    ``opencode_pid`` and ``opencode_ppid`` fields; for each, checks
    that ``/proc/<pid>/comm`` is readable AND starts with ``"opencode"``.
    """
    if agent is not AgentName.OPENCODE:
        return False
    parsed = _read_self(flag_dir, session_id)
    if parsed is None:
        return False
    opencode_pid, opencode_ppid = parsed
    return _pid_comm_starts_with(opencode_pid, "opencode") or _pid_comm_starts_with(
        opencode_ppid, "opencode"
    )


def _candidate_from_flag_file(
    agent: AgentName, session_id: str, source: str
) -> Optional[SessionCandidate]:
    """Build a :class:`SessionCandidate` from a flag-file-discovered id.

    Returns ``None`` when the id fails shape validation for ``agent``.
    """
    if not _is_valid_session_id(session_id):
        return None
    if not _session_id_matches_agent(session_id, agent):
        return None
    flag_dir = _agent_dir(agent)
    is_self = _is_self_session(agent, flag_dir, session_id)
    fingerprint = _read_fingerprint(flag_dir, session_id)
    return SessionCandidate(
        session_id=session_id,
        agent=agent,
        source=source,
        verified=True,
        is_self=is_self,
        fingerprint=fingerprint,
    )


@dataclass(frozen=True)
class SessionCandidate:
    """One possible current ``session_id`` from the detection cascade.

    Attributes
    ----------
    session_id:
        The candidate id string.
    agent:
        Owning agent, or ``None`` for the universal ``AI_SESSION_ID``
        override (which carries no agent context).
    source:
        Origin label — one of:

        * ``"AI_SESSION_ID"`` (env override)
        * ``"CLAUDE_CODE_SESSION_ID"`` / ``"CODEX_THREAD_ID"`` /
          ``"OPENCODE_SESSION_ID"`` (per-agent env var)
        * ``"ts_file:<agent>"`` (per-session file in the identity dir)
        * ``"flag/<agent>"`` (legacy ``current`` pointer, deprecated)
        * ``"ai-reader-list"`` (heuristic last-resort)

    verified:
        ``True`` when the id passed charset + agent-shape validation
        AND (if it came from a flag file) the file exists on disk.
    is_self:
        ``True`` when the candidate's ``.self`` sidecar names a live
        opencode process whose ``/proc/<pid>/comm`` starts with
        ``"opencode"``.  Only set for opencode candidates with a
        ``.self`` sidecar; ``False`` for everything else.
    fingerprint:
        First 8 chars of the sha256 hash from the ``.fingerprint``
        sidecar, or ``None`` when no sidecar exists.
    """

    session_id: str
    agent: Optional[AgentName]
    source: str
    verified: bool
    is_self: bool
    fingerprint: Optional[str]


def _env_candidates() -> list[SessionCandidate]:
    """Step 1+2: emit one candidate per env var that carries a valid id.

    ``AI_SESSION_ID`` is universal (no agent cross-check).  Per-agent
    env vars cross-check against :func:`_session_id_matches_agent` —
    the candidate is dropped silently on shape mismatch.
    """
    out: list[SessionCandidate] = []
    raw = os.environ.get("AI_SESSION_ID", "").strip()
    if raw and _is_valid_session_id(raw):
        out.append(
            SessionCandidate(
                session_id=raw,
                agent=None,
                source="AI_SESSION_ID",
                verified=True,
                is_self=False,
                fingerprint=None,
            )
        )
    for agent_name, env_var in _AGENT_BY_ENV.items():
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            continue
        if not _is_valid_session_id(raw):
            continue
        if not _session_id_matches_agent(raw, agent_name):
            continue
        out.append(
            SessionCandidate(
                session_id=raw,
                agent=agent_name,
                source=env_var,
                verified=True,
                is_self=False,
                fingerprint=None,
            )
        )
    return out


def _per_session_candidates(
    priority_agent: Optional[AgentName],
) -> list[SessionCandidate]:
    """Step 3: emit one candidate per per-session flag file.

    ``priority_agent`` (env-detected agent, if any) is scanned first so
    the cascade still prefers the running agent.  Symlinks, ``current``,
    and dotfiles are skipped.  Per-agent shape validation applies.
    """
    out: list[SessionCandidate] = []
    seen: set[tuple[AgentName, str]] = set()
    order: list[AgentName] = []
    if priority_agent is not None and priority_agent in _AGENT_FLAG_NAMES:
        order.append(priority_agent)
    for agent_name in _AGENT_FLAG_NAMES:
        if agent_name not in order:
            order.append(agent_name)
    for agent_name in order:
        for session_id in _per_session_flags(agent_name):
            key = (agent_name, session_id)
            if key in seen:
                continue
            seen.add(key)
            cand = _candidate_from_flag_file(
                agent_name, session_id, f"ts_file:{agent_name.value.lower()}"
            )
            if cand is not None:
                out.append(cand)
    return out


def _current_candidates(
    priority_agent: Optional[AgentName],
) -> list[SessionCandidate]:
    """Step 4: emit one deprecated ``current`` candidate per agent.

    Emits a :class:`warnings.warn` for the set (once, not per agent) so
    callers that care about the deprecation see it in stderr / test
    capsys output.  Source label stays ``flag/<agent>`` for backward
    compat with callers that grep on the legacy label.
    """
    out: list[SessionCandidate] = []
    order: list[AgentName] = []
    if priority_agent is not None and priority_agent in _AGENT_FLAG_NAMES:
        order.append(priority_agent)
    for agent_name in _AGENT_FLAG_NAMES:
        if agent_name not in order:
            order.append(agent_name)
    for agent_name in order:
        raw = _read_flag(_agent_current_flag(agent_name))
        if raw is None:
            continue
        if not _is_valid_session_id(raw):
            continue
        if not _session_id_matches_agent(raw, agent_name):
            continue
        out.append(
            SessionCandidate(
                session_id=raw,
                agent=agent_name,
                source=f"flag/{agent_name.value.lower()}",
                verified=True,
                is_self=False,
                fingerprint=None,
            )
        )
    if out:
        warnings.warn(
            "current pointer is deprecated, use per-session files "
            "(set AI_READER_SESSION_IDENTITY_DIR for sh-layer hook)",
            DeprecationWarning,
            stacklevel=3,
        )
    return out


def _aireader_list_candidate() -> Optional[SessionCandidate]:
    """Step 5: last-resort heuristic — query ``ai-reader list``.

    Reuses :func:`detect_agent` to scope the search to a single agent;
    on non-Linux, or when the CLI is missing, the heuristic silently
    yields nothing.  Source is ``"ai-reader-list"``; ``verified`` is
    ``False`` to flag heuristic provenance to callers.
    """
    agent = detect_agent()
    if agent is None:
        return None
    try:
        from ai_reader.cli import _PARSERS  # type: ignore

        parser = _PARSERS[agent]
    except Exception:
        return None
    try:
        for session in parser.list_sessions():
            sid = session.uuid
            if sid and _is_valid_session_id(sid):
                return SessionCandidate(
                    session_id=sid,
                    agent=agent,
                    source="ai-reader-list",
                    verified=False,
                    is_self=False,
                    fingerprint=None,
                )
    except Exception:
        return None
    return None


def detect_session_candidates() -> list[SessionCandidate]:
    """Return ALL candidate session_ids, parallel-safe.

    Cascade (each step appends to the list — never short-circuits):

    1. ``AI_SESSION_ID`` env override — one candidate, ``agent=None``.
    2. Per-agent env vars — one candidate per set var.
    3. Per-session flag files in ``$HOME/.agents/.session-identity/<agent>/``
       — one candidate per valid filename; env-detected agent scanned
       first.
    4. ``current`` pointer (deprecated) — emits a :class:`DeprecationWarning`
       the first time at least one ``current`` candidate is appended.
    5. ``ai-reader list`` heuristic — at most one candidate, ``verified=False``.

    The result may be empty when nothing matches.  All candidates are
    deduplicated by ``(agent, session_id)``; env-var candidates shadow
    flag-file candidates for the same id (env vars come first in the
    list, so callers taking ``[0]`` get the env-var provenance).
    """
    out: list[SessionCandidate] = []
    out.extend(_env_candidates())

    env_agent: Optional[AgentName] = None
    for cand in out:
        if cand.agent is not None and cand.source in (
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
            "OPENCODE_SESSION_ID",
        ):
            env_agent = cand.agent
            break
    if env_agent is None:
        env_agent = detect_agent()

    out.extend(_per_session_candidates(env_agent))
    out.extend(_current_candidates(env_agent))

    # Heuristic only when no env agent was declared AND no per-session
    # or current candidate surfaced.  Mirrors the old "declared agent
    # with no flag → return None" behaviour so callers that set
    # CLAUDECODE / OPENCODE / CODEX_HOME but have no flag file are not
    # silently pointed at a heuristic guess.
    if not out and env_agent is None:
        heuristic = _aireader_list_candidate()
        if heuristic is not None:
            out.append(heuristic)

    return out


def detect_session_id(agent: Optional[AgentName] = None) -> Optional[str]:
    """Return the running session id, or None if no signal can be derived.

    Thin wrapper over :func:`detect_session_id_with_source` preserved
    for backward compatibility.  Honours the ``AI_SESSION_OUTPUT`` env
    var:

    * ``"list"`` (default) — return the first candidate; print a WARN
      to stderr when more than one candidate exists.
    * ``"first"`` — return the first candidate, no warning.
    * ``"strict"`` — return the first candidate; raise
      :class:`AmbiguousSessionError` when more than one exists.
    * ``"self"`` — return the first candidate with ``is_self=True``,
      else ``None``.
    * ``"fingerprint:<hash>"`` — return the first candidate whose
      ``fingerprint`` matches ``<hash>``, else ``None``.
    """
    return _select_from_candidates(detect_session_candidates())


def _select_from_candidates(candidates: list[SessionCandidate]) -> Optional[str]:
    mode = (os.environ.get("AI_SESSION_OUTPUT", "list") or "list").strip().lower()
    if not candidates:
        return None
    if mode == "first":
        return candidates[0].session_id
    if mode == "strict":
        if len(candidates) > 1:
            raise AmbiguousSessionError(
                f"ambiguous session_id: {len(candidates)} candidates; "
                "set AI_SESSION_OUTPUT=self or fingerprint:<hash> to disambiguate"
            )
        return candidates[0].session_id
    if mode == "self":
        for cand in candidates:
            if cand.is_self:
                return cand.session_id
        return None
    if mode.startswith("fingerprint:"):
        target = mode.split(":", 1)[1].strip()
        for cand in candidates:
            if cand.fingerprint == target:
                return cand.session_id
        return None
    if mode != "list":
        import sys

        print(
            f"ai-reader: unknown AI_SESSION_OUTPUT={mode!r}; falling back to 'list'",
            file=sys.stderr,
        )
    if len(candidates) > 1:
        import sys

        print(
            "ai-reader: WARN: multiple session_id candidates; "
            "returning first. Pass AI_SESSION_OUTPUT=strict|self|fingerprint:<hash> "
            "for disambiguation.",
            file=sys.stderr,
        )
    return candidates[0].session_id


def detect_session_id_with_source(
    agent: Optional[AgentName] = None,
) -> "Tuple[Optional[str], Optional[str], Optional[AgentName]]":
    """Return ``(session_id, source_label, agent)`` from the cascade.

    Backward-compat wrapper — returns the first candidate's tuple, or
    ``(None, None, None)`` when the cascade is empty.  Honours
    ``AI_SESSION_OUTPUT`` semantics for the same modes that
    :func:`detect_session_id` does; the legacy tuple shape means the
    "self" and "fingerprint:<hash>" modes can return ``(None, None, None)``
    when the targeted match is not present.
    """
    candidates = detect_session_candidates()
    if not candidates:
        return None, None, None
    mode = (os.environ.get("AI_SESSION_OUTPUT", "list") or "list").strip().lower()
    if mode == "self":
        for cand in candidates:
            if cand.is_self:
                return cand.session_id, cand.source, cand.agent
        return None, None, None
    if mode.startswith("fingerprint:"):
        target = mode.split(":", 1)[1].strip()
        for cand in candidates:
            if cand.fingerprint == target:
                return cand.session_id, cand.source, cand.agent
        return None, None, None
    cand = candidates[0]
    return cand.session_id, cand.source, cand.agent
