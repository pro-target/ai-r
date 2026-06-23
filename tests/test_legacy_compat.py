"""Tests for ``ai_reader.legacy_compat`` — the backward-compat shim.

The shim's job is to be *conservative*: it must never silently change
the behaviour of the legacy ``get_latest_context.py`` and
``agent-audit.py`` scripts.  These tests pin down the rules:

* If ``ai-reader`` is missing → return ``None`` (caller falls back).
* If any requested flag has no ``ai-reader`` equivalent → return ``None``.
* Otherwise → run ``ai-reader`` and propagate its exit code.

We monkey-patch ``subprocess.run`` and the availability helper so the
tests are hermetic — no real ``ai-reader`` binary is invoked.
"""

from __future__ import annotations

import subprocess
import sys
from typing import List
from unittest import mock

import pytest

from ai_reader import legacy_compat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_run(calls: List[List[str]], returncode: int = 0):
    """Return a mock that records ``subprocess.run`` calls."""

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        completed = mock.Mock()
        completed.returncode = returncode
        return completed

    return _fake_run


def _set_argv(*pieces: str) -> None:
    sys.argv = ["script.py", *pieces]


@pytest.fixture
def clean_argv():
    """Restore ``sys.argv`` after each test."""
    original = list(sys.argv)
    yield
    sys.argv = original


def _available(monkeypatch, on: bool = True) -> None:
    monkeypatch.setattr(legacy_compat, "is_ai_reader_available", lambda: on)


# ---------------------------------------------------------------------------
# Availability helper
# ---------------------------------------------------------------------------


def test_is_ai_reader_available_uses_path(monkeypatch):
    monkeypatch.setattr(legacy_compat.shutil, "which", lambda name: "/usr/bin/x" if name == "ai-reader" else None)
    assert legacy_compat.is_ai_reader_available() is True

    monkeypatch.setattr(legacy_compat.shutil, "which", lambda name: None)
    assert legacy_compat.is_ai_reader_available() is False


# ---------------------------------------------------------------------------
# get_latest_context
# ---------------------------------------------------------------------------


def test_get_latest_context_returns_none_when_ai_reader_missing(monkeypatch, clean_argv):
    _available(monkeypatch, on=False)
    _set_argv("--agent", "OPENCODE")
    assert legacy_compat.run_legacy_get_latest_context() is None


def test_get_latest_context_id_without_agent_falls_back(monkeypatch, clean_argv):
    """``--id`` without ``--agent`` needs cross-source lookup that
    ``ai-reader read`` cannot do."""
    _available(monkeypatch)
    _set_argv("--id", "ses_abc")
    assert legacy_compat.run_legacy_get_latest_context() is None


def test_get_latest_context_id_with_unsupported_agent_falls_back(monkeypatch, clean_argv):
    """ROO has no ``ai-reader`` equivalent → fallback."""
    _available(monkeypatch)
    _set_argv("--id", "ses_abc", "--agent", "ROO")
    assert legacy_compat.run_legacy_get_latest_context() is None


def test_get_latest_context_fuzzy_falls_back(monkeypatch, clean_argv):
    """``--fuzzy`` is a legacy-only feature."""
    _available(monkeypatch)
    _set_argv("--id", "ses_abc", "--agent", "OPENCODE", "--fuzzy")
    assert legacy_compat.run_legacy_get_latest_context() is None


def test_get_latest_context_limit_falls_back(monkeypatch, clean_argv):
    """``ai-reader list`` has no ``--limit``; legacy must be used to
    honour the requested truncation."""
    _available(monkeypatch)
    _set_argv("--agent", "OPENCODE", "--limit", "5")
    assert legacy_compat.run_legacy_get_latest_context() is None


