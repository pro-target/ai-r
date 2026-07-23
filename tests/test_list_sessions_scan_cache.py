"""Hermetic tests for the ``list_sessions`` per-agent scan cache.

Spec: the ``list_sessions`` MCP tool must NOT re-run a parser's corpus scan
when nothing under that parser's source roots changed (same file set, same
per-file mtime + size).  Any file-level change — a new file, an mtime bump,
or a same-mtime size change (append) — must invalidate the entry and force
a fresh scan, so a cache HIT is byte-identical to a MISS.

Directory mtimes alone are NOT a sufficient validator: appending to a live
session JSONL changes the file but not its parent directory, which would
freeze ``date``/``message_count`` for exactly the actively-running sessions
(the honest-liveness contract).  Hence the per-file stat signature, and the
append case below as its regression guard.

The fake parser is mounted on the Codex slot (no pid-registry sampling) and
scoped to a per-test temp root, so every case is hermetic and the counting
seam observes ONLY this test's scans.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_r import mcp_server as m
from ai_r.parsers import AgentName, Session


@pytest.fixture(autouse=True)
def _clear_scan_cache():
    """Isolate every case from warm entries left by earlier tests."""
    cache = getattr(m, "_agent_sessions_cache", None)
    if cache is not None:
        cache.clear()
    yield
    cache = getattr(m, "_agent_sessions_cache", None)
    if cache is not None:
        cache.clear()


@pytest.fixture
def scan_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A counting fake Codex parser over a real (temp) source root.

    ``list_sessions`` stamps the scan ordinal into the session title so a
    served-from-cache result is distinguishable from a fresh scan by
    CONTENT, not just by call count.
    """
    root = tmp_path / "codex-root"
    root.mkdir()
    (root / "s1.jsonl").write_text('{"n": 1}\n', encoding="utf-8")
    calls = {"n": 0}

    def fake_list_sessions(base_dir=None):
        calls["n"] += 1
        return [
            Session(
                uuid="cache-probe-1",
                agent=AgentName.CODEX,
                title=f"probe scan {calls['n']}",
                date=datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc),
                path=str(root / "s1.jsonl"),
                message_count=2,
            )
        ]

    fake_parser = SimpleNamespace(
        list_sessions=fake_list_sessions,
        source_roots=lambda base_dir=None: [str(root)],
    )
    monkeypatch.setitem(m._PARSERS, AgentName.CODEX, fake_parser)
    return SimpleNamespace(root=root, calls=calls)


def test_warm_repeat_reuses_scan(scan_env: SimpleNamespace) -> None:
    """Second call with an unchanged root serves the FIRST scan, unchanged."""
    out1 = m.list_sessions(agent="codex")
    out2 = m.list_sessions(agent="codex")
    assert scan_env.calls["n"] == 1
    assert out1["total"] == 1
    assert out2["total"] == 1
    assert out2["sessions"][0]["uuid"] == "cache-probe-1"
    assert out2["sessions"][0]["title"] == "probe scan 1"


def test_mtime_bump_invalidates(scan_env: SimpleNamespace) -> None:
    """A changed file mtime is a changed signature → fresh scan served."""
    m.list_sessions(agent="codex")
    os.utime(scan_env.root / "s1.jsonl", ns=(1, 1))
    out = m.list_sessions(agent="codex")
    assert scan_env.calls["n"] == 2
    assert out["sessions"][0]["title"] == "probe scan 2"


def test_new_file_invalidates(scan_env: SimpleNamespace) -> None:
    """A file appearing under the root forces a fresh scan."""
    m.list_sessions(agent="codex")
    (scan_env.root / "s2.jsonl").write_text('{"n": 2}\n', encoding="utf-8")
    out = m.list_sessions(agent="codex")
    assert scan_env.calls["n"] == 2
    assert out["sessions"][0]["title"] == "probe scan 2"


def test_same_mtime_size_change_invalidates(scan_env: SimpleNamespace) -> None:
    """An append with a preserved mtime still invalidates (size is signed).

    Guards the validator design: a directory-mtime (or even file-mtime-only)
    check can miss an append that lands within one clock tick; ``st_size``
    in the signature closes that hole.
    """
    m.list_sessions(agent="codex")
    target = scan_env.root / "s1.jsonl"
    st = target.stat()
    with target.open("a", encoding="utf-8") as fh:
        fh.write('{"n": 2}\n')
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    out = m.list_sessions(agent="codex")
    assert scan_env.calls["n"] == 2
    assert out["sessions"][0]["title"] == "probe scan 2"
