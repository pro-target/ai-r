"""Session outcome classification (F2.3) — hermetic tests.

Unit layer: :func:`ai_r.outcome.session_outcome` over synthetic
:class:`~ai_r.parsers.models.Message` lists (all decision-table rows).
MCP layer: ``read_session`` carries the ``outcome`` block, computed from
the same raw messages the projection consumes.

Everything here is hermetic (synthetic sessions under the per-test
``AI_R_HOME``); no host data is read.
"""
from __future__ import annotations

import json
from pathlib import Path

from ai_r import mcp_server
from ai_r.outcome import ERROR_FLAG_RELIABLE_AGENTS, session_outcome
from ai_r.parsers.models import AgentName, Message


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _user(text: str) -> Message:
    return Message(role="user", text=text)


def _assistant(text: str = "done") -> Message:
    return Message(role="assistant", text=text)


def _tool_result(is_error: bool = False) -> Message:
    """A user-embedded tool result (the Claude shape: role stays user)."""
    return Message(
        role="user",
        text="",
        tool_result=({"content": "output", "is_error": is_error},),
    )


def _results(total: int, errors: int) -> list[Message]:
    return [_tool_result(is_error=(i < errors)) for i in range(total)]


# ---------------------------------------------------------------------------
# Unit: decision table
# ---------------------------------------------------------------------------


def test_unknown_when_no_signal() -> None:
    """No verdict words, no error signal → honest unknown, empty signals."""
    out = session_outcome(
        [_user("сделай рефакторинг"), _assistant()], AgentName.CLAUDE
    )
    assert out["status"] == "unknown"
    assert out["signals"] == []
    assert out["user_verdict"] == "neutral"
    assert out["tool_results"] == 0
    assert out["tool_errors"] == 0
    assert out["error_rate"] is None  # no calls → no rate, not 0.0
    assert out["error_rate_reliable"] is True


def test_unknown_on_empty_messages() -> None:
    out = session_outcome([], AgentName.CLAUDE)
    assert out["status"] == "unknown"
    assert out["signals"] == []


