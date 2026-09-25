"""Hermetic regression tests for the core ``list_sessions`` scan cache.

Spec: the core read paths that ``audit_dossier`` drives -- ``iter_events``
(and hence ``get_body``), ``children_of`` and ``find_tool_calls`` -- must NOT
re-run a parser's corpus scan on every call when nothing under that parser's
source roots changed.  Before the cache each call re-walked the whole corpus
(cProfile on ``audit_dossier`` attributed ~90% of a 1081s run to repeated
``claude.list_sessions`` walks; ``get_body`` alone drove 37 of 63 walks).
This test pins the fix: a stable root means one scan served to every repeat
call, and any file-level change means a fresh scan.

The fake parser is mounted on the Codex slot over a per-test temp root and
``AI_R_HOME`` is redirected there, so every case is hermetic (no real-home
read) and the counting seam observes ONLY this test's scans.

Known limitation (OpenCode SQLite WAL): the core cache's stat signature
covers the main DB file only (``opencode.db``), not its ``-wal``/``-shm``
companions -- so in WAL mode a long-lived process may serve a stale
OpenCode list until a checkpoint moves data into ``opencode.db``.  Pre-
existing in the MCP-layer mirror cache; file-back agents are unaffected.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import ai_r.parsers._common as _parsers_common
from ai_r import parsers
from ai_r.events.model import iter_events
from ai_r.events.plan import get_body
from ai_r.find_tool_calls import find_tool_calls
from ai_r.parsers import AgentName, Session
from ai_r.session_stats import children_of


@pytest.fixture(autouse=True)
def _isolate_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Hermetic corpus: redirect AI_R_HOME + clear the scan cache per case."""
    monkeypatch.setenv("AI_R_HOME", str(tmp_path))
    cache = getattr(_parsers_common, "_sessions_scan_cache", None)
    if cache is not None:
        cache.clear()
    yield
    cache = getattr(_parsers_common, "_sessions_scan_cache", None)
    if cache is not None:
        cache.clear()


@pytest.fixture
def counting_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """A counting fake Codex parser over a real (temp) source root."""
    root = tmp_path / "codex-root"
    root.mkdir()
    (root / "s1.jsonl").write_text('{"n": 1}\n', encoding="utf-8")
    calls = {"n": 0}

    def fake_list_sessions(base_dir=None):
        calls["n"] += 1
        return [
            Session(
                uuid="probe-1",
                agent=AgentName.CODEX,
                title="probe",
                date=datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc),
                path=str(root / "s1.jsonl"),
                message_count=1,
                parent_uuid="parent-x",
            )
        ]

    fake_parser = SimpleNamespace(
        list_sessions=fake_list_sessions,
        read_messages=lambda uuid, base_dir=None: [],
        read_session=lambda uuid, base_dir=None: fake_list_sessions()[0],
        search=lambda query, base_dir=None: [],
        session_exists=lambda uuid, base_dir=None: True,
        source_roots=lambda base_dir=None: [str(root)],
    )
    monkeypatch.setitem(parsers.PARSERS, AgentName.CODEX, fake_parser)
    return SimpleNamespace(root=root, calls=calls)


def test_iter_events_reuses_scan(counting_codex: SimpleNamespace) -> None:
    """Repeated ``iter_events`` for one session serves ONE scan, not N."""
    for _ in range(5):
        list(iter_events(agent="codex", session="probe-1"))
    assert counting_codex.calls["n"] == 1


def test_get_body_reuses_scan(counting_codex: SimpleNamespace) -> None:
    """``get_body`` drives ``iter_events`` -- the hottest path reuses the scan."""
    for _ in range(5):
        get_body("probe-1:0")
    assert counting_codex.calls["n"] == 1


def test_children_of_reuses_scan(counting_codex: SimpleNamespace) -> None:
    """Repeated ``children_of`` serves ONE scan."""
    for _ in range(5):
        children_of("parent-x", agent="codex")
    assert counting_codex.calls["n"] == 1


def test_find_tool_calls_reuses_scan(counting_codex: SimpleNamespace) -> None:
    """Repeated ``find_tool_calls`` serves ONE scan."""
    for _ in range(5):
        find_tool_calls(agent="codex", tool_name="Read")
    assert counting_codex.calls["n"] == 1


def test_cache_invalidates_on_file_change(counting_codex: SimpleNamespace) -> None:
    """A file mtime bump under the root forces a fresh scan (no pinned-stale)."""
    list(iter_events(agent="codex", session="probe-1"))
    os.utime(counting_codex.root / "s1.jsonl", ns=(1, 1))
    list(iter_events(agent="codex", session="probe-1"))
    assert counting_codex.calls["n"] == 2



def test_same_mtime_size_change_invalidates_core(
    counting_codex: SimpleNamespace,
) -> None:
    """An append preserving mtime still invalidates -- ``st_size`` is signed.

    Ports the design-critical guard from ``test_list_sessions_scan_cache`` (the
    MCP-layer mirror cache) to the core cache ``audit_dossier`` now exercises.
    A file-mtime-only validator could miss an append landing within one clock
    tick; ``st_size`` in the signature closes that hole.
    """
    list(iter_events(agent="codex", session="probe-1"))  # warm the cache
    target = counting_codex.root / "s1.jsonl"
    st = target.stat()
    with target.open("a", encoding="utf-8") as fh:
        fh.write('{"n": 2}\n')
    # Restore mtime so ONLY size changed.
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    list(iter_events(agent="codex", session="probe-1"))
    assert counting_codex.calls["n"] == 2


def test_new_file_invalidates_core(counting_codex: SimpleNamespace) -> None:
    """A file appearing under the root forces a fresh core-cache scan."""
    list(iter_events(agent="codex", session="probe-1"))  # warm the cache
    (counting_codex.root / "s2.jsonl").write_text('{"n": 2}\n', encoding="utf-8")
    list(iter_events(agent="codex", session="probe-1"))
    assert counting_codex.calls["n"] == 2
