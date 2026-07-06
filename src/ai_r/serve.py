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

import hmac
import os
import socket as _socket
import time
from typing import Any, Mapping, Optional

# Public knobs (env-driven; ``AI_R_*`` convention, mirrors AI_R_SEMANTIC_*).
VALID_TRANSPORTS = ("stdio", "http")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756
DEFAULT_IDLE_SEC = 900.0

# Optional shared bearer token for the http transport.  When set, every
# request must carry ``Authorization: Bearer <token>`` — the second line of
# defense (after the SDK's Origin/Host DNS-rebinding validation) against
# another local user on a shared machine reaching the transcript corpus.
# The env var name is public API (documented in the README http section).
HTTP_TOKEN_ENV = "AI_R_HTTP_TOKEN"

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


def resolve_http_token(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the configured http bearer token, or ``None`` when unset.

    Empty/whitespace-only values are treated as unset so an accidental
    ``AI_R_HTTP_TOKEN=`` never enables auth-with-an-empty-secret (which would
    accept an empty bearer). The presence of a token is what turns on the
    :func:`_bearer_auth_asgi` gate in :func:`run_http`.
    """
    env = os.environ if env is None else env
    token = (env.get(HTTP_TOKEN_ENV) or "").strip()
    return token or None


def is_local_host(host: str) -> bool:
    """True when ``host`` binds the server to loopback only (on-box)."""
    return host.strip().lower() in LOCAL_HOSTS


def require_http_token(
    host: str, token: Optional[str], env: Optional[Mapping[str, str]] = None
) -> None:
    """Fail-closed when a non-loopback bind has no bearer token.

    Binding off-box exposes secret-bearing transcripts to the network, so a
    remote bind without ``AI_R_HTTP_TOKEN`` is refused outright (mirroring
    :func:`resolve_host`'s fail-loud style). A loopback bind may still set a
    token (defense against other local users) but is not forced to.
    """
    if token is not None:
        return
    if is_local_host(host):
        return
    raise ValueError(
        f"refusing to serve ai-r http on non-local host {host!r} without a "
        f"bearer token: transcripts contain secrets. Set {HTTP_TOKEN_ENV} to a "
        "strong random value (clients send it as 'Authorization: Bearer …')."
    )


def transport_security_settings(host: str, port: int) -> Any:
    """Build DNS-rebinding protection matching the actual bind target.

    The SDK auto-enables a loopback-only allowlist when a FastMCP instance is
    constructed with a localhost host, but our ``mcp`` object is built at
    import time before the env-driven host/port are known — and for a
    deliberate remote bind (``AI_R_MCP_ALLOW_REMOTE=1``) the loopback-only
    allowlist would reject the operator's own Host/Origin. So we compute the
    settings here from the resolved host/port and always keep DNS-rebinding
    protection ON (never a silent open door).
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    if not is_local_host(host):
        # A deliberate off-box bind: allow the operator's actual host:port
        # (both the wildcard-port and the exact-port form) plus its http(s)
        # origins, on top of the loopback defaults.
        hosts += [f"{host}:*", f"{host}:{port}"]
        origins += [
            f"http://{host}:*",
            f"http://{host}:{port}",
            f"https://{host}:*",
            f"https://{host}:{port}",
        ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _bearer_auth_asgi(app: Any, token: str) -> Any:
    """Wrap an ASGI app to require ``Authorization: Bearer <token>``.

    A constant-time compare (:func:`hmac.compare_digest`) avoids leaking the
    token length/prefix through timing. Non-http scopes pass straight through
    (only http requests carry an Authorization header). This runs OUTSIDE the
    MCP protocol layer, so a rejected request never reaches any tool.
    """
    expected = token.encode("utf-8")

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        provided: Optional[bytes] = None
        for name, value in scope.get("headers", ()):  # raw ASGI header pairs
            if name == b"authorization":
                provided = value
                break
        ok = False
        if provided is not None:
            prefix = b"Bearer "
            if provided[: len(prefix)].lower() == prefix.lower():
                ok = hmac.compare_digest(provided[len(prefix):].strip(), expected)
        if not ok:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": b"Unauthorized"}
            )
            return
        await app(scope, receive, send)

    return wrapped


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


def _release_semantic_if_idle() -> bool:
    """Free the semantic model if it has idled past its own threshold.

    Thin, fail-soft wrapper the idle loop calls each tick.  The import is
    local and defensive: if the semantic module is somehow unavailable this
    must never take the server down (A4 is opt-in; its absence is not fatal).
    Returns whether a model was actually released (for tests/logging).
    """
    try:
        from ai_r.semantic import release_if_idle
    except Exception:  # pragma: no cover - defensive, semantic ships in-tree
        return False
    return release_if_idle()


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

    # Auth + DNS-rebinding: fail-closed for a remote bind without a token, and
    # pin the transport-security allowlist to the *resolved* host/port (the
    # module-level ``mcp`` was built before host/port were known).
    token = resolve_http_token(env)
    require_http_token(host, token, env)
    mcp.settings.transport_security = transport_security_settings(host, port)

    state: dict = {"last": time.monotonic(), "active": 0}
    inner = mcp.streamable_http_app()
    if token is not None:
        inner = _bearer_auth_asgi(inner, token)
    app = _activity_asgi(inner, state)

    sockets = systemd_listen_sockets(env)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def _idle_watch() -> None:
        cadence = max(1.0, min(idle_sec, 30.0))
        while True:
            await asyncio.sleep(cadence)
            # A4: reclaim the ~118 MB semantic model after its own (shorter,
            # AI_R_SEMANTIC_IDLE_SEC) idle window, independently of the
            # server's exit window — a warm server that has not run a semantic
            # search for a while should still give the model's RAM back.  This
            # borrows the existing idle loop rather than spawning a reaper
            # thread (keeps hermeticity; no thread leak).  release_if_idle is
            # a cheap no-op when nothing is loaded.
            _release_semantic_if_idle()
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
