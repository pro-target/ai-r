"""Hermetic tests for the process-liveness classifier (``ai_r.liveness``).

Liveness is the honest *process* signal that ``activity`` (recency) cannot
give: it fuses the claude-agents pid registry with cheap ``/proc`` probes.
The core :func:`session_liveness` is pure — it takes the already-sampled
signals (recency label + pid/io booleans) and never touches ``/proc`` or the
clock — so every case here feeds fixed literals from the spec's state table
and asserts exact output.  The impure edges (``/proc`` reads, the
``claude agents --json`` subprocess) are exercised through monkeypatched
seams; no real processes, no host data.
"""

from __future__ import annotations

import json

import pytest

from ai_r import liveness as lv
from ai_r.activity import FRESH as ACT_FRESH, STALE as ACT_STALE
from ai_r.liveness import (
    DEAD,
    FRESH,
    PAUSED,
    ZOMBIE,
    resolve_session_liveness,
    session_liveness,
)


# --- pure core: the spec's state table -------------------------------------


def test_alive_and_fresh_is_fresh() -> None:
    """pid present + live I/O + recent activity → fresh."""
    assert session_liveness(ACT_FRESH, pid_alive=True, io_alive=True) == FRESH


def test_alive_and_stale_is_paused() -> None:
    """pid present + live I/O + stale recency → paused (alive but silent)."""
    assert session_liveness(ACT_STALE, pid_alive=True, io_alive=True) == PAUSED


def test_io_unknown_falls_through_to_recency() -> None:
    """io_alive None (couldn't probe fds) is not a zombie — use recency."""
    assert session_liveness(ACT_FRESH, pid_alive=True, io_alive=None) == FRESH
    assert session_liveness(ACT_STALE, pid_alive=True, io_alive=None) == PAUSED


def test_no_fds_is_zombie_regardless_of_recency() -> None:
    """pid present but the fd table is empty → zombie, whatever the recency."""
    assert session_liveness(ACT_FRESH, pid_alive=True, io_alive=False) == ZOMBIE
    assert session_liveness(ACT_STALE, pid_alive=True, io_alive=False) == ZOMBIE


def test_pid_gone_is_dead() -> None:
    """A known pid that /proc no longer shows → dead (io/recency ignored)."""
    assert session_liveness(ACT_FRESH, pid_alive=False, io_alive=True) == DEAD
    assert session_liveness(ACT_STALE, pid_alive=False, io_alive=None) == DEAD


def test_no_pid_signal_is_none() -> None:
    """No pid to check at all → None (honest absence, never fabricated)."""
    assert session_liveness(ACT_FRESH, pid_alive=None, io_alive=None) is None


def test_alive_but_unknown_recency_is_none() -> None:
    """Defensive: pid alive but no recency label → cannot classify → None."""
    assert session_liveness(None, pid_alive=True, io_alive=True) is None


# --- resolver: pid_index + /proc seams -------------------------------------


def test_resolve_no_snapshot_is_none() -> None:
    """No registry snapshot (claude CLI absent) → None, never dead."""
    assert resolve_session_liveness("sid", None, ACT_FRESH) is None


def test_resolve_blank_session_id_is_none() -> None:
    assert resolve_session_liveness("", {"sid": 1}, ACT_FRESH) is None
    assert resolve_session_liveness(None, {"sid": 1}, ACT_FRESH) is None


def test_resolve_session_absent_from_registry_is_none() -> None:
    """Session not in the live registry → no pid signal → None, NOT dead."""
    assert resolve_session_liveness("other", {"sid": 1}, ACT_STALE) is None


def test_resolve_live_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lv, "_pid_present", lambda pid: True)
    monkeypatch.setattr(lv, "_pid_io_alive", lambda pid: True)
    assert resolve_session_liveness("sid", {"sid": 42}, ACT_FRESH) == FRESH


def test_resolve_live_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lv, "_pid_present", lambda pid: True)
    monkeypatch.setattr(lv, "_pid_io_alive", lambda pid: True)
    assert resolve_session_liveness("sid", {"sid": 42}, ACT_STALE) == PAUSED