def test_get_latest_context_id_with_agent_invokes_ai_reader(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv("--id", "ses_abc", "--agent", "OPENCODE")
    rc = legacy_compat.run_legacy_get_latest_context()
    assert rc == 0
    assert calls == [["ai-reader", "read", "--agent", "opencode", "ses_abc"]]


def test_get_latest_context_agent_only_invokes_list(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv("--agent", "CLAUDE")
    rc = legacy_compat.run_legacy_get_latest_context()
    assert rc == 0
    assert calls == [["ai-reader", "list", "--agent", "claude"]]


def test_get_latest_context_all_agents_invokes_list(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv("--all-agents")
    rc = legacy_compat.run_legacy_get_latest_context()
    assert rc == 0
    assert calls == [["ai-reader", "list"]]


def test_get_latest_context_no_args_falls_back(monkeypatch, clean_argv):
    """No flags → legacy uses ``CURRENT_AGENT`` env var; ai-reader's
    no-flag list is a different default.  Fall back."""
    _available(monkeypatch)
    _set_argv()
    assert legacy_compat.run_legacy_get_latest_context() is None


# ---------------------------------------------------------------------------
# agent-audit
# ---------------------------------------------------------------------------


def test_audit_legacy_only_flag_falls_back(monkeypatch, clean_argv):
    _available(monkeypatch)
    _set_argv("--agent", "OPENCODE", "--stats")
    assert legacy_compat.run_legacy_agent_audit() is None


def test_audit_search_falls_back(monkeypatch, clean_argv):
    """``--search`` is content search in legacy, title search in
    ai-reader — different semantics, must fall back."""
    _available(monkeypatch)
    _set_argv("--search", "docker")
    assert legacy_compat.run_legacy_agent_audit() is None


def test_audit_limit_falls_back(monkeypatch, clean_argv):
    _available(monkeypatch)
    _set_argv("--agent", "OPENCODE", "--limit", "5")
    assert legacy_compat.run_legacy_agent_audit() is None


def test_audit_agent_only_invokes_list(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv("--agent", "OPENCODE")
    rc = legacy_compat.run_legacy_agent_audit()
    assert rc == 0
    assert calls == [["ai-reader", "list", "--agent", "opencode"]]


def test_audit_id_with_agent_invokes_read(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv("--id", "ses_abc", "--agent", "OPENCODE")
    rc = legacy_compat.run_legacy_agent_audit()
    assert rc == 0
    assert calls == [["ai-reader", "read", "--agent", "opencode", "ses_abc"]]


def test_audit_id_with_agent_and_fuzzy_invokes_read(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv("--id", "46d7b4fc", "--agent", "CLAUDE", "--fuzzy")
    rc = legacy_compat.run_legacy_agent_audit()
    assert rc == 0
    assert calls == [["ai-reader", "read", "--agent", "claude", "46d7b4fc"]]


def test_audit_id_with_fuzzy_invokes_cross_agent_read(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv("--id", "46d7b4fc", "--fuzzy")
    rc = legacy_compat.run_legacy_agent_audit()
    assert rc == 0
    assert calls == [["ai-reader", "read", "46d7b4fc"]]


def test_audit_no_args_invokes_list(monkeypatch, clean_argv):
    _available(monkeypatch)
    calls: List[List[str]] = []
    monkeypatch.setattr(legacy_compat.subprocess, "run", _stub_run(calls, returncode=0))
    _set_argv()
    rc = legacy_compat.run_legacy_agent_audit()
    assert rc == 0
    assert calls == [["ai-reader", "list"]]


def test_audit_returns_none_when_ai_reader_missing(monkeypatch, clean_argv):
    _available(monkeypatch, on=False)
    _set_argv()
    assert legacy_compat.run_legacy_agent_audit() is None


# ---------------------------------------------------------------------------
# _run_ai_reader plumbing
# ---------------------------------------------------------------------------


def test_run_ai_reader_propagates_exit_code(monkeypatch):
    captured = mock.Mock(returncode=42)
    fake = mock.Mock(return_value=captured)
    monkeypatch.setattr(legacy_compat.subprocess, "run", fake)
    assert legacy_compat._run_ai_reader(["list"]) == 42
    fake.assert_called_once()
    args, kwargs = fake.call_args
    assert args[0] == ["ai-reader", "list"]
    assert kwargs["stdout"] is sys.stdout
    assert kwargs["stderr"] is sys.stderr
    assert kwargs["check"] is False


def test_run_ai_reader_handles_missing_binary(monkeypatch):
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("ai-reader")

    monkeypatch.setattr(legacy_compat.subprocess, "run", boom)
    assert legacy_compat._run_ai_reader(["list"]) == 127
