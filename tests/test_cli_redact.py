"""Hermetic regression tests for CLI-side secret redaction (F2.1).

Audit PR #4 defect #2: ``ai-r read`` / ``export rounds`` / ``list`` printed
RAW session-derived text, bypassing the redaction pass that the MCP server
applies by default.  A pasted secret in a transcript therefore leaked verbatim
through the CLI.  These tests seed a fake ``AI_R_HOME`` with a Claude session
that carries a secret in both the title and the message body, then assert:

* the CLI masks the secret by default (mirrors MCP ``redact=true``);
* ``--no-redact`` / ``--raw`` is a deliberate, symmetric opt-out that shows raw.

Redaction reuses ``ai_r.redact`` (the SAME functions the MCP wrappers call),
so a passing redaction unit-test suite plus these tests prove the CLI is wired
to the shared boundary rather than reimplementing masking.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import pytest

from ai_r import cli as cli_module


# A synthetic secret that trips the GITHUB_TOKEN pattern (``ghp_`` + 36 chars
# incl. a digit).  It never existed anywhere real; it only needs to LOOK like a
# leaked token so the redaction pass fires.  Split so the source line is not a
# scannable ``_SECRET = "<long>"`` assignment; the runtime value is unchanged.
_SECRET = "ghp_" + "0abcdefghijklmnopqrstuvwxyz0123456789"
_REDACTED = "[REDACTED_GITHUB_TOKEN]"

_ENV_KEYS = ("AI_R_HOME", "OPENCODE_DB")


def _run_inproc(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Run ``cli.main(argv)`` in-process; return (rc, stdout, stderr)."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    try:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli_module.main(argv)
            except SystemExit as exc:  # argparse may exit
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def secret_session(tmp_sessions_dir: Path) -> str:
    """Seed a Claude session whose title AND body carry a secret token."""
    uuid = "redact-cli-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-a" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            # First user text becomes the session title in the Claude parser,
            # so the secret rides both the title and the message body surface.
            "message": {"role": "user", "content": f"my token is {_SECRET}"},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"stored {_SECRET} ok"}],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": uuid,
        },
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return uuid


# ---------------------------------------------------------------------------
# read — the primary defect surface
# ---------------------------------------------------------------------------


def test_read_messages_masks_secret_by_default(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        ["read", "--agent", "claude", "--messages", secret_session],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert _SECRET not in out, "raw secret leaked through `read --messages`"
    assert _REDACTED in out


def test_read_json_masks_secret_by_default(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        ["read", "--agent", "claude", "--json", "--messages", secret_session],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    payload = json.loads(out)
    blob = json.dumps(payload)
    assert _SECRET not in blob, "raw secret leaked through `read --json`"
    assert _REDACTED in blob
    # Title is session-derived text too — it must be masked.
    assert _SECRET not in payload["title"]


def test_read_no_redact_shows_raw(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        [
            "read",
            "--agent",
            "claude",
            "--messages",
            "--no-redact",
            secret_session,
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert _SECRET in out, "--no-redact must show the raw secret"
    assert _REDACTED not in out


def test_read_raw_alias_shows_raw(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        ["read", "--agent", "claude", "--messages", "--raw", secret_session],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert _SECRET in out


# ---------------------------------------------------------------------------
# export rounds — markdown body surface
# ---------------------------------------------------------------------------


def test_export_rounds_masks_secret_by_default(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        [
            "export",
            "rounds",
            "--agent",
            "claude",
            "--include-round",
            secret_session,
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert _SECRET not in out, "raw secret leaked through `export rounds`"
    assert _REDACTED in out


def test_export_rounds_no_redact_shows_raw(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        [
            "export",
            "rounds",
            "--agent",
            "claude",
            "--include-round",
            "--no-redact",
            secret_session,
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert _SECRET in out


def test_export_rounds_output_file_masks_secret(
    secret_session: str, tmp_sessions_dir: Path, tmp_path: Path
) -> None:
    """The ``--output PATH`` file surface must be masked too, not just stdout."""
    out_path = tmp_path / "rounds.md"
    rc, _out, _err = _run_inproc(
        [
            "export",
            "rounds",
            "--agent",
            "claude",
            "--include-round",
            "--output",
            str(out_path),
            secret_session,
        ],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    written = out_path.read_text(encoding="utf-8")
    assert _SECRET not in written, "raw secret leaked into the exported file"
    assert _REDACTED in written


# ---------------------------------------------------------------------------
# list — title surface
# ---------------------------------------------------------------------------


def test_list_masks_secret_in_title(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        ["list", "--agent", "claude", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    payload = json.loads(out)
    blob = json.dumps(payload)
    assert _SECRET not in blob, "raw secret leaked through `list` title"
    assert _REDACTED in blob


def test_list_no_redact_shows_raw_title(
    secret_session: str, tmp_sessions_dir: Path
) -> None:
    rc, out, _err = _run_inproc(
        ["list", "--agent", "claude", "--json", "--no-redact"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert _SECRET in json.dumps(json.loads(out))
