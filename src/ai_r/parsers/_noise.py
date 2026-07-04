"""Session-level noise criterion — the single source of truth (SSOT).

A session is *noise* when it is not a top-level human-driven conversation:
today that means **spawned subagent (sidechain) sessions** — ``kind ==
"subagent"`` or a non-empty ``parent_uuid``.  Both signals are checked so a
parser that fills only one of them still classifies correctly.

Scope decision (F1.2): warmup / scaffold sessions are **not** classified as
noise.  No parser exposes a reliable, cheap signal for them (no flag in the
source records, and title heuristics like "Untitled" would misfire on real
work sessions), so rather than guess we keep the criterion exact:
noise == subagent.  If a real warmup marker appears in some agent's format,
add it here — every consumer (``query`` / ``list_sessions`` /
``search_sessions``) picks it up automatically.

The ``noise`` parameter contract shared by the public surface:

* ``"include"`` (default) — no filtering, noise sessions are returned too.
* ``"exclude"`` — drop noise sessions, keep top-level agent sessions.
* ``"only"``    — keep only noise sessions (audit the subagent tree).
"""

from __future__ import annotations

from .models import Session

#: Accepted values for the public ``noise`` parameter, in doc order.
NOISE_MODES = ("exclude", "include", "only")


def is_noise(session: Session) -> bool:
    """True when ``session`` is a noise (non-top-level) session.

    Criterion: the session is a spawned subagent — ``kind == "subagent"``
    or ``parent_uuid`` set.  Either signal alone is sufficient.
    """
    return session.kind == "subagent" or bool(session.parent_uuid)


def validate_noise(noise: str) -> str:
    """Return ``noise`` if valid, else raise :class:`ValueError` (fail-loud)."""
    if noise not in NOISE_MODES:
        raise ValueError(
            f"noise must be one of {'/'.join(NOISE_MODES)}, got {noise!r}"
        )
    return noise


def noise_allows(session: Session, noise: str) -> bool:
    """Apply the ``noise`` mode to one session (assumes a validated mode)."""
    if noise == "include":
        return True
    noisy = is_noise(session)
    return noisy if noise == "only" else not noisy


__all__ = ["NOISE_MODES", "is_noise", "noise_allows", "validate_noise"]
