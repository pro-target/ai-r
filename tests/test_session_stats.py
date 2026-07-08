"""Tests for ``ai_r.session_stats`` core.

The grouping/ranking is the only new logic — the underlying inventory
(``list_sessions``) and the edit enrichment
(:func:`ai_r.find_file_edits.find_file_edits`) are exercised by their own
tests; this module covers the group-by dimensions, the ranking tie-break,
the ``totals`` block, argument validation, and the RISK-4 degenerate
``kind`` split flag.

All fixtures come from :mod:`tests.conftest` (the hermetic
``tmp_sessions_dir`` tree); layout mirrors ``tests/test_file_frequency``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_r.session_stats import (
    GROUP_BY,
    TOKEN_SCAN_LIMIT,
    TOKEN_SCAN_WARN,
    group_key,
    session_stats,
)


# ---------------------------------------------------------------------------
# Helpers (local — kept private to this module)
# ---------------------------------------------------------------------------


def _write_claude_edit_session(
    tmp_sessions_dir: Path,
    uuid: str,
    *,
    turns: list[tuple[str, str, str]],
    proj: str = "proj-ss",
    base_ts: str = "2026-06-14T10:0",
) -> None:
    """Write a Claude JSONL of (user_text, tool_name, file_path) edit turns."""
    records: list[dict] = []
    for i, (user_text, tool_name, file_path) in enumerate(turns):
        records.append(
            {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "timestamp": f"{base_ts}{i}:00Z",
                "sessionId": uuid,
            }
        )
        records.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"Editing {file_path}."},
                        {
                            "type": "tool_use",
                            "name": tool_name,
                            "input": {
                                "file_path": file_path,
                                "old_string": "a",
                                "new_string": "b",
                            },
                        },
                    ],
                },
                "timestamp": f"{base_ts}{i}:05Z",
                "sessionId": uuid,
            }
        )
    jsonl = tmp_sessions_dir / ".claude" / "projects" / proj / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _patch_claude(monkeypatch: pytest.MonkeyPatch, tmp_sessions_dir: Path) -> None:
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )


def _silence_non_claude_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every non-Claude parser report an empty inventory.

    ``AI_R_HOME`` isolates most parsers onto the fake temp home, but a few
    (notably OpenCode via its own ``OPENCODE_DB`` discovery) can still reach
    real host data — which would make an *unscoped* rollup's session count
    host-dependent.  Stubbing their ``list_sessions`` to ``[]`` pins an
    unscoped scan to the patched hermetic Claude tree, so these tests stay
    deterministic on any host (host data absent OR present).
    """
    from ai_r.parsers import PARSERS, AgentName

    for agent_name, parser in PARSERS.items():
        if agent_name is AgentName.CLAUDE:
            continue
        monkeypatch.setattr(parser, "list_sessions", lambda *a, **k: [])


# ---------------------------------------------------------------------------
# group_key() — pure unit
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(
        self, agent_value, kind="agent", date=None, extra=None, mc=0,
        project_dir=None,
    ):
        self.agent = type("A", (), {"value": agent_value})()
        self.kind = kind
        self.date = date
        self.extra = extra or {}
        self.message_count = mc
        self.project_dir = project_dir


def test_group_key_dimensions() -> None:
    from datetime import datetime, timezone

    s = _FakeSession(
        "CLAUDE",
        kind="subagent",
        date=datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc),
        extra={"project_slug": "proj-x"},
    )
    assert group_key(s, "agent") == "claude"
    assert group_key(s, "kind") == "subagent"
    assert group_key(s, "dir") == "proj-x"
    assert group_key(s, "date") == "2026-06-14"


def test_group_key_dir_prefers_cwd_over_slug() -> None:
    s = _FakeSession("CODEX", extra={"cwd": "/tmp/work", "project_slug": "p"})
    assert group_key(s, "dir") == "/tmp/work"


def test_group_key_dir_prefers_normalized_project_dir() -> None:
    """``Session.project_dir`` (normalized) wins over every extra fallback."""
    s = _FakeSession(
        "CLAUDE",
        extra={"project_slug": "-home-u-dev-ai-r"},
        project_dir="/home/u/dev/ai-r",
    )
    assert group_key(s, "dir") == "/home/u/dev/ai-r"


def test_group_key_dir_one_directory_one_bucket_across_agents() -> None:
    """The same real directory folds into ONE bucket regardless of agent.

    Before the ``project_dir``-first rule a Claude session (storage slug)
    and a codex session (absolute ``cwd``) from the same project landed in
    two different ``dir`` buckets.
    """
    claude = _FakeSession(
        "CLAUDE",
        extra={"project_slug": "-home-u-dev-ai-r"},
        project_dir="/home/u/dev/ai-r",
    )
    codex = _FakeSession(
        "CODEX",
        extra={"cwd": "/home/u/dev/ai-r"},
        project_dir="/home/u/dev/ai-r",
    )
    assert group_key(claude, "dir") == group_key(codex, "dir")


