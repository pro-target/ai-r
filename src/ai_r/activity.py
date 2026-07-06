"""Session *recency* classification (A3) — an honest freshness fact, no liveness.

``list_sessions`` already reports each session's ``date`` — the last-activity
timestamp (an in-file timestamp when the format records one, otherwise file
mtime / DB ``time_updated``).  A supervising poller wants a cheap, explicit
signal of *how long ago* that activity was, so it can decide when a session
looks like it may have stalled.

**Honest contract (F1.1 — never fabricate what the file cannot show).**  A
session *file* cannot tell us whether the producing process is still alive.
It can only tell us *when the last record was written*.  So this module
reports exactly that and nothing more:

* ``age_sec`` — whole seconds since the last recorded activity;
* ``activity`` — ``"fresh"`` when that age is at or under a threshold,
  ``"stale"`` when it is past the threshold.

``activity`` is a statement about the *recency of the last written record*,
**not** about process liveness.  "Running but silent" vs. "crashed" is a
consumer-side inference: correlate ``activity == "stale"`` with an OS signal
(is the pid still alive?).  ai-r deliberately does not make that call — it
would be fabricating a fact the transcript does not contain.

The classifier (:func:`session_activity`) takes ``now`` as an argument and
never reads the clock itself, so it stays pure and hermetically testable; the
MCP surface samples the real clock once per call and passes it in.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ai_r.semantic import _positive_float_env

__all__ = [
    "FRESH",
    "STALE",
    "STALL_SEC_ENV",
    "DEFAULT_STALL_SEC",
    "stall_seconds",
    "session_activity",
]

# Freshness labels.  ``activity`` is one of these two — a recency verdict, not
# a liveness verdict (see module docstring).
FRESH = "fresh"
STALE = "stale"

# Env var overriding the fresh/stale threshold, in seconds.
STALL_SEC_ENV = "AI_R_STALL_SEC"
# Default threshold: 10 minutes.  Long enough that a session mid-turn (model
# thinking, a slow tool) is not flagged as stale on every poll; short enough
# that a genuinely idle/hung session surfaces to a supervisor within minutes.
DEFAULT_STALL_SEC = 600.0


def stall_seconds() -> float:
    """Fresh/stale threshold: ``AI_R_STALL_SEC`` or the default (seconds).

    Blank / unparseable / ``<= 0`` values fall back to
    :data:`DEFAULT_STALL_SEC` without crashing (same env-parsing contract as
    the semantic knobs — DRY, via :func:`ai_r.semantic._positive_float_env`).
    """
    return _positive_float_env(STALL_SEC_ENV, DEFAULT_STALL_SEC)


def session_activity(
    last_activity: datetime, now: datetime, stale_sec: float
) -> dict:
    """Classify how long ago a session last recorded activity — pure.

    Args:
        last_activity: The session's last-activity timestamp (``session.date``:
            an in-file timestamp, else file mtime / DB ``time_updated``).  May
            be naive or tz-aware; naive is treated as UTC.
        now: The reference "current" time, supplied by the caller (this
            function never reads the clock, so tests can feed a fixed value).
            Naive is treated as UTC.
        stale_sec: The fresh→stale threshold in seconds.  ``age_sec`` strictly
            greater than this is ``"stale"``; at or below is ``"fresh"``.

    Returns:
        ``{"age_sec": int, "activity": "fresh" | "stale"}``.  ``age_sec`` is
        whole seconds of ``now - last_activity``, clamped at ``0`` from below:
        a ``last_activity`` in the future (clock skew between the writer and
        this reader) yields ``age_sec == 0`` rather than a negative age, and
        is therefore ``"fresh"``.

    Honesty note: ``activity`` describes the recency of the last written
    record only — it does **not** assert whether the producing process is
    alive.  "Stale" means "nothing written for a while", not "crashed".
    """
    if last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    delta = (now - last_activity).total_seconds()
    # Clamp future timestamps (clock skew) to 0 rather than reporting a
    # negative age — a record cannot honestly be "-30s old".
    age_sec = int(delta) if delta > 0.0 else 0
    activity = STALE if age_sec > stale_sec else FRESH
    return {"age_sec": age_sec, "activity": activity}
