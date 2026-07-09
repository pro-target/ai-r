"""Hermetic regression tests for haystack-cache eviction (Defect #6).

Two invariants the audit flagged:

1. **No dead-key pileup on mtime change.** When a session file is rewritten
   its mtime advances → a new cache key. The *previous* ``(agent, uuid,
   old_mtime)`` entry must be purged, not left wedged in the LRU until the
   count cap eventually reaches it (that was the 00e4248 regression).
2. **Bounded by summed size, not just entry count.** A long-lived shared
   server must cap the total haystack chars held, so many large sessions
   cannot balloon RSS to GiB.

The cache is a module-global; each test snapshots and restores it so the
suite stays order-independent and touches no host data.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_r import mcp_server as m


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    """Give each test a fresh, small-capped cache; restore globals after."""
    from collections import OrderedDict

    saved_cache = m._haystack_cache
    saved_chars = m._haystack_cache_chars
    saved_max = m._HAYSTACK_CACHE_MAX
    saved_chars_max = m._HAYSTACK_CACHE_CHARS_MAX

    m._haystack_cache = OrderedDict()
    m._haystack_cache_chars = 0
    yield
    m._haystack_cache = saved_cache
    m._haystack_cache_chars = saved_chars
    m._HAYSTACK_CACHE_MAX = saved_max
    m._HAYSTACK_CACHE_CHARS_MAX = saved_chars_max


def _store(agent: str, uuid: str, mtime: float, text: str) -> None:
    with m._haystack_cache_lock:
        m._haystack_store((agent, uuid, mtime), (text, False))


# --- char-cap resolver (env → int, fail-soft to default) -------------------

def test_chars_max_default() -> None:
    assert m._resolve_haystack_cache_chars_max({}) == m._HAYSTACK_CACHE_CHARS_MAX_DEFAULT


def test_chars_max_env_override() -> None:
    assert m._resolve_haystack_cache_chars_max(
        {"AI_R_HAYSTACK_CACHE_CHARS_MAX": "12345"}
    ) == 12345


@pytest.mark.parametrize("bad", ["0", "-5", "big", ""])
def test_chars_max_bad_falls_back(bad: str) -> None:
    assert m._resolve_haystack_cache_chars_max(
        {"AI_R_HAYSTACK_CACHE_CHARS_MAX": bad}
    ) == m._HAYSTACK_CACHE_CHARS_MAX_DEFAULT


# --- invariant 1: mtime change purges the stale sibling --------------------

def test_mtime_change_purges_old_key() -> None:
    _store("claude", "s1", 100.0, "old-body")
    assert ("claude", "s1", 100.0) in m._haystack_cache

    # Same session, newer mtime → rebuilt entry.
    _store("claude", "s1", 200.0, "new-body")

    keys = list(m._haystack_cache)
    # Exactly ONE live version for (claude, s1): the fresh one.
    assert keys == [("claude", "s1", 200.0)]
    # Char accounting reflects only the surviving entry.
    assert m._haystack_cache_chars == len("new-body")


def test_distinct_sessions_coexist() -> None:
    _store("claude", "s1", 100.0, "aaa")
    _store("claude", "s2", 100.0, "bbbb")
    _store("codex", "s1", 100.0, "cc")  # same uuid, different agent
    assert set(m._haystack_cache) == {
        ("claude", "s1", 100.0),
        ("claude", "s2", 100.0),
        ("codex", "s1", 100.0),
    }
    assert m._haystack_cache_chars == len("aaa") + len("bbbb") + len("cc")


# --- invariant 2: char-based cap -------------------------------------------

def test_char_cap_evicts_oldest() -> None:
    m._HAYSTACK_CACHE_MAX = 1000  # count cap out of the way
    m._HAYSTACK_CACHE_CHARS_MAX = 10

    _store("a", "s1", 1.0, "x" * 6)  # 6 chars
    _store("a", "s2", 1.0, "y" * 6)  # +6 = 12 > 10 → evict oldest (s1)

    keys = list(m._haystack_cache)
    assert ("a", "s1", 1.0) not in keys
    assert ("a", "s2", 1.0) in keys
    assert m._haystack_cache_chars == 6


def test_single_oversize_entry_still_served() -> None:
    """One session larger than the whole cap must remain servable."""
    m._HAYSTACK_CACHE_MAX = 1000
    m._HAYSTACK_CACHE_CHARS_MAX = 10

    _store("a", "big", 1.0, "z" * 50)  # alone exceeds the cap
    assert list(m._haystack_cache) == [("a", "big", 1.0)]
    assert m._haystack_cache_chars == 50


def test_count_cap_still_enforced() -> None:
    m._HAYSTACK_CACHE_MAX = 2
    m._HAYSTACK_CACHE_CHARS_MAX = 10_000_000

    _store("a", "s1", 1.0, "1")
    _store("a", "s2", 1.0, "2")
    _store("a", "s3", 1.0, "3")  # over count cap → oldest (s1) evicted

    keys = list(m._haystack_cache)
    assert ("a", "s1", 1.0) not in keys
    assert len(keys) == 2
    assert m._haystack_cache_chars == len("2") + len("3")


def test_char_sum_stays_consistent_under_churn() -> None:
    """Running char sum must equal the actual sum of live values at all times."""
    m._HAYSTACK_CACHE_MAX = 1000
    m._HAYSTACK_CACHE_CHARS_MAX = 1000
    for i in range(20):
        _store("a", f"s{i % 5}", float(i), "q" * (i + 1))
        live = sum(len(v[0]) for v in m._haystack_cache.values())
        assert m._haystack_cache_chars == live


# --- mtime invalidation still correct end-to-end ---------------------------

def test_get_cached_haystack_rebuilds_on_mtime_change(monkeypatch) -> None:
    """A HIT reuses; an mtime bump forces a rebuild AND drops the old key."""
    builds: list[str] = []

    def fake_build(session):  # _body_search_messages stand-in
        builds.append(session.uuid)
        return ([], False)

    monkeypatch.setattr(m, "_body_search_messages", fake_build)
    monkeypatch.setattr(m, "_build_haystack", lambda msgs, *, include_thinking=False: "H")

    mtime = {"v": 100.0}
    monkeypatch.setattr(m, "_session_source_mtime", lambda s: mtime["v"])

    sess = SimpleNamespace(uuid="s1", path="/fake")

    m._get_cached_haystack(sess, "claude")
    m._get_cached_haystack(sess, "claude")  # HIT — no second build
    assert builds == ["s1"]
    assert list(m._haystack_cache) == [("claude", "s1", 100.0, False)]

    mtime["v"] = 200.0  # file rewritten
    m._get_cached_haystack(sess, "claude")  # MISS — rebuild
    assert builds == ["s1", "s1"]
    # Old-mtime key gone; only the fresh one remains (no dead-key pileup).
    assert list(m._haystack_cache) == [("claude", "s1", 200.0, False)]
