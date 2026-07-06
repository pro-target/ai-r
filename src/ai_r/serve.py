"""Shared streamable-http transport for the ``ai-r`` MCP server.

Motivation (F6 — kill the stdio swarm)
--------------------------------------
Every agent/subagent that launches ``ai-r-mcp`` over **stdio** gets its own
process with a *cold*, per-process cache.  Under multi-agent fan-out that means
N processes each re-scanning the whole session corpus N times — the measured
root cause of host RAM/CPU exhaustion (swap thrash → compositor starvation →
graphical artifacts) diagnosed from a real session.

A single long-lived **streamable-http** server, socket-activated by systemd
(idle-off + respawn-on-demand), collapses the swarm to one warm process shared
by every agent.  ``stdio`` stays the default transport — http is strictly
opt-in via ``AI_R_MCP_TRANSPORT=http`` and binds localhost-only (a non-loopback
``AI_R_MCP_HOST`` is refused fail-closed unless ``AI_R_MCP_ALLOW_REMOTE=1``), so
nothing is exposed off-box and existing stdio sessions keep working unchanged.

This module keeps the *decisions* as pure, unit-testable predicates
(``resolve_transport`` / ``should_exit_idle`` / ``systemd_listen_sockets``) and
isolates the imperative uvicorn wiring in ``run_http``.
"""

from __future__ import annotations

import os
import socket as _socket
import time
from typing import Any, Mapping, Optional

# Public knobs (env-driven; ``AI_R_*`` convention, mirrors AI_R_SEMANTIC_*).
VALID_TRANSPORTS = ("stdio", "http")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756
DEFAULT_IDLE_SEC = 900.0

# systemd passes activation sockets starting at fd 3 (SD_LISTEN_FDS_START).
_SD_LISTEN_FDS_START = 3


