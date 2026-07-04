"""CLI error contract: never a Python traceback (hermetic).

An unexpected exception escaping a subcommand handler must surface as ONE
structured JSON line on stderr (``{"error": "internal_error", ...}``) plus
a non-zero exit code — a consumer script gets a parseable error, not a
stack dump.  ``AI_R_DEBUG=1`` re-raises for debugging.
"""
from __future__ import annotations

import json

import pytest

from ai_r import cli as cli_module
from ai_r.cli.commands import list_cmd


def _boom(args):  # noqa: ANN001 — argparse.Namespace
    raise RuntimeError("kaboom: unexpected internal failure")


def test_unexpected_error_is_structured_not_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AI_R_DEBUG", raising=False)
    monkeypatch.setattr(list_cmd, "_run_list", _boom)

    rc = cli_module.main(["list"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["error"] == "internal_error"
    assert payload["type"] == "RuntimeError"
    assert "kaboom" in payload["message"]


def test_debug_env_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_R_DEBUG", "1")
    monkeypatch.setattr(list_cmd, "_run_list", _boom)
    with pytest.raises(RuntimeError, match="kaboom"):
        cli_module.main(["list"])


def test_keyboard_interrupt_returns_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupt(args):  # noqa: ANN001
        raise KeyboardInterrupt

    monkeypatch.setattr(list_cmd, "_run_list", _interrupt)
    rc = cli_module.main(["list"])
    captured = capsys.readouterr()
    assert rc == 130
    assert "interrupted" in captured.err
    assert "Traceback" not in captured.err


def test_broken_pipe_returns_141(monkeypatch: pytest.MonkeyPatch) -> None:
    def _pipe(args):  # noqa: ANN001
        raise BrokenPipeError

    monkeypatch.setattr(list_cmd, "_run_list", _pipe)
    assert cli_module.main(["list"]) == 141


def test_expected_error_path_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Handled errors keep the historical ``ai-r: <message>`` contract."""
    rc = cli_module.main(["list", "--from-date", "junk"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.startswith("ai-r: ")
    assert "Traceback" not in captured.err