def test_success_from_russian_tail() -> None:
    msgs = [_user("почини парсер"), _assistant(), _user("Супер, работает!")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "success"
    assert out["user_verdict"] == "positive"
    assert "работает" in out["markers"]["positive"]
    assert any("user verdict: positive" in s for s in out["signals"])


def test_failure_from_russian_tail() -> None:
    msgs = [_user("почини парсер"), _assistant(), _user("Не работает, откати")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "failure"
    assert out["user_verdict"] == "negative"
    assert "не работает" in out["markers"]["negative"]
    assert "откати" in out["markers"]["negative"]


def test_failure_from_english_tail() -> None:
    msgs = [_user("fix the parser"), _assistant(), _user("still broken, revert")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "failure"
    assert "undo/revert" in out["markers"]["negative"]


def test_success_from_english_tail() -> None:
    msgs = [_user("fix it"), _assistant(), _user("perfect, thanks, lgtm")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "success"


def test_mixed_positive_words_but_errors_dominant() -> None:
    """Positive user verdict + dominant tool errors → mixed, both signals."""
    msgs = _results(total=6, errors=4) + [_user("Ок, спасибо")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "mixed"
    assert out["error_rate"] == round(4 / 6, 4)
    assert len(out["signals"]) == 2


def test_failure_from_error_rate_alone() -> None:
    """Neutral words + rate >= 0.5 across >= 4 results → failure."""
    msgs = _results(total=4, errors=3) + [_user("посмотрю позже")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "failure"
    assert out["user_verdict"] == "neutral"
    assert out["tool_errors"] == 3
    assert out["error_rate"] == 0.75
    assert any("tool error rate" in s for s in out["signals"])


def test_micro_session_errors_not_dominant() -> None:
    """1 error of 2 results is below the >=4 calibrated floor → unknown."""
    msgs = _results(total=2, errors=1)
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "unknown"
    assert out["error_rate"] == 0.5  # reported, but not a deciding signal
    assert out["signals"] == []


def test_low_error_rate_is_not_a_failure_signal() -> None:
    """Median-ish real-history rate (~0.1) never decides the status."""
    msgs = _results(total=10, errors=1)
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "unknown"
    assert out["error_rate"] == 0.1


def test_unreliable_agent_error_fields_are_none() -> None:
    """Codex has no per-result error flag → None fields, words still decide."""
    assert AgentName.CODEX not in ERROR_FLAG_RELIABLE_AGENTS
    msgs = [
        Message(
            role="tool",
            text="",
            # Even a (hypothetical) truthy flag must not be trusted for an
            # unreliable agent — the source format carries no such signal.
            tool_result=({"content": "x", "is_error": True},),
        )
        for _ in range(5)
    ] + [_user("Отлично, спасибо")]
    out = session_outcome(msgs, AgentName.CODEX)
    assert out["status"] == "success"
    assert out["tool_results"] == 5
    assert out["tool_errors"] is None
    assert out["error_rate"] is None
    assert out["error_rate_reliable"] is False


def test_unreliable_agent_no_words_is_unknown() -> None:
    msgs = _results(total=8, errors=8) + [_user("продолжай дальше")]
    out = session_outcome(msgs, AgentName.PI)
    assert out["status"] == "unknown"
    assert out["error_rate"] is None


def test_verdict_scans_only_the_tail() -> None:
    """An early complaint is history once the closing turns are positive."""
    msgs = [
        _user("не работает, переделай"),  # early trouble (outside the tail)
        _assistant(),
        _user("уже лучше"),
        _assistant(),
        _user("почти"),
        _assistant(),
        _user("Готово, всё чисто, спасибо"),
    ]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "success"
    assert out["markers"]["negative"] == []


def test_non_human_user_turns_are_skipped() -> None:
    """XML wrappers / harness placeholders / caveats never carry a verdict."""
    msgs = [
        _user("Супер, работает"),
        _user("<system-reminder>broken failed wrong</system-reminder>"),
        _user("[Request interrupted by user]"),
        _user("Caveat: the messages below were generated. broken wrong"),
    ]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "success"
    assert out["markers"]["negative"] == []


def test_tie_of_markers_is_neutral() -> None:
    """Equal positive and negative evidence must abstain, not guess."""
    msgs = [_user("работает, но код говно")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["user_verdict"] == "neutral"
    assert out["status"] == "unknown"


def test_negative_verdict_beats_dominant_errors_consistently() -> None:
    """negative + dominant errors is still failure (no double counting)."""
    msgs = _results(total=6, errors=6) + [_user("всё сломалось, откати")]
    out = session_outcome(msgs, AgentName.CLAUDE)
    assert out["status"] == "failure"
    assert len(out["signals"]) == 2  # both reasons spelled out


def test_word_boundaries_do_not_fire_inside_words() -> None:
    """«неверно» must not fire «верно»; 'ок' must not fire inside words."""
    out = session_outcome([_user("поток данных широкий")], AgentName.CLAUDE)
    assert out["markers"]["positive"] == []
    assert out["status"] == "unknown"


def test_outcome_contains_no_raw_session_text() -> None:
    """The block carries only dictionary labels — never transcript text."""
    secret_ish = "PASSWORD=hunter2x9extra и вообще всё работает"
    out = session_outcome([_user(secret_ish)], AgentName.CLAUDE)
    dumped = json.dumps(out, ensure_ascii=False)
    assert "hunter2x9extra" not in dumped
    assert out["status"] == "success"


# ---------------------------------------------------------------------------
# MCP layer: read_session carries the outcome block
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _claude_record(role: str, content, ts: str, sid: str) -> dict:
    return {
        "type": role,
        "message": {"role": role, "content": content},
        "timestamp": ts,
        "sessionId": sid,
    }


def _seed_claude_session_with_errors(home: Path, sid: str) -> None:
    """4 tool results (3 errors) + a neutral closing user turn."""
    tool_use = [
        {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "x"}}
        for i in range(4)
    ]
    records = [
        _claude_record("user", "запусти тесты", "2026-06-14T10:00:00Z", sid),
        _claude_record("assistant", tool_use, "2026-06-14T10:00:01Z", sid),
    ]
    for i in range(4):
        records.append(
            _claude_record(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{i}",
                        "content": "boom" if i < 3 else "ok",
                        "is_error": i < 3,
                    }
                ],
                f"2026-06-14T10:00:0{2 + i}Z",
                sid,
            )
        )
    records.append(
        _claude_record("user", "посмотрю позже", "2026-06-14T10:00:09Z", sid)
    )
    _write_jsonl(
        home / ".claude" / "projects" / "proj-a" / f"{sid}.jsonl", records
    )


def test_read_session_outcome_failure_from_error_rate(
    tmp_sessions_dir: Path,
) -> None:
    sid = "outcome-claude-err"
    _seed_claude_session_with_errors(tmp_sessions_dir, sid)
    out = mcp_server.read_session(uuid=sid, agent="claude")
    assert "error" not in out
    outcome = out["outcome"]
    assert outcome["status"] == "failure"
    assert outcome["tool_results"] == 4
    assert outcome["tool_errors"] == 3
    assert outcome["error_rate"] == 0.75
    assert outcome["error_rate_reliable"] is True


def test_read_session_outcome_success_from_words(
    tmp_sessions_dir: Path,
) -> None:
    sid = "outcome-claude-ok"
    _write_jsonl(
        tmp_sessions_dir / ".claude" / "projects" / "proj-a" / f"{sid}.jsonl",
        [
            _claude_record("user", "почини баг", "2026-06-14T10:00:00Z", sid),
            _claude_record(
                "assistant",
                [{"type": "text", "text": "Починил."}],
                "2026-06-14T10:00:01Z",
                sid,
            ),
            _claude_record(
                "user", "Отлично, работает!", "2026-06-14T10:00:02Z", sid
            ),
        ],
    )
    out = mcp_server.read_session(uuid=sid, agent="claude")
    outcome = out["outcome"]
    assert outcome["status"] == "success"
    assert outcome["user_verdict"] == "positive"


def test_read_session_outcome_unknown_no_signal(
    fake_claude_session: Path,
) -> None:
    """The stock hello-world fixture has no verdict words and no calls."""
    out = mcp_server.read_session(uuid="test-claude-1", agent="claude")
    outcome = out["outcome"]
    assert outcome["status"] == "unknown"
    assert outcome["signals"] == []


def test_read_session_outcome_codex_error_fields_null(
    fake_codex_session: Path,
) -> None:
    """Unreliable-flag agent: error fields are None on the MCP surface too."""
    out = mcp_server.read_session(uuid="test-codex-1", agent="codex")
    outcome = out["outcome"]
    assert outcome["error_rate_reliable"] is False
    assert outcome["tool_errors"] is None
    assert outcome["error_rate"] is None


# ---------------------------------------------------------------------------
# Host calibration guard (skips when the host has no Claude data)
# ---------------------------------------------------------------------------


def test_calibration_sanity_on_real_history(real_claude_dir: Path) -> None:
    """The calibrated classifier stays sane on real history.

    Guards the F2.3 calibration itself (audit 2026-07-04: median tool
    error rate ~0.09, p90 ~0.22): on a real corpus the classifier must
    (a) always emit a valid status, (b) keep the "empty signals <=>
    unknown" honesty invariant, and (c) not degenerate into blanket
    failure — with rate>=0.5 across >=4 results calibrated to sit far
    above the p90, a majority-failure verdict would mean the thresholds
    or the dictionary regressed.  Auto-tagged ``host`` (skips, never
    fails, on a bare machine).
    """
    import pytest as _pytest

    from ai_r.parsers import claude as claude_parser

    base = str(real_claude_dir)
    sessions = [
        s
        for s in claude_parser.list_sessions(base_dir=base)
        if s.kind != "subagent"
    ][:40]
    if len(sessions) < 5:
        _pytest.skip("not enough real Claude sessions for a sanity check")
    statuses: list[str] = []
    for s in sessions:
        try:
            msgs = claude_parser.read_messages(s.uuid, base_dir=base)
        except (OSError, ValueError):
            continue
        out = session_outcome(msgs, s.agent)
        assert out["status"] in {"success", "failure", "mixed", "unknown"}
        assert (out["status"] == "unknown") == (out["signals"] == [])
        statuses.append(out["status"])
    failures = statuses.count("failure")
    assert failures <= len(statuses) * 0.5, (
        f"calibration regressed: {failures}/{len(statuses)} real sessions "
        "classify as failure"
    )
