"""Hermetic tests for the http-transport security additions (Defect #1).

Covers the pure predicates that gate the shared streamable-http server:

* ``resolve_http_token`` — env → optional bearer token (empty ⇒ unset).
* ``require_http_token`` — fail-closed for a remote bind without a token.
* ``transport_security_settings`` — DNS-rebinding allowlist pinned to the
  resolved host/port (loopback default + explicit remote host on opt-in).
* ``_bearer_auth_asgi`` — 401 unless a correct ``Bearer`` header is present.

All inputs are arguments (env dicts, fixed host/port), never the real
process, so every case is deterministic and needs no host data.
"""

from __future__ import annotations

import asyncio

import pytest

from ai_r.serve import (
    HTTP_TOKEN_ENV,
    _bearer_auth_asgi,
    is_local_host,
    require_http_token,
    resolve_http_token,
    transport_security_settings,
)


# --- resolve_http_token ----------------------------------------------------

def test_token_unset_is_none() -> None:
    assert resolve_http_token({}) is None


def test_token_blank_is_none() -> None:
    """An accidental empty value must not enable auth-with-empty-secret."""
    assert resolve_http_token({HTTP_TOKEN_ENV: "   "}) is None


def test_token_value_is_stripped() -> None:
    assert resolve_http_token({HTTP_TOKEN_ENV: "  s3cr3t  "}) == "s3cr3t"


# --- require_http_token (fail-closed) --------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_local_bind_without_token_is_allowed(host: str) -> None:
    """Loopback may run tokenless (Origin/Host validation still applies)."""
    require_http_token(host, None)  # no raise


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_remote_bind_without_token_fails_closed(host: str) -> None:
    with pytest.raises(ValueError, match="without a bearer token"):
        require_http_token(host, None)


def test_remote_bind_with_token_is_allowed() -> None:
    require_http_token("0.0.0.0", "tok")  # no raise


def test_is_local_host() -> None:
    assert is_local_host("127.0.0.1")
    assert is_local_host("LOCALHOST")
    assert not is_local_host("0.0.0.0")


# --- transport_security_settings -------------------------------------------

def test_rebinding_protection_always_on_local() -> None:
    ts = transport_security_settings("127.0.0.1", 8756)
    assert ts.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in ts.allowed_hosts
    assert "http://127.0.0.1:*" in ts.allowed_origins
    # A loopback bind must NOT silently whitelist off-box hosts.
    assert not any(h.startswith("0.0.0.0") for h in ts.allowed_hosts)


def test_remote_host_added_to_allowlist() -> None:
    ts = transport_security_settings("192.168.1.10", 9000)
    assert ts.enable_dns_rebinding_protection is True
    # Loopback defaults survive AND the operator's real host:port is allowed.
    assert "127.0.0.1:*" in ts.allowed_hosts
    assert "192.168.1.10:*" in ts.allowed_hosts
    assert "192.168.1.10:9000" in ts.allowed_hosts
    assert "http://192.168.1.10:9000" in ts.allowed_origins
    assert "https://192.168.1.10:9000" in ts.allowed_origins


# --- _bearer_auth_asgi -----------------------------------------------------

def _run_asgi(app, headers) -> tuple[int, bytes]:
    """Drive one http request through an ASGI app, return (status, body)."""
    scope = {"type": "http", "headers": headers}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return status, body


def _passthrough_app():
    async def app(scope, receive, send) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def test_bearer_missing_header_rejected() -> None:
    app = _bearer_auth_asgi(_passthrough_app(), "s3cr3t")
    status, _ = _run_asgi(app, headers=[])
    assert status == 401


def test_bearer_wrong_token_rejected() -> None:
    app = _bearer_auth_asgi(_passthrough_app(), "s3cr3t")
    status, _ = _run_asgi(app, headers=[(b"authorization", b"Bearer nope")])
    assert status == 401


def test_bearer_correct_token_passes() -> None:
    app = _bearer_auth_asgi(_passthrough_app(), "s3cr3t")
    status, body = _run_asgi(
        app, headers=[(b"authorization", b"Bearer s3cr3t")]
    )
    assert status == 200
    assert body == b"ok"


def test_bearer_scheme_case_insensitive() -> None:
    """The 'Bearer' scheme token is case-insensitive; the secret is not."""
    app = _bearer_auth_asgi(_passthrough_app(), "s3cr3t")
    status, _ = _run_asgi(
        app, headers=[(b"authorization", b"bearer s3cr3t")]
    )
    assert status == 200


def test_bearer_non_http_scope_passes_through() -> None:
    """A lifespan/websocket scope carries no auth header — never gated."""
    seen: list[str] = []

    async def app(scope, receive, send) -> None:
        seen.append(scope["type"])

    wrapped = _bearer_auth_asgi(app, "s3cr3t")
    asyncio.run(wrapped({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]