def test_resolve_zombie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lv, "_pid_present", lambda pid: True)
    monkeypatch.setattr(lv, "_pid_io_alive", lambda pid: False)
    assert resolve_session_liveness("sid", {"sid": 42}, ACT_FRESH) == ZOMBIE


def test_resolve_dead_when_registry_pid_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry named a pid but /proc shows it gone (stale registry) → dead."""
    monkeypatch.setattr(lv, "_pid_present", lambda pid: False)
    # io must not even be consulted once the pid is gone.
    monkeypatch.setattr(
        lv, "_pid_io_alive", lambda pid: pytest.fail("io probed for dead pid")
    )
    assert resolve_session_liveness("sid", {"sid": 42}, ACT_FRESH) == DEAD


# --- /proc probe helpers ---------------------------------------------------


def test_pid_present_reuses_comm_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """_pid_present is the empty-prefix reuse of session._pid_comm_starts_with."""
    seen: dict[str, object] = {}

    def fake(pid: int, prefix: str) -> bool:
        seen["pid"], seen["prefix"] = pid, prefix
        return True

    monkeypatch.setattr(lv, "_pid_comm_starts_with", fake)
    assert lv._pid_present(99) is True
    # Reused with the empty prefix == "is there any process at this pid".
    assert seen == {"pid": 99, "prefix": ""}


def test_pid_io_alive_true_when_fds_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lv.os, "listdir", lambda path: ["0", "1", "2"])
    assert lv._pid_io_alive(99) is True


def test_pid_io_alive_false_when_fd_table_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A defunct (zombie) process keeps /proc/<pid>/fd but with zero fds."""
    monkeypatch.setattr(lv.os, "listdir", lambda path: [])
    assert lv._pid_io_alive(99) is False


def test_pid_io_alive_none_when_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(path: str) -> list:
        raise PermissionError(path)

    monkeypatch.setattr(lv.os, "listdir", boom)
    assert lv._pid_io_alive(99) is None


# --- claude agents --json parsing + TTL cache ------------------------------


_AGENTS_JSON = json.dumps(
    [
        {"pid": 186931, "sessionId": "aaaa", "name": "one", "status": "busy"},
        {"pid": 188759, "sessionId": "bbbb", "name": "two", "status": "idle"},
        {"pid": None, "sessionId": "cccc"},  # missing pid → skipped
        {"pid": 5, "name": "no-session"},  # missing sessionId → skipped
        {"pid": "x", "sessionId": "dddd"},  # non-int pid → skipped
    ]
)


def test_parse_agents_index_maps_session_to_pid() -> None:
    assert lv._parse_agents_index(_AGENTS_JSON) == {"aaaa": 186931, "bbbb": 188759}


def test_parse_agents_index_malformed_is_empty() -> None:
    assert lv._parse_agents_index("not json {{{") == {}
    assert lv._parse_agents_index("") == {}
    # A JSON object (not the expected array) yields nothing, never a crash.
    assert lv._parse_agents_index('{"pid": 1}') == {}


def test_pid_index_ttl_caches_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subprocess runs once per TTL window, not once per session."""
    calls = {"n": 0}

    def fake_stdout() -> str:
        calls["n"] += 1
        return _AGENTS_JSON

    fake_clock = {"t": 1000.0}
    monkeypatch.setattr(lv, "_read_claude_agents_stdout", fake_stdout)
    monkeypatch.setattr(lv.time, "monotonic", lambda: fake_clock["t"])
    monkeypatch.setattr(lv, "_agents_cache", None, raising=False)

    first = lv.claude_agents_pid_index()
    second = lv.claude_agents_pid_index()  # within TTL → cached
    assert first == {"aaaa": 186931, "bbbb": 188759}
    assert second == first
    assert calls["n"] == 1

    fake_clock["t"] += lv._AGENTS_TTL_SEC + 0.1  # past TTL → refetch
    lv.claude_agents_pid_index()
    assert calls["n"] == 2


def test_pid_index_missing_cli_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No claude CLI on PATH → empty index, best-effort, never raises."""
    monkeypatch.setattr(lv, "_read_claude_agents_stdout", lambda: "")
    monkeypatch.setattr(lv, "_agents_cache", None, raising=False)
    assert lv.claude_agents_pid_index() == {}
