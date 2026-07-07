"""Unit coverage for serve.py internals that the auth/config tests don't touch.

These target the imperative-but-testable helpers — the activity-tracking ASGI
wrapper, the idle semantic-release shim, and the systemd socket-activation FD
loop — that are real logic (not the ``uvicorn`` bootstrap, which is
``# pragma: no cover``). Kept hermetic: a ``socketpair`` stands in for a
systemd-passed listener, no real bind.
"""

from __future__ import annotations

import asyncio
import os
import socket as _socket

from ai_r.serve import (
    _SD_LISTEN_FDS_START,
    _activity_asgi,
    _release_semantic_if_idle,
    systemd_listen_sockets,
)


# --- _activity_asgi --------------------------------------------------------

def test_activity_asgi_tracks_in_flight_and_settles() -> None:
    """An http request bumps ``active`` to 1 mid-call, back to 0 in ``finally``,
    and stamps ``last`` on both entry and body-end."""
    state = {"active": 0, "last": 0.0}
    seen: dict = {}

    async def app(scope, receive, send) -> None:
        seen["active_during"] = state["active"]
        await send({"type": "http.response.start", "status": 200})
        await send(
            {"type": "http.response.body", "body": b"x", "more_body": False}
        )

    wrapped = _activity_asgi(app, state)
    sent: list = []

    async def send(msg) -> None:
        sent.append(msg)

    async def receive():  # pragma: no cover - not driven in this path
        return {"type": "http.request"}

    asyncio.run(wrapped({"type": "http"}, receive, send))

    assert seen["active_during"] == 1
    assert state["active"] == 0
    assert state["last"] > 0.0
    assert any(m["type"] == "http.response.body" for m in sent)


def test_activity_asgi_passes_non_http_through_untracked() -> None:
    """A lifespan/websocket scope is forwarded verbatim and never touches the
    request counter."""
    state = {"active": 0, "last": 0.0}
    called: dict = {}

    async def app(scope, receive, send) -> None:
        called["scope_type"] = scope["type"]

    wrapped = _activity_asgi(app, state)

    async def send(msg) -> None:  # pragma: no cover - not driven here
        pass

    async def receive():  # pragma: no cover - not driven here
        return {}

    asyncio.run(wrapped({"type": "lifespan"}, receive, send))

    assert called["scope_type"] == "lifespan"
    assert state["active"] == 0
    assert state["last"] == 0.0


# --- _release_semantic_if_idle ---------------------------------------------

def test_release_semantic_if_idle_delegates_to_semantic(monkeypatch) -> None:
    """The shim forwards to ``ai_r.semantic.release_if_idle`` and returns its
    verdict."""
    import ai_r.semantic as sem

    calls = {"n": 0}

    def fake_release() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(sem, "release_if_idle", fake_release, raising=False)
    assert _release_semantic_if_idle() is True
    assert calls["n"] == 1


# --- systemd_listen_sockets (FD loop) --------------------------------------

def test_systemd_listen_sockets_wraps_passed_fd() -> None:
    """A real fd placed at ``LISTEN_FDS_START`` is wrapped into a socket object.

    Uses a ``socketpair`` duped onto fd 3 to emulate systemd socket-activation,
    restoring whatever occupied fd 3 afterwards so the test never corrupts the
    interpreter's descriptor table.
    """
    sock_a, sock_b = _socket.socketpair()
    saved = None
    try:
        try:
            saved = os.dup(_SD_LISTEN_FDS_START)
        except OSError:
            saved = None
        os.dup2(sock_a.fileno(), _SD_LISTEN_FDS_START)

        env = {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid())}
        socks = systemd_listen_sockets(env)

        assert socks is not None
        assert len(socks) == 1
        assert isinstance(socks[0], _socket.socket)
        # Detach so garbage-collecting the wrapper does not close fd 3 out from
        # under our own restore step below.
        for s in socks:
            s.detach()
    finally:
        if saved is not None:
            os.dup2(saved, _SD_LISTEN_FDS_START)
            os.close(saved)
        else:
            try:
                os.close(_SD_LISTEN_FDS_START)
            except OSError:
                pass
        sock_a.close()
        sock_b.close()