def test_group_key_dir_extra_fallback_without_project_dir() -> None:
    """No normalized dir → the historical extra fallbacks still apply."""
    s = _FakeSession("CLAUDE", extra={"project_slug": "proj-x"})
    assert group_key(s, "dir") == "proj-x"


def test_group_key_dir_unknown_fallback() -> None:
    s = _FakeSession("OPENCODE", extra={})
    assert group_key(s, "dir") == "(unknown)"


def test_group_key_date_undated() -> None:
    s = _FakeSession("CLAUDE", date=None)
    assert group_key(s, "date") == "(undated)"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_session_stats_unknown_group_by_raises() -> None:
    with pytest.raises(ValueError, match="group_by"):
        session_stats(group_by="mystery")


def test_session_stats_negative_top_raises() -> None:
    with pytest.raises(ValueError, match="top"):
        session_stats(top=-1)


def test_session_stats_bool_top_raises() -> None:
    with pytest.raises(ValueError, match="top"):
        session_stats(top=True)  # type: ignore[arg-type]


def test_session_stats_unknown_agent_raises() -> None:
    with pytest.raises(ValueError, match="agent"):
        session_stats(agent="mystery")


def test_session_stats_bad_since_raises() -> None:
    with pytest.raises(ValueError, match="since"):
        session_stats(since="not-a-date")


def test_group_by_set_is_complete() -> None:
    assert GROUP_BY == {"agent", "dir", "date", "kind"}


# ---------------------------------------------------------------------------
# Core integration (hermetic tree)
# ---------------------------------------------------------------------------


def test_session_stats_no_sessions_empty(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_claude(monkeypatch, tmp_sessions_dir)
    # Scope to claude: only that parser is patched onto the hermetic tree;
    # the other parsers resolve host-default paths (e.g. opencode's
    # ~/.local/share/opencode.db) which AI_R_HOME does not isolate, so a
    # cross-agent session *count* would be host-dependent.
    result = session_stats(group_by="agent", agent="claude")
    assert result["groups"] == []
    assert result["totals"]["sessions"] == 0
    assert result["totals"]["edits"] == 0
    assert result["totals"]["agents"] == 0
    assert result["totals"]["agents_list"] == []
    # No subagents => degenerate split flag + note.
    assert result["kind_split_available"] is False
    assert "note" in result


def test_session_stats_group_by_agent_counts(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two claude sessions, one editing two files, one editing one.
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ss-s1",
        turns=[
            ("change hot", "Edit", "/repo/hot.py"),
            ("change hot again", "Edit", "/repo/hot.py"),
            ("touch cold", "Write", "/repo/cold.py"),
        ],
    )
    _write_claude_edit_session(
        tmp_sessions_dir,
        "ss-s2",
        turns=[("edit other", "Edit", "/repo/other.py")],
        proj="proj-ss2",
        base_ts="2026-06-14T11:0",
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)

    result = session_stats(group_by="agent", agent="claude")
    assert result["group_by"] == "agent"
    assert result["totals"]["sessions"] == 2
    assert result["totals"]["agents"] == 1
    assert result["totals"]["agents_list"] == ["claude"]
    # All edits roll up under the single claude bucket: 3 + 1 = 4.
    assert result["totals"]["edits"] == 4

    assert len(result["groups"]) == 1
    g = result["groups"][0]
    assert g["group"] == "claude"
    assert g["sessions"] == 2
    assert g["edits"] == 4
    assert g["agents"] == ["claude"]
    # Distinct intents across both sessions: 3 distinct user texts in s1
    # (each unique) + 1 in s2 = 4.
    assert g["intents"] == 4


def test_session_stats_top_truncates_groups_not_totals(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three distinct projects/dates => three date buckets.
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-d1",
        turns=[("a", "Edit", "/r/a.py")], base_ts="2026-06-10T10:0",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-d2",
        turns=[("b", "Edit", "/r/b.py")], proj="proj-d2",
        base_ts="2026-06-11T10:0",
    )
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-d3",
        turns=[("c", "Edit", "/r/c.py")], proj="proj-d3",
        base_ts="2026-06-12T10:0",
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="date", top=1, agent="claude")
    assert len(result["groups"]) == 1
    # totals reflect the full match set, not the truncated list
    assert result["totals"]["sessions"] == 3


