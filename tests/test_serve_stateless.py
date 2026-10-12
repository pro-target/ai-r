"""D3: the shared http transport must survive stale ``Mcp-Session-Id``s.

Forensics 2026-09-29: the long-lived shared daemon (one process for every
agent) outlives its clients.  The SDK's stateful
``StreamableHTTPSessionManager`` keeps the session registry in process
memory and answers a request whose session id it no longer knows with
HTTP 404 + JSON-RPC ``-32600 "Session not found"`` — so a client reusing
its cached id after a session expiry/restart failed EVERY call while the
CLI kept working.  These tests lock in the fix: stateless dispatch (the
default) serves such requests; the stateful mode's 404 is documented as
the defect mechanism.

Everything runs through Starlette's in-process ``TestClient`` (an ASGI
bridge) — no socket is ever bound.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from ai_r.serve import STATELESS_ENV, apply_http_settings, resolve_stateless

# Headers a streamable-http POST must carry (Accept per spec; the stale
# session id is the defect trigger).
_POST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Mcp-Session-Id": "definitely-not-in-the-registry",
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _fresh_mcp(stateless: bool) -> FastMCP:
    """A tiny FastMCP with one tool, dispatch mode forced explicitly."""
    mcp = FastMCP("stateless-test")

    @mcp.tool()
    def probe() -> str:
        return "pong"

    mcp.settings.stateless_http = stateless
    return mcp


# --- resolve_stateless (pure predicate) -------------------------------------


def test_stateless_defaults_on() -> None:
    assert resolve_stateless({}) is True
    assert resolve_stateless({STATELESS_ENV: ""}) is True
    assert resolve_stateless({STATELESS_ENV: "  "}) is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_stateless_truthy_values(value: str) -> None:
    assert resolve_stateless({STATELESS_ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_stateless_falsy_values(value: str) -> None:
    assert resolve_stateless({STATELESS_ENV: value}) is False


def test_stateless_malformed_fails_loud() -> None:
    with pytest.raises(ValueError, match=STATELESS_ENV):
        resolve_stateless({STATELESS_ENV: "maybe"})


# --- apply_http_settings (the run_http wiring, testable) --------------------


def test_apply_http_settings_enables_stateless_by_default() -> None:
    mcp = _fresh_mcp(stateless=False)  # run_http must flip it ON
    apply_http_settings(mcp, {}, "127.0.0.1", 8756)
    assert mcp.settings.stateless_http is True
    assert mcp.settings.transport_security is not None
    # the session manager built from the app inherits the dispatch mode
    mcp.streamable_http_app()
    assert mcp._session_manager.stateless is True


def test_apply_http_settings_stateful_opt_out() -> None:
    mcp = _fresh_mcp(stateless=True)  # explicit opt-out wins
    apply_http_settings(mcp, {STATELESS_ENV: "0"}, "127.0.0.1", 8756)
    assert mcp.settings.stateless_http is False


# --- ASGI-level regression: the stale-session-id request ---------------------


def test_stale_session_id_is_served_statelessly() -> None:
    """The fix: a request carrying an unknown session id is SERVED —
    stateless dispatch never consults a session registry."""
    app = _fresh_mcp(stateless=True).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:9999") as client:
        resp = client.post("/mcp", headers=_POST_HEADERS, json=_TOOLS_LIST)
    assert resp.status_code == 200
    assert "Session not found" not in resp.text
    assert "probe" in resp.text  # the tool list actually came back


def test_stale_session_id_stateful_404_documents_the_defect() -> None:
    """The mechanism (kept as documentation): the stateful registry answers
    an unknown session id with 404 -32600 "Session not found" — exactly
    what the shared daemon's long-lived clients kept hitting."""
    app = _fresh_mcp(stateless=False).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:9999") as client:
        resp = client.post("/mcp", headers=_POST_HEADERS, json=_TOOLS_LIST)
    assert resp.status_code == 404
    assert "Session not found" in resp.text
