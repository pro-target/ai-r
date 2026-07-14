"""``session_stats(group_by="model")`` — the dimension a cost audit groups by.

Two layers, on purpose:

* the pure ``group_key`` unit (what a bucket is called, and what is NEVER
  guessed);
* the PUBLIC surface — ``session_stats`` and ``ai-r stats --group-by model``
  on a hermetic vault.  A dimension the core supports but the verb (or the
  CLI's argparse choice list) rejects is not a shipped feature, and only an
  end-to-end call catches that.

Hermetic: sessions are written under the per-test ``AI_R_HOME`` (autouse
``_isolate_ai_r_home``); the non-Claude parsers are stubbed empty because a
few of them reach host data through their own discovery.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_r import cli as cli_module
from ai_r.session_stats import GROUP_BY, group_key, session_stats


# ---------------------------------------------------------------------------
# group_key() — pure unit
# ---------------------------------------------------------------------------


def _sess(models):
    return SimpleNamespace(models=models, kind="agent", date=None, uuid="u")


def test_model_is_a_group_by_dimension():
    assert "model" in GROUP_BY


def test_single_model_buckets_by_its_name():
    assert group_key(_sess(("claude-haiku-4-5",)), "model") == "claude-haiku-4-5"


def test_mixed_session_is_not_attributed_to_one_model():
    key = group_key(_sess(("claude-opus-4-8", "claude-haiku-4-5")), "model")
    assert key == "(mixed)"


def test_missing_model_is_unknown_not_guessed():
    assert group_key(_sess(()), "model") == "(unknown)"
    assert group_key(_sess(None), "model") == "(unknown)"


# ---------------------------------------------------------------------------
# Public surface — session_stats + CLI on a hermetic vault
# ---------------------------------------------------------------------------


def _write_session(
    tmp_sessions_dir: Path, uuid: str, models: list[str | None]
) -> None:
    """One Claude session with one assistant turn per entry in ``models``.

    A ``None`` entry writes an assistant record carrying NO model signal.
    """
    records: list[dict] = [
        {
            "type": "user",
            "message": {"role": "user", "content": "go"},
            "timestamp": "2026-07-14T10:00:00Z",
            "sessionId": uuid,
        }
    ]
    for i, model in enumerate(models):
        message: dict = {
            "id": f"m{i}",
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
        }
        if model is not None:
            message["model"] = model
        records.append(
            {
                "type": "assistant",
                "message": message,
                "timestamp": f"2026-07-14T10:0{i + 1}:00Z",
                "sessionId": uuid,
            }
        )
    path = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-model" / f"{uuid}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def model_vault(tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Five sessions: two on haiku, one on opus, one that MIXED both, and one
    with no model signal at all."""
    from ai_r.parsers import PARSERS, AgentName

    _write_session(tmp_sessions_dir, "ms-haiku-1", ["claude-haiku-4-5"])
    _write_session(tmp_sessions_dir, "ms-haiku-2", ["claude-haiku-4-5"])
    _write_session(tmp_sessions_dir, "ms-opus-1", ["claude-opus-4-8[1m]"])
    _write_session(
        tmp_sessions_dir,
        "ms-mixed-1",
        ["claude-opus-4-8[1m]", "claude-haiku-4-5"],
    )
    _write_session(tmp_sessions_dir, "ms-nomodel-1", [None])
    for agent_name, parser in PARSERS.items():
        if agent_name is not AgentName.CLAUDE:
            monkeypatch.setattr(parser, "list_sessions", lambda *a, **k: [])
    return tmp_sessions_dir


def test_session_stats_group_by_model_buckets_the_vault(model_vault: Path) -> None:
    """The verb accepts the dimension and rolls the vault up by model.

    The expected buckets come from the seed (2 haiku / 1 opus / 1 mixed / 1
    without a signal), not from a run.
    """
    result = session_stats(group_by="model", agent="claude")

    assert result["group_by"] == "model"
    assert result["totals"]["sessions"] == 5
    counts = {g["group"]: g["sessions"] for g in result["groups"]}
    assert counts == {
        "claude-haiku-4-5": 2,
        "claude-opus-4-8[1m]": 1,
        "(mixed)": 1,
        "(unknown)": 1,
    }


def test_session_stats_group_by_model_never_guesses(model_vault: Path) -> None:
    """On the verb (not just the unit): a mixed session is attributed to
    NEITHER of its models, and a session with no signal to no model at all."""
    counts = {
        g["group"]: g["sessions"]
        for g in session_stats(group_by="model", agent="claude")["groups"]
    }
    # Had the mixed session leaked into its models' buckets, these would be 3/2.
    assert counts["claude-haiku-4-5"] == 2
    assert counts["claude-opus-4-8[1m]"] == 1


def _run_cli(argv: list[str], home: Path) -> tuple[int, str]:
    """Run ``ai-r <argv>`` in-process against the hermetic home."""
    saved = os.environ.get("AI_R_HOME")
    os.environ["AI_R_HOME"] = str(home)
    os.environ.pop("OPENCODE_DB", None)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            try:
                rc = cli_module.main(argv)
            except SystemExit as exc:  # argparse exits on a bad --group-by
                rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        if saved is None:
            os.environ.pop("AI_R_HOME", None)
        else:
            os.environ["AI_R_HOME"] = saved
    return rc, out.getvalue()


def test_cli_stats_accepts_group_by_model(model_vault: Path) -> None:
    """``ai-r stats --group-by model``: the CLI's argparse choice list must
    carry the dimension too, else the call dies before any code runs."""
    rc, stdout = _run_cli(
        ["stats", "--group-by", "model", "--agent", "claude", "--json"],
        model_vault,
    )
    assert rc == 0, stdout
    payload = json.loads(stdout)
    assert payload["group_by"] == "model"
    counts = {g["group"]: g["sessions"] for g in payload["groups"]}
    assert counts["claude-haiku-4-5"] == 2
    assert counts["(mixed)"] == 1
