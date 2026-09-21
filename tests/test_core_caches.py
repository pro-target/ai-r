"""Hermetic tests for the core per-process read caches (F1, RCA RC-1).

Spec: every hot core call (``iter_events`` → ``get_body`` / ``query`` / the
plan projections / ``audit_brief``) used to re-run the parser's full corpus
scan AND a full message parse on every invocation.  The core now keeps two
shared caches in :mod:`ai_r.parsers._common`:

* the INVENTORY cache — ``list_sessions`` per agent, revalidated by a
  stat-only signature of the parser's ``source_roots()`` (moved verbatim
  from ``mcp_server``; the MCP ``list_sessions`` wrapper now shares it), and
* a bounded LRU ``read_messages`` cache keyed by
  ``(agent, uuid, path, mtime_ns, size)``.

Freshness contract (same as the haystack cache): a HIT must be
byte-identical to a MISS — any change (new/removed file, mtime bump, or a
same-mtime size change) alters the signature/key and forces a fresh
scan/parse.  Fail-open: a session the inventory cannot resolve, or a source
path that cannot be statted, bypasses the cache entirely (never a pinned
stale answer, never a crash).

The counting fake parser is mounted on the Claude slot over a per-test temp
root, so every case is hermetic and the seams observe ONLY this test's
scans/parses.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_r.events.model import iter_events
from ai_r.parsers import AgentName, PARSERS, Session
from ai_r.parsers import _common


@pytest.fixture(autouse=True)
def _clear_core_caches():
    """Isolate every case from warm entries left by earlier tests."""

    def _clear() -> None:
        _common._agent_sessions_cache.clear()
        _common._msg_cache.clear()
        _common._msg_cache_bytes = 0

    _clear()
    yield
    _clear()


def _make_parser(root: Path, calls: dict) -> SimpleNamespace:
    """A counting fake parser over a real (temp) source root.

    ``list_sessions`` stamps the scan ordinal into the session title and
    ``read_messages`` stamps the parse ordinal into the message text, so a
    served-from-cache result is distinguishable by CONTENT, not just by
    call count.  Two sessions share the root so LRU interactions are
    observable.
    """
    root.mkdir(parents=True, exist_ok=True)
    s1 = root / "s1.jsonl"
    s2 = root / "s2.jsonl"
    s1.write_text('{"n": 1}\n', encoding="utf-8")
    s2.write_text('{"n": 2}\n', encoding="utf-8")

    def fake_list_sessions(base_dir=None):
        calls["scan"] += 1
        return [
            Session(
                uuid="cache-probe-1",
                agent=AgentName.CLAUDE,
                title=f"probe scan {calls['scan']}",
                date=datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc),
                path=str(s1),
                message_count=1,
            ),
            Session(
                uuid="cache-probe-2",
                agent=AgentName.CLAUDE,
                title=f"probe scan {calls['scan']}",
                date=datetime(2026, 6, 14, 10, 1, tzinfo=timezone.utc),
                path=str(s2),
                message_count=1,
            ),
        ]

    def fake_read_messages(uuid, base_dir=None):
        calls["parse"] += 1
        name = "s1.jsonl" if uuid == "cache-probe-1" else "s2.jsonl"
        return [
            SimpleNamespace(role="user", text=f"parse {calls['parse']} of {name}")
        ]

    return SimpleNamespace(
        list_sessions=fake_list_sessions,
        read_messages=fake_read_messages,
        source_roots=lambda base_dir=None: [str(root)],
    )


# --- (a) inventory cache: iter_events must not rescan a warm corpus ----------


def test_iter_events_warm_repeat_skips_rescan_and_reparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second ``iter_events`` over an unchanged root: 0 extra scans, 0 parses."""
    calls = {"scan": 0, "parse": 0}
    parser = _make_parser(tmp_path / "root", calls)
    monkeypatch.setitem(PARSERS, AgentName.CLAUDE, parser)

    list(iter_events(agent="claude", session="cache-probe-1"))
    out2 = list(iter_events(agent="claude", session="cache-probe-1"))

    assert calls["scan"] == 1
    assert calls["parse"] == 1
    # Warm content is the FIRST parse's, byte-identical (served from cache).
    assert out2[0].text == "parse 1 of s1.jsonl"


