"""Session *process-liveness* classification — the OS fact ``activity`` omits.

:mod:`ai_r.activity` deliberately reports only *recency* ("nothing written for
a while"), never whether the producing process is still running — a session
*file* cannot show that (honest contract F1.1).  A supervising poller, though,
genuinely needs the missing half: is a *stale* session paused-but-alive, or
actually dead?  That verdict is not in the transcript, so it must come from an
OS signal — not from the agent's own say-so (a self-declared status could
lie; ``/proc`` cannot).

This module fuses two verifiable signals into one ``liveness`` label:

* the **pid registry** — ``claude agents --json`` maps each live Claude
  session to its process id (a live, first-party snapshot of "which agents
  the CLI considers running right now");
* cheap **``/proc`` probes** on that pid — is the process still present, and
  does it still hold open file descriptors?

The states (spec / state table):

============  ============================================================
``liveness``  meaning
============  ============================================================
``fresh``     pid present, live fds, recent activity
``paused``    pid present, live fds, but activity is stale (alive & silent)
``zombie``    pid present but its fd table is empty (defunct: no live I/O)
``dead``      the registry named a pid, but ``/proc`` no longer shows it
``None``      no pid signal at all (session not in the registry, or no
              registry snapshot) — honest absence, never fabricated
============  ============================================================

``liveness`` **complements** ``activity``; it does not replace it.  ``activity``
stays the pure recency verdict (F1.1); ``liveness`` is the process verdict
layered on top of it.  Only Claude exposes a pid registry, so non-Claude
sessions honestly report ``liveness = None`` (no signal, not a guess).

The classifier core (:func:`session_liveness`) is pure — it takes the
already-sampled signals and never reads ``/proc`` or the clock — so it stays
hermetically testable.  The impure edges (the ``claude agents`` subprocess,
``/proc`` reads) are isolated behind small seams the MCP surface samples once
per call and the tests monkeypatch.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Dict, Mapping, Optional

from ai_r.activity import FRESH as _ACT_FRESH, STALE as _ACT_STALE
from ai_r.session import _pid_comm_starts_with

__all__ = [
    "FRESH",
    "PAUSED",
    "ZOMBIE",
    "DEAD",
    "session_liveness",
    "resolve_session_liveness",
    "claude_agents_pid_index",
]

# Liveness labels.  Distinct from ``activity``'s fresh/stale: these describe
# the *process*, correlating the pid registry with ``/proc``.
FRESH = "fresh"
PAUSED = "paused"
ZOMBIE = "zombie"
DEAD = "dead"


def session_liveness(
    activity: Optional[str],
    pid_alive: Optional[bool],
    io_alive: Optional[bool] = None,
) -> Optional[str]:
    """Fuse recency + pid/io signals into one liveness label — pure.

    Args:
        activity: The A3 recency label (:data:`ai_r.activity.FRESH` /
            ``STALE``) for the session, or ``None`` when unknown.  Only
            consulted for a live, non-zombie process (to split ``fresh`` vs.
            ``paused``).
        pid_alive: Tri-state pid signal — ``True`` the process is present in
            ``/proc``; ``False`` a pid was known but ``/proc`` no longer shows
            it; ``None`` no pid was available to check at all.
        io_alive: ``True`` the process still holds open fds; ``False`` its fd
            table is empty (defunct); ``None`` the fds could not be probed.
            Only consulted when ``pid_alive`` is ``True``.

    Returns:
        One of :data:`FRESH` / :data:`PAUSED` / :data:`ZOMBIE` / :data:`DEAD`,
        or ``None`` when there is no pid signal to reason from (``pid_alive is
        None``) — honest absence, never a fabricated verdict.

    Honesty note: ``None`` means "no OS signal", not "alive" or "dead".  A
    ``dead`` label is emitted only when a concrete pid turned out to be gone,
    never merely because a session is absent from the registry.
    """
    if pid_alive is None:
        # Nothing to reason from — do not fabricate a verdict (F1.1).
        return None
    if pid_alive is False:
        # A pid was known, but /proc no longer shows the process.
        return DEAD
    # pid_alive is True from here.
    if io_alive is False:
        # Process present but its fd table is empty — defunct, no live I/O.
        return ZOMBIE
    if activity == _ACT_FRESH:
        return FRESH
    if activity == _ACT_STALE:
        return PAUSED
    # Alive but recency is unknown — cannot honestly split fresh vs. paused.
    return None


def resolve_session_liveness(
    session_id: Optional[str],
    pid_index: Optional[Mapping[str, int]],
    activity: Optional[str],
) -> Optional[str]:
    """Resolve a session's liveness from a pid snapshot + live ``/proc`` probes.

    Args:
        session_id: The session's id (its uuid), looked up in ``pid_index``.
        pid_index: A ``{session_id: pid}`` snapshot from
            :func:`claude_agents_pid_index`, or ``None`` when no snapshot was
            taken (e.g. the claude CLI is absent).
        activity: The session's A3 recency label, passed through to
            :func:`session_liveness`.

    Returns:
        The liveness label, or ``None`` when there is no pid signal for this
        session (no snapshot, blank id, or the session is not in the live
        registry).  Absence from the registry is reported as ``None``, never
        ``dead`` — the registry is not assumed exhaustive.
    """
    if pid_index is None or not session_id:
        return None
    pid = pid_index.get(session_id)
    if pid is None:
        # Not in the live registry → no pid to check → no signal.
        return None
    pid_alive = _pid_present(pid)
    io_alive = _pid_io_alive(pid) if pid_alive else None
    return session_liveness(activity, pid_alive, io_alive)


def _pid_present(pid: int) -> bool:
    """True when a process exists at ``pid`` (``/proc/<pid>`` is readable).

    Reuses :func:`ai_r.session._pid_comm_starts_with` with the empty prefix:
    every string starts with ``""``, so this is exactly "is ``/proc/<pid>/comm``
    readable" — i.e. does any process (including a not-yet-reaped defunct one)
    live at this pid.  DRY: the ``/proc`` open + error contract is not
    re-implemented here.
    """
    return _pid_comm_starts_with(pid, "")


def _pid_io_alive(pid: int) -> Optional[bool]:
    """Whether ``pid`` still holds open file descriptors.

    ``True`` when ``/proc/<pid>/fd`` lists at least one fd (a running process
    always holds some); ``False`` when the directory is present but empty — the
    kernel closes every fd when a process becomes defunct (zombie), so an empty
    fd table is the ``/proc`` signature of "process exists but its I/O is
    dead".  ``None`` when the directory cannot be read (no permission / gone) —
    no signal, so the caller must not infer a zombie.
    """
    try:
        return len(os.listdir(f"/proc/{pid}/fd")) > 0
    except OSError:
        return None


# --- claude agents --json pid registry (TTL-cached) ------------------------
#
# ``claude agents --json`` is a first-party live snapshot of running Claude
# agents ({pid, sessionId, ...}).  ``list_sessions`` must consult it once per
# call, not once per session, so the (subprocess) result is cached under a
# short TTL — long enough to serve every session in one listing from a single
# spawn, short enough that a session that dies mid-poll surfaces within a few
# seconds.  Same shape as the OpenCode DB pool's ``_CONN_TTL_SEC`` cache.
_AGENTS_TTL_SEC = 2.5
# Best-effort subprocess timeout; a wedged CLI must never hang a listing.
_AGENTS_TIMEOUT_SEC = 4.0

_agents_cache_lock = threading.Lock()
_agents_cache: Optional[tuple[float, Dict[str, int]]] = None


def claude_agents_pid_index() -> Dict[str, int]:
    """A ``{session_id: pid}`` snapshot of live Claude agents, TTL-cached.

    Best-effort: a missing ``claude`` CLI, a non-zero exit, a timeout, or
    unparseable output all yield an empty index rather than raising — the
    caller treats an empty index as "no pid signal" and reports honest
    ``None`` liveness.  The snapshot is cached for :data:`_AGENTS_TTL_SEC` so a
    single ``list_sessions`` call spawns the subprocess at most once.
    """
    global _agents_cache
    with _agents_cache_lock:
        now = time.monotonic()
        cache = _agents_cache
        if cache is not None and now - cache[0] < _AGENTS_TTL_SEC:
            return cache[1]
        index = _parse_agents_index(_read_claude_agents_stdout())
        _agents_cache = (now, index)
        return index


def _read_claude_agents_stdout() -> str:
    """Run ``claude agents --json`` and return stdout, ``""`` on any failure.

    The subprocess boundary — isolated so tests monkeypatch it instead of
    spawning a real CLI.  Never raises: OS errors (no CLI on PATH), timeouts
    and non-zero exits all collapse to an empty string.
    """
    try:
        proc = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=_AGENTS_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _parse_agents_index(stdout: str) -> Dict[str, int]:
    """Parse ``claude agents --json`` output into ``{session_id: pid}``.

    Pure and defensive: non-JSON, an unexpected top-level shape, or entries
    missing / mistyping ``sessionId`` / ``pid`` are silently skipped.  Only
    ``str`` session ids paired with ``int`` pids are kept (``bool`` is a
    ``int`` subclass but never a real pid, so it is rejected).
    """
    if not stdout.strip():
        return {}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    index: Dict[str, int] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("sessionId")
        pid = entry.get("pid")
        if isinstance(sid, str) and sid and isinstance(pid, int) \
                and not isinstance(pid, bool):
            index[sid] = pid
    return index
