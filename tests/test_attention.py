"""The ``attention`` preset (F4.4) — hermetic tests.

Unit layer: the pacing arithmetic (:func:`classify_pacing`) and the decision
dictionary (:func:`match_decision`).
Core layer: :func:`ai_r.attention.attention` over synthetic Claude sessions
written under the per-test ``AI_R_HOME`` (gate detection, message-level
answer correlation, the reading-speed verdict, filters, caps, redaction,
diagnostics).
MCP layer: registration + the thin-wrapper error contract.

Everything here is hermetic; no host data is read.  All session content is
DATA under a temp dir, never executed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r import mcp_server
from ai_r.attention import (
    AVG_CPS,
    GATE_KINDS,
    SEVERITY_MODES,
    attention,
    classify_pacing,
    match_decision,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _user_text(text: str, ts: str, uuid: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "sessionId": uuid,
    }


def _assistant(content: list, ts: str, uuid: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": content},
        "timestamp": ts,
        "sessionId": uuid,
    }


def _user_result(
    tool_use_id: str, content: str, ts: str, uuid: str, is_error: bool = False
) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }],
        },
        "timestamp": ts,
        "sessionId": uuid,
    }


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _exit_plan(call_id: str, plan: str | None = None) -> dict:
    inp: dict = {} if plan is None else {"plan": plan}
    return {"type": "tool_use", "id": call_id, "name": "ExitPlanMode", "input": inp}


def _ask(call_id: str, questions: list) -> dict:
    return {
        "type": "tool_use",
        "id": call_id,
        "name": "AskUserQuestion",
        "input": {"questions": questions},
    }


def _write_plan(call_id: str, path: str, content: str) -> dict:
    return {
        "type": "tool_use",
        "id": call_id,
        "name": "Write",
        "input": {"file_path": path, "content": content},
    }


def _q(question: str, header: str = "h", options: list | None = None) -> dict:
    return {
        "question": question,
        "header": header,
        "options": options if options is not None else [
            {"label": "x", "description": "y"}
        ],
    }


def _write_claude(tmp_sessions_dir: Path, uuid: str, records: list) -> Path:
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-att" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return jsonl


PLAN_BODY = "P" * 2000        # ~2000-char reviewed plan
Q_BIG = "Q" * 800            # a long question body


@pytest.fixture
def pacing_session(tmp_sessions_dir: Path) -> str:
    """One Claude session mirroring reality: a RUSHED plan + a READ question.

    * a plan written to ``plans/t.md`` (2000 chars), ``ExitPlanMode`` at
      10:00:05, user approval at 10:00:08 → gap 3s → **red** plan gate.
    * an ``AskUserQuestion`` (long body) at 10:01:00 answered at 10:01:40 →
      gap 40s → clean (ratio < 2), no signal under the default filter.
    """
    uuid = "sess-pacing-1"
    records = [
        _user_text("go", "2026-07-08T10:00:00Z", uuid),
        _assistant(
            [_text("writing plan"), _write_plan("w1", "/p/plans/t.md", PLAN_BODY)],
            "2026-07-08T10:00:02Z", uuid,
        ),
        _assistant([_text("review?"), _exit_plan("e1")], "2026-07-08T10:00:05Z", uuid),
        _user_result(
            "e1", "User has approved your plan. Start coding.",
            "2026-07-08T10:00:08Z", uuid,
        ),
        _assistant(
            [_text("ask"), _ask("a1", [_q(Q_BIG)])],
            "2026-07-08T10:01:00Z", uuid,
        ),
        _user_result(
            "a1", "Your questions have been answered: x",
            "2026-07-08T10:01:40Z", uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    return uuid


# ---------------------------------------------------------------------------
# Unit: pacing arithmetic + decision dictionary
# ---------------------------------------------------------------------------


def test_classify_pacing_red_amber_none() -> None:
    # Fast read of a big plan → red (ratio ≫ 4).
    sev, req, ratio = classify_pacing(3.0, 2000)
    assert sev == "red"
    assert req == pytest.approx(2000 / 3.0)
    assert ratio == pytest.approx((2000 / 3.0) / AVG_CPS)
    # Skim-range → amber (2 ≤ ratio < 4): 60 c/s = 2.4× the 25 c/s average.
    assert classify_pacing(20.0, 1200)[0] == "amber"
    # Unhurried → no signal.
    assert classify_pacing(300.0, 2000)[0] is None


def test_classify_pacing_floor_and_unmeasured_content() -> None:
    # Near-instant answer to unmeasured content → floor red, no ratio.
    sev, req, ratio = classify_pacing(1.0, None)
    assert sev == "red"
    assert req is None and ratio is None
    # Unmeasured content but a real pause → cannot rate → no signal.
    assert classify_pacing(10.0, None) == (None, None, None)
    # A non-positive gap yields no ratio (caller drops negatives separately).
    assert classify_pacing(0.0, 2000)[0] == "red"  # 0 < floor → instant


def test_match_decision() -> None:
    assert match_decision("User has approved your plan.") == "approved"
    assert match_decision("Keep planning — revise section 2") == "rejected"
    assert match_decision("Your questions have been answered: x") == "answered"
    assert match_decision("одобрил, поехали") == "approved"
    assert match_decision("нет, доработай") == "rejected"
    assert match_decision("") == "other"


def test_vocab_shapes() -> None:
    assert GATE_KINDS == {"plan", "question"}
    assert SEVERITY_MODES == {"flagged", "red", "all"}


# ---------------------------------------------------------------------------
# Core: gate detection + verdict on a synthetic session
# ---------------------------------------------------------------------------


def test_flagged_default_returns_rushed_plan_only(pacing_session: str) -> None:
    out = attention(agent="claude")
    assert out["count"] == 1          # only the rushed plan; the question is clean
    assert out["red_count"] == 1
    assert out["amber_count"] == 0
    assert out["by_gate"] == {"plan": 1}
    assert out["truncated"] is False
    assert "diagnostics" not in out
    assert out["params"]["avg_cps"] == AVG_CPS

    rec = out["gates"][0]
    assert rec["gate"] == "plan"
    assert rec["tool"] == "ExitPlanMode"
    assert rec["agent"] == "claude"
    assert rec["session_id"] == pacing_session
    assert rec["id"].startswith(pacing_session + ":")
    assert rec["content_chars"] == len(PLAN_BODY)
    assert rec["gap_sec"] == pytest.approx(3.0)
    assert rec["ratio"] > 4.0
    assert rec["severity"] == "red"
    # Decision classified on the LEADING verdict line, not the echoed plan.
    assert rec["reaction"]["kind"] == "approved"
    assert "approved" in rec["reaction"]["preview"]


def test_severity_all_includes_clean_question(pacing_session: str) -> None:
    out = attention(agent="claude", severity="all")
    assert out["count"] == 2
    by_gate = {r["gate"]: r for r in out["gates"]}
    q = by_gate["question"]
    assert q["tool"] == "AskUserQuestion"
    assert q["severity"] is None          # answered after a real pause
    assert q["ratio"] < 2.0
    assert q["reaction"]["kind"] == "answered"
    assert q["content_chars"] >= len(Q_BIG)   # question + option strings


def test_gate_filter(pacing_session: str) -> None:
    plan_only = attention(agent="claude", gate="plan")
    assert plan_only["count"] == 1 and plan_only["gates"][0]["gate"] == "plan"
    # The question is clean → absent from the default 'flagged' set.
    q_flagged = attention(agent="claude", gate="question")
    assert q_flagged["count"] == 0
    # …but present under severity='all'.
    q_all = attention(agent="claude", gate="question", severity="all")
    assert q_all["count"] == 1


def test_severity_red_filter(pacing_session: str) -> None:
    red = attention(agent="claude", severity="red")
    assert red["count"] == 1
    assert all(r["severity"] == "red" for r in red["gates"])


def test_limit_and_truncated(pacing_session: str) -> None:
    out = attention(agent="claude", severity="all", limit=1)
    assert out["count"] == 2           # totals reflect the full matched set
    assert len(out["gates"]) == 1
    assert out["truncated"] is True


def test_session_scope_single_and_list(pacing_session: str) -> None:
    scoped = attention(session=pacing_session)
    assert scoped["count"] == 1
    listed = attention(session=[pacing_session, "no-such-uuid"])
    assert listed["count"] == 1       # unknown uuid contributes nothing


# ---------------------------------------------------------------------------
# Core: edge cases (each a tiny dedicated session)
# ---------------------------------------------------------------------------


def test_slow_plan_not_flagged(tmp_sessions_dir: Path) -> None:
    uuid = "sess-slow-plan"
    records = [
        _assistant([_write_plan("w1", "/p/plans/s.md", PLAN_BODY)], "2026-07-08T10:00:00Z", uuid),
        _assistant([_exit_plan("e1")], "2026-07-08T10:00:02Z", uuid),
        _user_result("e1", "User has approved your plan.", "2026-07-08T10:05:02Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    assert attention(agent="claude")["count"] == 0            # 300s → clean
    allg = attention(agent="claude", severity="all")
    assert allg["count"] == 1 and allg["gates"][0]["severity"] is None


def test_floor_flags_instant_unmeasured_plan(tmp_sessions_dir: Path) -> None:
    """No plan-file write + empty ExitPlanMode input → content unmeasured.

    An instant (<floor) approval still fires on the floor; a paused one does
    not (nothing to rate).
    """
    uuid = "sess-floor"
    records = [
        _assistant([_exit_plan("e1")], "2026-07-08T10:00:05Z", uuid),
        _user_result("e1", "User has approved your plan.", "2026-07-08T10:00:06Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = attention(agent="claude")
    assert out["count"] == 1
    rec = out["gates"][0]
    assert rec["content_chars"] is None       # unmeasured
    assert rec["ratio"] is None               # nothing to rate
    assert rec["severity"] == "red"           # floor fired (gap 1s < 2s)

    uuid2 = "sess-floor-paused"
    records2 = [
        _assistant([_exit_plan("e2")], "2026-07-08T10:00:05Z", uuid2),
        _user_result("e2", "User has approved your plan.", "2026-07-08T10:00:20Z", uuid2),
    ]
    _write_claude(tmp_sessions_dir, uuid2, records2)
    assert attention(session=uuid2)["count"] == 0             # 15s, unmeasured → none


def test_trivial_short_question_skipped(tmp_sessions_dir: Path) -> None:
    uuid = "sess-trivial-q"
    records = [
        _assistant([_ask("a1", [_q("ok?", header="", options=[])])], "2026-07-08T10:00:00Z", uuid),
        _user_result("a1", "answered: yes", "2026-07-08T10:00:01Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    # Content below MIN_CHARS → never flagged, even under severity='all'.
    assert attention(agent="claude", severity="all")["count"] == 0


def test_rushed_question_red(tmp_sessions_dir: Path) -> None:
    uuid = "sess-rushed-q"
    records = [
        _assistant([_ask("a1", [_q(Q_BIG)])], "2026-07-08T10:00:00Z", uuid),
        _user_result("a1", "answered: x", "2026-07-08T10:00:02Z", uuid),  # gap 2s
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = attention(agent="claude")
    assert out["count"] == 1
    assert out["gates"][0]["gate"] == "question"
    assert out["gates"][0]["severity"] == "red"


def test_reject_decision_label(tmp_sessions_dir: Path) -> None:
    uuid = "sess-reject"
    records = [
        _assistant([_write_plan("w1", "/p/plans/r.md", PLAN_BODY)], "2026-07-08T10:00:00Z", uuid),
        _assistant([_exit_plan("e1")], "2026-07-08T10:00:02Z", uuid),
        _user_result("e1", "Keep planning — the user wants changes.", "2026-07-08T10:00:04Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    rec = attention(agent="claude")["gates"][0]
    assert rec["reaction"]["kind"] == "rejected"
    assert rec["severity"] == "red"           # still fast, still measured


def test_negative_gap_dropped(tmp_sessions_dir: Path) -> None:
    """An answer timestamped BEFORE the gate (clock skew) yields no signal."""
    uuid = "sess-skew"
    records = [
        _assistant([_write_plan("w1", "/p/plans/k.md", PLAN_BODY)], "2026-07-08T10:00:00Z", uuid),
        _assistant([_exit_plan("e1")], "2026-07-08T10:00:10Z", uuid),
        _user_result("e1", "User has approved your plan.", "2026-07-08T10:00:05Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    assert attention(agent="claude", severity="all")["count"] == 0


def test_no_answer_no_signal(tmp_sessions_dir: Path) -> None:
    """A gate still awaiting its answer (session tail) is never guessed."""
    uuid = "sess-pending"
    records = [
        _assistant([_write_plan("w1", "/p/plans/p.md", PLAN_BODY)], "2026-07-08T10:00:00Z", uuid),
        _assistant([_exit_plan("e1")], "2026-07-08T10:00:02Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    assert attention(agent="claude", severity="all")["count"] == 0


def test_redaction_on_preview(tmp_sessions_dir: Path) -> None:
    uuid = "sess-secret-ans"
    secret_answer = "User has approved. token=sk-abc123def456ghi789jkl0"
    records = [
        _assistant([_write_plan("w1", "/p/plans/x.md", PLAN_BODY)], "2026-07-08T10:00:00Z", uuid),
        _assistant([_exit_plan("e1")], "2026-07-08T10:00:02Z", uuid),
        _user_result("e1", secret_answer, "2026-07-08T10:00:05Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = attention(agent="claude")
    assert "[REDACTED_" in out["gates"][0]["reaction"]["preview"]
    assert out["redactions"]
    raw = attention(agent="claude", redact=False)
    assert "sk-abc123def456ghi789jkl0" in raw["gates"][0]["reaction"]["preview"]
    assert "redactions" not in raw


def test_empty_corpus_diagnostics() -> None:
    out = attention(agent="claude")
    assert out["count"] == 0
    assert out["gates"] == []
    assert out["by_gate"] == {}
    assert "diagnostics" in out


# ---------------------------------------------------------------------------
# Core: validation (fail-loud)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"gate": "edit"}, "gate"),
        ({"severity": "high"}, "severity"),
        ({"limit": -1}, "limit"),
        ({"limit": True}, "limit"),
        ({"redact": "yes"}, "redact"),
        ({"agent": "gemini"}, "agent"),
        ({"session": []}, "session"),
        ({"noise": "drop"}, "noise"),
    ],
)
def test_invalid_arguments_fail_loud(kwargs: dict, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        attention(**kwargs)


# ---------------------------------------------------------------------------
# MCP layer: registration + thin-wrapper contract
# ---------------------------------------------------------------------------


def test_mcp_attention_registered() -> None:
    assert "attention" in mcp_server.mcp._tool_manager._tools


def test_mcp_attention_result_shape(pacing_session: str) -> None:
    out = mcp_server.attention(agent="claude", severity="all")
    assert out["count"] == 2
    assert {r["gate"] for r in out["gates"]} == {"plan", "question"}


def test_mcp_attention_invalid_argument_dict() -> None:
    out = mcp_server.attention(gate="delete")
    assert out["error"] == "invalid_argument"
    assert "gate" in out["message"]
    out2 = mcp_server.attention(severity="urgent")
    assert out2["error"] == "invalid_argument"