def test_iter_events_mtime_bump_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed file mtime is a changed signature → fresh scan + reparse."""
    calls = {"scan": 0, "parse": 0}
    root = tmp_path / "root"
    parser = _make_parser(root, calls)
    monkeypatch.setitem(PARSERS, AgentName.CLAUDE, parser)

    list(iter_events(agent="claude", session="cache-probe-1"))
    os.utime(root / "s1.jsonl", ns=(1, 1))
    list(iter_events(agent="claude", session="cache-probe-1"))

    assert calls["scan"] == 2
    assert calls["parse"] == 2


# --- (b) read_messages LRU cache ---------------------------------------------


def test_read_messages_warm_repeat_returns_same_object(
    tmp_path: Path,
) -> None:
    """Warm repeat: one parse, the SAME list object (shared, treat immutable)."""
    calls = {"scan": 0, "parse": 0}
    parser = _make_parser(tmp_path / "root", calls)

    first = _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")
    second = _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")

    assert calls["parse"] == 1
    assert first is second


def test_read_messages_same_mtime_size_change_invalidates(
    tmp_path: Path,
) -> None:
    """An append with a preserved mtime still invalidates (size is signed)."""
    calls = {"scan": 0, "parse": 0}
    root = tmp_path / "root"
    parser = _make_parser(root, calls)
    target = root / "s1.jsonl"

    _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")
    st = target.stat()
    with target.open("a", encoding="utf-8") as fh:
        fh.write('{"n": 3}\n')
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")

    assert calls["parse"] == 2


def test_read_messages_lru_evicts_by_entry_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap of 1 entry: alternating sessions re-parse the evicted one."""
    monkeypatch.setattr(_common, "_MSG_CACHE_MAX", 1)
    calls = {"scan": 0, "parse": 0}
    parser = _make_parser(tmp_path / "root", calls)

    _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")
    _common.cached_read_messages("CLAUDE", parser, "cache-probe-2")
    _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")

    assert calls["parse"] == 3
    assert len(_common._msg_cache) == 1


def test_read_messages_lru_evicts_by_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summed source bytes over the cap evict the LRU entry (newest kept)."""
    monkeypatch.setattr(_common, "_MSG_CACHE_MAX", 64)
    # Each fixture file is 9 bytes; a 10-byte cap fits one entry, not two.
    monkeypatch.setattr(_common, "_MSG_CACHE_BYTES_MAX", 10)
    calls = {"scan": 0, "parse": 0}
    parser = _make_parser(tmp_path / "root", calls)

    _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")
    _common.cached_read_messages("CLAUDE", parser, "cache-probe-2")
    _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")

    assert calls["parse"] == 3
    assert len(_common._msg_cache) == 1


def test_read_messages_unknown_uuid_bypasses_cache(tmp_path: Path) -> None:
    """A uuid the inventory cannot resolve is read directly every time."""
    calls = {"scan": 0, "parse": 0}
    parser = _make_parser(tmp_path / "root", calls)

    _common.cached_read_messages("CLAUDE", parser, "cache-probe-42")
    _common.cached_read_messages("CLAUDE", parser, "cache-probe-42")

    assert calls["parse"] == 2
    assert not _common._msg_cache


def test_read_messages_unstattable_path_bypasses_cache(
    tmp_path: Path,
) -> None:
    """A session path that cannot be statted never pins stale content."""
    calls = {"scan": 0, "parse": 0}
    root = tmp_path / "root"
    parser = _make_parser(root, calls)
    gone = root / "gone.jsonl"

    def listing_with_ghost(base_dir=None):
        sessions = parser.list_sessions(base_dir)
        return sessions + [
            Session(
                uuid="cache-ghost",
                agent=AgentName.CLAUDE,
                title="ghost",
                date=datetime(2026, 6, 14, 10, 2, tzinfo=timezone.utc),
                path=str(gone),
                message_count=0,
            )
        ]

    ghost_parser = SimpleNamespace(
        list_sessions=listing_with_ghost,
        read_messages=parser.read_messages,
        source_roots=parser.source_roots,
    )

    _common.cached_read_messages("CLAUDE", ghost_parser, "cache-ghost")
    _common.cached_read_messages("CLAUDE", ghost_parser, "cache-ghost")

    assert calls["parse"] == 2


def test_read_messages_thread_safety_smoke(tmp_path: Path) -> None:
    """Concurrent readers: no crash, bounded parses, stable warm identity.

    The shared http server dispatches sync tools on up to 40 worker threads,
    so both caches must be safe under concurrent mixed-uuid access.  A tiny
    parse delay widens the race window on purpose; the assertions accept
    benign double-parses (last writer wins, both fresh) but require a
    consistent object identity once warm and a cache size within the cap.
    """
    calls = {"scan": 0, "parse": 0}
    parser = _make_parser(tmp_path / "root", calls)
    real_read = parser.read_messages

    def slow_read(uuid, base_dir=None):
        time.sleep(0.002)
        return real_read(uuid, base_dir)

    parser.read_messages = slow_read  # type: ignore[method-assign]

    uuids = ("cache-probe-1", "cache-probe-2")
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        for _ in range(25):
            msgs = _common.cached_read_messages("CLAUDE", parser, uuids[i % 2])
            assert msgs, "cache returned an empty message list"

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    # Warm identity: after the dust settles, repeats serve the same object.
    a = _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")
    b = _common.cached_read_messages("CLAUDE", parser, "cache-probe-1")
    assert a is b
    assert calls["parse"] >= 2  # at least the two cold misses
    assert calls["parse"] <= 2 + 8  # benign racing double-parses only
    assert len(_common._msg_cache) <= 2
