"""Hermetic tests for the shared-http transport helpers (``ai_r.serve``).

The decisions are pure predicates — ``now`` / ``env`` / ``pid`` are arguments,
never the real clock or real process — so every case feeds fixed inputs and
asserts exact output.  The imperative ``run_http`` (uvicorn wiring) is not
exercised here; only the logic that decides transport, idle-exit, and
socket-activation.
"""

from __future__ import annotations

import pytest

from ai_r.serve import (
    DEFAULT_HOST,
    DEFAULT_IDLE_SEC,
    DEFAULT_PORT,
    VALID_TRANSPORTS,
    resolve_host,
    resolve_transport,
    should_exit_idle,
    systemd_listen_sockets,
)


# --- resolve_transport -----------------------------------------------------

def test_transport_defaults_to_stdio() -> None:
    """No env var -> stdio (back-compat)."""
    assert resolve_transport({}) == "stdio"


def test_transport_empty_string_is_stdio() -> None:
    """Empty/whitespace value falls back to stdio, not an error."""
    assert resolve_transport({"AI_R_MCP_TRANSPORT": "   "}) == "stdio"


@pytest.mark.parametrize(
    "value", ["http", "HTTP", "streamable-http", "streamable_http"]
)
def test_transport_http_aliases(value: str) -> None:
    """``http`` and its streamable aliases all normalise to ``http``."""
    assert resolve_transport({"AI_R_MCP_TRANSPORT": value}) == "http"


def test_transport_unknown_fails_closed() -> None:
    """An unknown transport is a hard error, never a silent fallback."""
    with pytest.raises(ValueError, match="unknown AI_R_MCP_TRANSPORT"):
        resolve_transport({"AI_R_MCP_TRANSPORT": "grpc"})


def test_valid_transports_are_stdio_and_http() -> None:
    assert VALID_TRANSPORTS == ("stdio", "http")


# --- resolve_host ----------------------------------------------------------

def test_host_defaults_to_loopback() -> None:
    """No env var -> loopback bind, nothing exposed off-box."""
    assert resolve_host({}) == DEFAULT_HOST


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST"])
def test_host_local_values_pass(host: str) -> None:
    """Loopback aliases are accepted (case-insensitive)."""
    assert resolve_host({"AI_R_MCP_HOST": host}) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_host_non_local_fails_closed(host: str) -> None:
    """A non-local bind is refused unless explicitly allowed."""
    with pytest.raises(ValueError, match="non-local host"):
        resolve_host({"AI_R_MCP_HOST": host})


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_host_non_local_opt_in(flag: str) -> None:
    """AI_R_MCP_ALLOW_REMOTE lets an operator opt into an off-box bind."""
    env = {"AI_R_MCP_HOST": "0.0.0.0", "AI_R_MCP_ALLOW_REMOTE": flag}
    assert resolve_host(env) == "0.0.0.0"


# --- should_exit_idle ------------------------------------------------------

def test_idle_exit_after_threshold() -> None:
    """Idle past the threshold with nothing in flight -> exit."""
    assert should_exit_idle(
        last_activity=0.0,
        now=DEFAULT_IDLE_SEC,
        idle_sec=DEFAULT_IDLE_SEC,
        active_requests=0,
    )


def test_idle_no_exit_below_threshold() -> None:
    assert not should_exit_idle(
        last_activity=100.0, now=200.0, idle_sec=900.0, active_requests=0
    )


def test_idle_never_exits_with_request_in_flight() -> None:
    """A long-running request blocks exit no matter how long since ``last``."""
    assert not should_exit_idle(
        last_activity=0.0, now=10_000.0, idle_sec=900.0, active_requests=1
    )


def test_idle_disabled_when_idle_sec_zero() -> None:
    """``idle_sec <= 0`` disables idle-exit (server runs forever)."""
    assert not should_exit_idle(
        last_activity=0.0, now=10_000.0, idle_sec=0.0, active_requests=0
    )


def test_idle_negative_elapsed_clamped() -> None:
    """Clock jitter (now < last) clamps elapsed to 0 -> never exits."""
    assert not should_exit_idle(
        last_activity=500.0, now=100.0, idle_sec=300.0, active_requests=0
    )


# --- systemd_listen_sockets ------------------------------------------------

def test_no_listen_fds_returns_none() -> None:
    """Not socket-activated -> None (fall back to explicit bind)."""
    assert systemd_listen_sockets({}) is None


def test_listen_pid_mismatch_returns_none() -> None:
    """LISTEN_PID for a different process -> not ours, so no sockets."""
    env = {"LISTEN_FDS": "1", "LISTEN_PID": "999999"}
    assert systemd_listen_sockets(env, pid=1234) is None


def test_listen_fds_non_numeric_returns_none() -> None:
    assert systemd_listen_sockets({"LISTEN_FDS": "notanumber"}) is None


def test_listen_fds_zero_returns_none() -> None:
    assert systemd_listen_sockets({"LISTEN_FDS": "0"}) is None


def test_default_port_is_documented_value() -> None:
    """Guard the documented default so docs/config stay in sync."""
    assert DEFAULT_PORT == 8756
