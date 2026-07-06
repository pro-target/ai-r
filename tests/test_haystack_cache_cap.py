"""Hermetic tests for the body-search haystack cache cap resolver.

The cap must be large enough that the shared long-lived server holds a whole
corpus (otherwise a full-corpus body search thrashes the LRU and re-parses
every file). The resolver is pure — env is an argument — so cases feed fixed
dicts and assert exact output.
"""

from __future__ import annotations

from ai_r import mcp_server as m


def test_default_holds_a_large_corpus() -> None:
    """The default must comfortably exceed a real multi-thousand corpus."""
    assert m._HAYSTACK_CACHE_MAX_DEFAULT == 2048
    assert m._resolve_haystack_cache_max({}) == 2048


def test_env_override_positive() -> None:
    assert m._resolve_haystack_cache_max({"AI_R_HAYSTACK_CACHE_MAX": "5000"}) == 5000


def test_env_override_zero_falls_back() -> None:
    """A non-positive cap is nonsensical → default, never a 0-size cache."""
    assert m._resolve_haystack_cache_max({"AI_R_HAYSTACK_CACHE_MAX": "0"}) == 2048


def test_env_override_negative_falls_back() -> None:
    assert m._resolve_haystack_cache_max({"AI_R_HAYSTACK_CACHE_MAX": "-10"}) == 2048


def test_env_override_non_numeric_falls_back() -> None:
    assert m._resolve_haystack_cache_max({"AI_R_HAYSTACK_CACHE_MAX": "big"}) == 2048


def test_env_empty_falls_back() -> None:
    assert m._resolve_haystack_cache_max({"AI_R_HAYSTACK_CACHE_MAX": ""}) == 2048


def test_module_cap_is_resolved_default() -> None:
    """With no env set at import, the live module cap is the default."""
    assert m._HAYSTACK_CACHE_MAX == 2048