def test_session_stats_kind_split(
    tmp_sessions_dir: Path,
    fake_claude_session: Path,
    fake_claude_subagent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fake_claude_session (top-level agent) + fake_claude_subagent (subagent)
    # both live under .claude/projects; point the parser at that tree.
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="kind", agent="claude")

    assert result["group_by"] == "kind"
    assert result["kind_split_available"] is True
    assert "note" not in result

    by_group = {g["group"]: g for g in result["groups"]}
    assert set(by_group) == {"agent", "subagent"}
    assert by_group["agent"]["sessions"] == 1
    assert by_group["subagent"]["sessions"] == 1
    assert result["totals"]["sessions"] == 2


def test_session_stats_kind_split_degenerate_flag(
    tmp_sessions_dir: Path,
    fake_claude_session: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only a top-level session, no subagents: split must flag itself as
    # degenerate so an auditor does not read it as "verified no subagents".
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="kind", agent="claude")

    assert result["kind_split_available"] is False
    assert "note" in result
    assert "subagent" in result["note"].lower()
    by_group = {g["group"]: g for g in result["groups"]}
    assert set(by_group) == {"agent"}


def test_session_stats_agent_filter(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir, "ss-af", turns=[("edit x", "Edit", "/r/x.py")],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="agent", agent="claude")
    assert result["totals"]["agents_list"] == ["claude"]
    assert result["totals"]["sessions"] == 1


