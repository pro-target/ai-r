"""Hermetic tests for the A3 session-recency classifier (``ai_r.activity``).

The classifier is pure — ``now`` is an argument, never the real clock — so
every case here feeds fixed datetimes and asserts exact output.  No wall-clock
reads, no host data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_r.activity import (
    DEFAULT_STALL_SEC,
    FRESH,
    STALE,
    STALL_SEC_ENV,
    session_activity,
    stall_seconds,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_fresh_below_threshold() -> None:
    """Age at or under the threshold → fresh, integer age."""
    last = _NOW - timedelta(seconds=100)
    result = session_activity(last, _NOW, 600.0)
    assert result == {"age_sec": 100, "activity": FRESH}


def test_stale_above_threshold() -> None:
    """Age strictly past the threshold → stale."""
    last = _NOW - timedelta(seconds=700)
    result = session_activity(last, _NOW, 600.0)
    assert result == {"age_sec": 700, "activity": STALE}


def test_boundary_equal_threshold_is_fresh() -> None:
    """Exactly at the threshold is fresh (stale is strictly greater)."""
    last = _NOW - timedelta(seconds=600)
    result = session_activity(last, _NOW, 600.0)
    assert result == {"age_sec": 600, "activity": FRESH}


def test_boundary_one_second_past_is_stale() -> None:
    """One second past the threshold flips to stale."""
    last = _NOW - timedelta(seconds=601)
    result = session_activity(last, _NOW, 600.0)
    assert result == {"age_sec": 601, "activity": STALE}


def test_clock_skew_future_last_activity_clamps_to_zero() -> None:
    """A last_activity in the future (skew) → age 0, fresh (never negative)."""
    last = _NOW + timedelta(seconds=50)
    result = session_activity(last, _NOW, 600.0)
    assert result == {"age_sec": 0, "activity": FRESH}


def test_naive_last_activity_treated_as_utc() -> None:
    """A naive last_activity is interpreted as UTC (matches parser output)."""
    naive_last = datetime(2026, 1, 1, 11, 55, 0)  # 5 min before _NOW, no tz
    result = session_activity(naive_last, _NOW, 600.0)
    assert result == {"age_sec": 300, "activity": FRESH}


def test_naive_now_treated_as_utc() -> None:
    """A naive now is interpreted as UTC too."""
    naive_now = datetime(2026, 1, 1, 12, 0, 0)
    last = datetime(2026, 1, 1, 11, 45, 0, tzinfo=timezone.utc)  # 15 min prior
    result = session_activity(last, naive_now, 600.0)
    assert result == {"age_sec": 900, "activity": STALE}


def test_fractional_seconds_floored() -> None:
    """age_sec is whole seconds (truncated toward zero)."""
    last = _NOW - timedelta(seconds=100, milliseconds=900)
    result = session_activity(last, _NOW, 600.0)
    assert result["age_sec"] == 100


def test_stall_seconds_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(STALL_SEC_ENV, raising=False)
    assert stall_seconds() == DEFAULT_STALL_SEC


def test_stall_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STALL_SEC_ENV, "120")
    assert stall_seconds() == 120.0


@pytest.mark.parametrize("bad", ["", "   ", "abc", "0", "-5", "nan-ish"])
def test_stall_seconds_bad_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Blank / unparseable / non-positive env → default, never a crash."""
    monkeypatch.setenv(STALL_SEC_ENV, bad)
    assert stall_seconds() == DEFAULT_STALL_SEC


def test_env_threshold_reaches_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tighter env threshold makes an otherwise-fresh age read as stale."""
    monkeypatch.setenv(STALL_SEC_ENV, "60")
    last = _NOW - timedelta(seconds=100)
    result = session_activity(last, _NOW, stall_seconds())
    assert result == {"age_sec": 100, "activity": STALE}
