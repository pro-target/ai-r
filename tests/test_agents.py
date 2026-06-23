"""Tests for runtime agent detection (env-var cascade)."""

from __future__ import annotations

import pytest

from ai_reader.agents import detect_agent, detect_agent_strict
from ai_reader.parsers.models import AgentName


_DETECT_VARS = (
    "AGENT_NAME",
    "AI_AGENT",
    "CODING_AGENT",
    "CODEX_HOME",
    "CLAUDECODE",
    "OPENCODE",
)


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every detection var so each test starts from zero."""
    for var in _DETECT_VARS:
        monkeypatch.delenv(var, raising=False)


def test_detect_agent_from_envvar(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_NAME", "claude")
    assert detect_agent() == AgentName.CLAUDE


def test_detect_agent_fallback_chain(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", "/foo")
    assert detect_agent() == AgentName.CODEX


def test_detect_agent_returns_none(_clean_env: None) -> None:
    assert detect_agent() is None


def test_detect_agent_strict_raises_when_missing(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ai_reader.agents.detect_agent", lambda: None
    )
    with pytest.raises(RuntimeError):
        detect_agent_strict()


def test_named_var_higher_priority_than_marker(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_NAME", "opencode")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert detect_agent() == AgentName.OPENCODE


def test_invalid_named_value_falls_through(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_NAME", "garbage")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert detect_agent() == AgentName.CLAUDE