def test_session_stats_dir_buckets_by_normalized_project_dir(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record-level ``cwd`` normalizes the ``dir`` bucket away from the slug.

    The Claude parser lifts the transcript ``cwd`` into
    ``Session.project_dir``; the rollup must bucket by that normalized path,
    not by the storage slug, so the same directory never splits in two.
    """
    record = {
        "type": "user",
        "message": {"role": "user", "content": "edit it"},
        "timestamp": "2026-06-14T10:00:00Z",
        "sessionId": "ss-pdir",
        "cwd": "/tmp/one-project",
    }
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-slug"
        / "ss-pdir.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    result = session_stats(group_by="dir", agent="claude")
    groups = [g["group"] for g in result["groups"]]
    assert "/tmp/one-project" in groups
    assert "proj-slug" not in groups


# ---------------------------------------------------------------------------
# with_tokens scan guard (regression: unscoped with_tokens must NOT hang)
# ---------------------------------------------------------------------------
#
# Root cause reproduced by the *shape* of the call, not by real data: an
# unscoped ``with_tokens=True`` reads every matched session's files, so a
# large corpus is a per-session I/O storm the caller sees as a hang.  These
# tests build many tiny fixture sessions and assert the guard fires on the
# cheap inventory count BEFORE any token read — proven by spying on the
# per-session token reader (``_session_tokens``): a refusal must return
# without ever calling it, so a broken guard fails instantly here instead of
# hanging in CI.


def _run_with_timeout(fn, *, seconds: float = 30.0):  # type: ignore[no-untyped-def]
    """Run ``fn()`` in a worker thread, failing the test if it does not return.

    Dependency-free stand-in for ``pytest-timeout`` (not installed here): the
    whole point of the guard is that an unscoped ``with_tokens`` scan must not
    hang, so a run that overshoots the deadline is a hard failure, never a
    silently-passing slow test.  Returns ``fn``'s result on success.
    """
    import threading

    box: dict[str, Any] = {}

    def _target() -> None:
        box["result"] = fn()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():  # pragma: no cover - only on a regression (hang)
        pytest.fail(
            f"session_stats did not return within {seconds}s — the "
            f"with_tokens scan guard regressed into a hang"
        )
    if "result" not in box:  # the target raised
        raise AssertionError("session_stats raised in the worker thread")
    return box["result"]


def _write_many_claude_sessions(
    tmp_sessions_dir: Path, count: int, *, base_ts: str = "2026-06-14T10:00:00Z"
) -> None:
    """Write ``count`` minimal one-turn Claude sessions under one project.

    Deliberately tiny (one user + one assistant record each): the scan guard
    keys on the *number* of matched sessions, so the corpus only needs to be
    numerous, not large per file.
    """
    proj_dir = tmp_sessions_dir / ".claude" / "projects" / "proj-many"
    proj_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        uuid = f"many-{i:05d}"
        records = [
            {
                "type": "user",
                "message": {"role": "user", "content": f"q{i}"},
                "timestamp": base_ts,
                "sessionId": uuid,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"a{i}"}],
                },
                "timestamp": base_ts,
                "sessionId": uuid,
            },
        ]
        (proj_dir / f"{uuid}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )


def _spy_session_tokens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``_session_tokens`` with a call-recording spy.

    Returns a list that receives one session uuid per call, so a test can
    assert the token reader was (or was NOT) reached.  The spy returns a
    trivial honest-``unknown`` block so a permitted scan still produces a
    well-formed result.
    """
    calls: list[str] = []

    def _spy(session, **_kw):  # type: ignore[no-untyped-def]
        calls.append(getattr(session, "uuid", "?"))
        return {
            "input": None,
            "output": None,
            "reasoning": None,
            "cache_read": None,
            "cache_write": None,
            "total": None,
            "source": None,
        }

    monkeypatch.setattr("ai_r.session_stats._session_tokens", _spy)
    return calls


def test_token_scan_limit_validation() -> None:
    with pytest.raises(ValueError, match="token_scan_limit"):
        session_stats(token_scan_limit=-1)
    with pytest.raises(ValueError, match="token_scan_limit"):
        session_stats(token_scan_limit=True)  # type: ignore[arg-type]


def test_with_tokens_unscoped_over_limit_refuses_without_scanning(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A corpus past a small limit; unscoped with_tokens must refuse.  The
    # 6 fixture sessions alone already exceed limit=5, so the refusal fires
    # deterministically whether or not this host also carries real sessions
    # for the OTHER (unpatched) agents — an unscoped scan sees them all,
    # which is precisely the runaway this guard exists to stop.
    limit = 5
    _write_many_claude_sessions(tmp_sessions_dir, limit + 1)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    calls = _spy_session_tokens(monkeypatch)

    # Wall-clock guarded: a regressed guard that fell through to a real scan
    # would be caught here as a hang (fail), not a silently-slow pass.
    result = _run_with_timeout(
        lambda: session_stats(with_tokens=True, token_scan_limit=limit)
    )

    # Structured, self-explaining refusal — NOT a hang, NOT a bare empty.
    assert result["error"] == "scope_required"
    matched = result["matched_sessions"]
    assert isinstance(matched, int) and matched > limit
    assert result["token_scan_limit"] == limit
    assert result["scoped"] is False
    # The message explains itself: it names the actual count and the limit.
    assert str(matched) in result["message"]
    assert str(limit) in result["message"]
    # The guard fired on the inventory count: no session file was read.
    assert calls == []


def test_with_tokens_scoped_by_agent_scans_over_limit(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same over-limit corpus, but a narrowing agent filter → the scan runs.
    limit = 5
    _write_many_claude_sessions(tmp_sessions_dir, limit + 1)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    calls = _spy_session_tokens(monkeypatch)

    result = _run_with_timeout(
        lambda: session_stats(
            with_tokens=True, agent="claude", token_scan_limit=limit
        )
    )

    assert "error" not in result
    assert result["totals"]["sessions"] == limit + 1
    # Scoped run actually read every matched session's tokens.
    assert len(calls) == limit + 1
    assert "tokens" in result["totals"]


def test_with_tokens_scoped_by_since_no_refusal(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ``since`` bound alone counts as scope: the unscoped-refusal must NOT
    # fire even over the limit.  (Left agent-unfiltered on purpose so this
    # exercises the ``since``-only branch of the scope test; the exact session
    # count is therefore host-dependent and not asserted — only the invariant
    # "since ⇒ no scope_required refusal" is, which holds on any host.)
    limit = 5
    _write_many_claude_sessions(tmp_sessions_dir, limit + 1)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    _spy_session_tokens(monkeypatch)

    result = _run_with_timeout(
        lambda: session_stats(
            with_tokens=True, since="2026-01-01", token_scan_limit=limit
        )
    )

    assert result.get("error") != "scope_required"
    assert "tokens" in result["totals"]


def test_with_tokens_unscoped_under_limit_scans(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Below the limit, an unscoped with_tokens run proceeds normally.
    _write_many_claude_sessions(tmp_sessions_dir, 3)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    _silence_non_claude_parsers(monkeypatch)
    calls = _spy_session_tokens(monkeypatch)

    result = session_stats(with_tokens=True, token_scan_limit=10)

    assert "error" not in result
    assert result["totals"]["sessions"] == 3
    assert len(calls) == 3


def test_with_tokens_limit_zero_disables_cap(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # token_scan_limit=0 is the explicit opt-in to scan the whole corpus.
    _write_many_claude_sessions(tmp_sessions_dir, 6)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    _silence_non_claude_parsers(monkeypatch)
    calls = _spy_session_tokens(monkeypatch)

    result = _run_with_timeout(
        lambda: session_stats(with_tokens=True, token_scan_limit=0)
    )

    assert "error" not in result
    assert result["totals"]["sessions"] == 6
    assert len(calls) == 6


def test_with_tokens_warns_on_large_permitted_scan(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scoped (agent) so the run is permitted, but big enough to trip the
    # soft warning: the result must carry an explanatory ``warning``.
    count = TOKEN_SCAN_WARN + 2
    _write_many_claude_sessions(tmp_sessions_dir, count)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    _spy_session_tokens(monkeypatch)

    result = session_stats(
        with_tokens=True, agent="claude", token_scan_limit=0
    )

    assert "error" not in result
    assert "warning" in result
    assert str(count) in result["warning"]


def test_with_tokens_no_warning_below_threshold(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_many_claude_sessions(tmp_sessions_dir, 3)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    _spy_session_tokens(monkeypatch)

    result = session_stats(with_tokens=True, agent="claude")

    assert "warning" not in result


def test_without_tokens_ignores_scan_guard(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard is a with_tokens concern only: a plain unscoped rollup over a
    # large corpus is cheap (no per-session read) and must never be refused.
    _write_many_claude_sessions(tmp_sessions_dir, TOKEN_SCAN_LIMIT + 5)
    _patch_claude(monkeypatch, tmp_sessions_dir)
    _silence_non_claude_parsers(monkeypatch)
    calls = _spy_session_tokens(monkeypatch)

    result = session_stats(group_by="agent")

    assert "error" not in result
    assert "warning" not in result
    assert result["totals"]["sessions"] == TOKEN_SCAN_LIMIT + 5
    # No token reads at all when with_tokens is False.
    assert calls == []


# ---------------------------------------------------------------------------
# CLI: ``ai-r stats`` (thin wrapper over the same core; layout mirrors the
# ``file-frequency`` CLI tests)
# ---------------------------------------------------------------------------


def _run_inproc(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run ``cli.main`` in-process; return ``(rc, stdout, stderr)``."""
    import contextlib
    import io
    import os

    from ai_r import cli as cli_module

    saved_env = {k: os.environ.get(k) for k in ("AI_R_HOME", "OPENCODE_DB")}
    try:
        for k in ("AI_R_HOME", "OPENCODE_DB"):
            os.environ.pop(k, None)
        if env:
            os.environ.update(env)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = cli_module.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, stdout.getvalue(), stderr.getvalue()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_cli_stats_group_by_choices_match_core() -> None:
    """The CLI ``--group-by`` choices stay in step with the core set."""
    from ai_r.cli.commands.stats_cmd import _GROUP_BY_CHOICES

    assert set(_GROUP_BY_CHOICES) == GROUP_BY


def test_cli_stats_human(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir, "st-cli-h",
        turns=[("edit one", "Edit", "/repo/one.py")],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    rc, out, err = _run_inproc(
        ["stats", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    assert "scanned:" in out
    assert "group by: agent" in out
    assert "claude" in out


def test_cli_stats_json_group_by_dir(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir, "st-cli-j",
        turns=[("edit it", "Edit", "/repo/file.py")],
        proj="proj-st",
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    rc, out, err = _run_inproc(
        ["stats", "--agent", "claude", "--group-by", "dir", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["group_by"] == "dir"
    assert payload["totals"]["sessions"] == 1
    assert payload["groups"][0]["group"] == "proj-st"


def test_cli_stats_with_tokens_json(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_edit_session(
        tmp_sessions_dir, "st-cli-t",
        turns=[("edit it", "Edit", "/repo/tok.py")],
    )
    _patch_claude(monkeypatch, tmp_sessions_dir)
    rc, out, err = _run_inproc(
        ["stats", "--agent", "claude", "--with-tokens", "--json"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert "tokens" in payload["groups"][0]
    assert "tokens" in payload["totals"]


def test_cli_stats_no_sessions_stderr(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_claude(monkeypatch, tmp_sessions_dir)
    rc, out, err = _run_inproc(
        ["stats", "--agent", "claude"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 0
    assert "no sessions" in err.lower()


def test_cli_stats_bad_iso_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, err = _run_inproc(
        ["stats", "--since", "not-a-date"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "iso" in err.lower()


def test_cli_stats_scan_guard_refusal_exits_2(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core's structured refusal surfaces via the CLI error contract."""
    monkeypatch.setattr(
        "ai_r.session_stats.session_stats",
        lambda **kwargs: {
            "error": "scope_required",
            "message": "narrow the scope (agent / since / until)",
        },
    )
    rc, out, err = _run_inproc(
        ["stats", "--with-tokens"],
        env={"AI_R_HOME": str(tmp_sessions_dir)},
    )
    assert rc == 2
    assert "narrow the scope" in err
