"""Hermeticity guard — a meta-test that keeps the suite CI-safe.

The failure mode this prevents: a test reads the *real* user home
(``~/.claude``, ``~/.codex`` …) and ``assert``s on data that exists on the
author's laptop but not on a clean CI runner.  That turns ``main`` red even
though nothing is actually broken (this is exactly what happened with the
Codex/OpenCode ``*_real`` tests).

The blessed way to touch real host data is the ``real_*`` session-scoped
fixtures in :mod:`tests.conftest`.  They ``pytest.skip`` when the data is
absent and are auto-tagged ``@pytest.mark.host`` (so the hermetic CI job
``pytest -m "not host"`` deselects them).  Any *other* file that reaches into
the real home with a raw ``expanduser()`` / hard-coded ``~/…`` path is a
latent CI-reddener and is rejected here.

If this test fails, do ONE of:
  * take a ``real_*`` fixture instead of reading the home directly, or
  * seed a fake home (``tmp_sessions_dir``) and pass ``AI_R_HOME``, or
  * if the raw read is genuinely guarded by ``pytest.skip`` when absent,
    add the file's name to ``_ALLOWLIST`` below with a one-line reason.
"""
from __future__ import annotations

from pathlib import Path

# Real-home access patterns.  We match the *mechanism* (``expanduser`` /
# ``Path.home()``) rather than bare ``~/…`` literals: every real read in this
# codebase resolves the home that way, and matching the mechanism avoids false
# positives on path strings that merely appear in docstrings/comments.
_NEEDLES = (
    "expanduser",
    "Path.home(",
)

# Files permitted to read the real home directly.  Each MUST guard the read
# with ``pytest.skip`` when the data is absent.
_ALLOWLIST = {
    "conftest.py",  # defines the real_* probes — the single source of truth
    "test_cli.py",  # _first_claude_uuid(): skips when no real session exists
    "test_mcp.py",  # _first_claude_uuid(): skips when no real session exists
    "test_semantic.py",  # host smoke: skipif when the real model is absent
}

_TESTS_ROOT = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve().name


def test_no_unguarded_real_home_reads() -> None:
    offenders: list[str] = []
    for py in sorted(_TESTS_ROOT.rglob("*.py")):
        if py.name == _SELF or py.name in _ALLOWLIST:
            continue
        if "__pycache__" in py.parts:
            continue
        text = py.read_text(encoding="utf-8")
        hits = [n for n in _NEEDLES if n in text]
        if hits:
            rel = py.relative_to(_TESTS_ROOT)
            offenders.append(f"{rel}: {', '.join(hits)}")

    assert not offenders, (
        "These test files read the real user home directly, which makes them "
        "fail on a clean CI runner. Use a real_* fixture (auto-skips + "
        "auto-marks host) or seed a fake home with AI_R_HOME. If the read is "
        "already guarded by pytest.skip, add the file to _ALLOWLIST.\n  "
        + "\n  ".join(offenders)
    )