def resolve_transport(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the transport to run: ``"stdio"`` (default) or ``"http"``.

    ``streamable-http`` / ``streamable_http`` are accepted aliases for
    ``http``.  An unknown value is a hard error (fail-closed) rather than a
    silent fallback, so a typo can never quietly launch the wrong transport.
    """
    env = os.environ if env is None else env
    raw = (env.get("AI_R_MCP_TRANSPORT") or "").strip().lower()
    if not raw:
        return "stdio"
    if raw in ("streamable-http", "streamable_http"):
        raw = "http"
    if raw not in VALID_TRANSPORTS:
        raise ValueError(
            f"unknown AI_R_MCP_TRANSPORT={raw!r}; "
            f"expected one of {VALID_TRANSPORTS}"
        )
    return raw


# Hosts that keep the server on-box (loopback only, no off-machine exposure).
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")


def resolve_host(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the http bind host, refusing an unguarded off-box bind.

    Session transcripts routinely contain pasted secrets and are served
    with **no auth token**, so binding anywhere but loopback would expose
    every local transcript to the network.  A non-local ``AI_R_MCP_HOST``
    is therefore a hard error (fail-closed) unless the operator sets
    ``AI_R_MCP_ALLOW_REMOTE=1`` to opt in deliberately — mirroring
    ``resolve_transport``'s fail-loud style.  Default (unset) is loopback.
    """
    env = os.environ if env is None else env
    host = (env.get("AI_R_MCP_HOST") or DEFAULT_HOST).strip()
    if host.lower() in LOCAL_HOSTS:
        return host
    allow = (env.get("AI_R_MCP_ALLOW_REMOTE") or "").strip().lower()
    if allow not in ("1", "true", "yes", "on"):
        raise ValueError(
            f"refusing to bind ai-r http server to non-local host {host!r}: "
            "transcripts contain secrets and are served without auth. "
            "Set AI_R_MCP_ALLOW_REMOTE=1 to override deliberately."
        )
    return host


def should_exit_idle(
    last_activity: float,
    now: float,
    idle_sec: float,
    active_requests: int,
) -> bool:
    """Pure predicate: should the http server self-exit for idleness?

    * ``idle_sec <= 0`` disables idle-exit (server runs forever) → ``False``.
    * A request in flight (``active_requests > 0``) always blocks exit, so a
      long-running call is never cut off mid-flight.
    * Elapsed time is clamped at ``0`` for writer/reader clock jitter, matching
      the clamp in :func:`ai_r.activity.session_activity`.

    Exiting is safe only under socket-activation: systemd keeps the listening
    socket and respawns the service on the next connection.  ``run_http`` owns
    that contract; this function only decides *when*.
    """
    if idle_sec is None or idle_sec <= 0:
        return False
    if active_requests > 0:
        return False
    elapsed = now - last_activity
    if elapsed < 0:
        elapsed = 0.0
    return elapsed >= idle_sec


def systemd_listen_sockets(
    env: Optional[Mapping[str, str]] = None,
    *,
    pid: Optional[int] = None,
) -> Optional[list["_socket.socket"]]:
    """Return sockets inherited from systemd socket-activation, or ``None``.

    Honours the sd_listen_fds(3) protocol: ``LISTEN_FDS`` (count, from fd 3)
    and ``LISTEN_PID`` (must equal our pid, guarding against an fd meant for a
    different process).  Returns ``None`` when not socket-activated, so
    ``run_http`` falls back to an explicit host/port bind.
    """
    env = os.environ if env is None else env
    raw_fds = env.get("LISTEN_FDS")
    if not raw_fds:
        return None
    me = os.getpid() if pid is None else pid
    listen_pid = env.get("LISTEN_PID")
    if listen_pid not in (None, "", str(me)):
        return None
    try:
        count = int(raw_fds)
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    socks: list[_socket.socket] = []
    for fd in range(_SD_LISTEN_FDS_START, _SD_LISTEN_FDS_START + count):
        sock = _socket.socket(fileno=fd)
        sock.setblocking(False)
        socks.append(sock)
    return socks or None


def _activity_asgi(app: Any, state: dict) -> Any:
    """Wrap an ASGI app to track request activity without buffering.

    A raw-ASGI wrapper (not Starlette ``BaseHTTPMiddleware``) is used on
    purpose: ``BaseHTTPMiddleware`` buffers the response body and would break
    the long-lived streaming responses that streamable-http relies on.  The
    loop is single-threaded, so the plain-int counter needs no lock.
    """

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        state["active"] += 1
        state["last"] = time.monotonic()

        async def _send(message):
            await send(message)
            if (
                message.get("type") == "http.response.body"
                and not message.get("more_body", False)
            ):
                state["last"] = time.monotonic()

        try:
            await app(scope, receive, _send)
        finally:
            state["active"] -= 1
            state["last"] = time.monotonic()

    return wrapped


def run_http(mcp: Any, env: Optional[Mapping[str, str]] = None) -> int:  # pragma: no cover
    """Serve the FastMCP app over streamable-http with idle self-exit.

    Uses a systemd-passed socket when present (socket-activation); otherwise
    binds ``AI_R_MCP_HOST``:``AI_R_MCP_PORT`` (localhost:8756 by default).
    After ``AI_R_MCP_IDLE_SEC`` with no in-flight and no recent request the
    process exits so a socket-activation unit can respawn on demand.

    ``uvicorn`` is imported lazily here so stdio users never need it installed
    (honest optional dependency — the ``ai-r[http]`` extra).
    """
    import asyncio

    import uvicorn

    env = os.environ if env is None else env
    host = resolve_host(env)
    port = int(env.get("AI_R_MCP_PORT") or DEFAULT_PORT)
    idle_sec = float(env.get("AI_R_MCP_IDLE_SEC") or DEFAULT_IDLE_SEC)

    state: dict = {"last": time.monotonic(), "active": 0}
    app = _activity_asgi(mcp.streamable_http_app(), state)

    sockets = systemd_listen_sockets(env)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def _idle_watch() -> None:
        cadence = max(1.0, min(idle_sec, 30.0))
        while True:
            await asyncio.sleep(cadence)
            if should_exit_idle(
                state["last"], time.monotonic(), idle_sec, state["active"]
            ):
                server.should_exit = True
                return

    async def _serve() -> None:
        watcher = asyncio.create_task(_idle_watch()) if idle_sec > 0 else None
        try:
            await server.serve(sockets=sockets)
        finally:
            if watcher is not None:
                watcher.cancel()

    asyncio.run(_serve())
    return 0
