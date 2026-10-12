"""Hermetic tests for the single-scan ``audit_brief`` (F2, RCA RC-2).

The preset used to walk the session FIVE times (resolve → query scan →
plan → plan_feedback → read_messages for tokens ≈ 29.7 s CPU on a 2.5 MB
transcript).  Now ONE ``query`` materialization feeds every projection:
the plan projections receive the already-materialized ``plan_event`` rows
via the internal ``_events`` seam, and the token breakdown reuses the
core read cache filled by that same pass.

Two contracts:

1. **Single scan / single parse** — counting seams around the real Claude
   parser observe exactly ONE ``list_sessions`` corpus scan and ONE
   ``read_messages`` full parse per cold ``audit_brief`` call.
2. **Semantic equivalence** — the digest's sections equal what the BASE
   verbs (``query`` / ``plan`` / ``plan_feedback``) return on their own,
   and the injected-``_events`` plan projections equal their un-injected
   twins byte-for-byte.  (The full 309-line ``test_audit_brief.py`` suite
   additionally pins the digest's exact values — it must stay green
   unchanged.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.audit_brief import audit_brief
from ai_r.events.plan import plan, plan_feedback
from ai_r.events.query import query


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _exit_plan_rec(plan_text: str, tool_use_id: str, ts: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "ExitPlanMode",
                "input": {"plan": plan_text},
            }],
        },
        "timestamp": ts,
    }


def _tool_result_rec(
    tool_use_id: str, content: str, ts: str, *, is_error: bool = False
) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                **({"is_error": True} if is_error else {}),
            }],
        },
        "timestamp": ts,
    }


REJECT_BOILER = (
    "The user doesn't want to proceed with this tool use. "
    "The tool use was rejected. To tell you how to proceed, the "
    "user said:\n"
)


@pytest.fixture
def plan_session(tmp_sessions_dir: Path) -> str:
    """A Claude session exercising every audit_brief section at once.

    Two plan revisions of one task (a rejected first, an approved final —
    so ``plan_feedback`` has a real pair), an errored edit (tool-error
    footprint), and verbatim user turns.
    """
    session_id = "brief-single-scan-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-brief"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "Plan the fix."},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            _exit_plan_rec(
                "# Fix Plan\n\nFIRST revision.", "tu-a", "2026-06-14T10:05:00Z"
            ),
            _tool_result_rec(
                "tu-a",
                REJECT_BOILER + "Rework the first revision.",
                "2026-06-14T10:05:01Z",
            ),
            _exit_plan_rec(
                "# Fix Plan\n\nSECOND revision.", "tu-b", "2026-06-14T10:06:00Z"
            ),
            _tool_result_rec(
                "tu-b", "approved", "2026-06-14T10:06:01Z"
            ),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Applying the fix."},
                        {
                            "type": "tool_use",
                            "id": "tu-edit-1",
                            "name": "Edit",
                            "input": {
                                "file_path": "/repo/src/auth.py",
                                "old_string": "a",
                                "new_string": "b",
                            },
                        },
                    ],
                },
                "timestamp": "2026-06-14T10:07:00Z",
            },
            _tool_result_rec(
                "tu-edit-1",
                "old_string not found",
                "2026-06-14T10:07:01Z",
                is_error=True,
            ),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Now run the tests and report back.",
                },
                "timestamp": "2026-06-14T10:08:00Z",
            },
        ],
    )
    return session_id


def test_cold_audit_brief_scans_and_parses_once(
    plan_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: the whole digest costs ONE corpus scan + ONE message parse."""
    from ai_r.parsers import claude

    calls = {"scan": 0, "parse": 0}
    real_ls = claude.list_sessions
    real_rm = claude.read_messages

    def counting_ls(*args, **kwargs):
        calls["scan"] += 1
        return real_ls(*args, **kwargs)

    def counting_rm(*args, **kwargs):
        calls["parse"] += 1
        return real_rm(*args, **kwargs)

    monkeypatch.setattr(claude, "list_sessions", counting_ls)
    monkeypatch.setattr(claude, "read_messages", counting_rm)

    brief = audit_brief(plan_session, agent="claude")

    assert calls["scan"] == 1, "audit_brief must not rescan the corpus"
    assert calls["parse"] == 1, "audit_brief must parse the transcript once"
    # Sanity: the digest actually saw the session's content.
    assert brief["user_turns_count"] == 2
    assert brief["plans"]["count"] >= 1
    assert brief["tools"]["errors_count"] == 1


def test_injected_plan_events_match_uninjected(plan_session: str) -> None:
    """The ``_events`` seam is byte-equivalent to the internal query."""
    rows = query(
        session=[plan_session], agent="claude", limit=0, redact=False
    )
    plan_rows = [ev for ev in rows if ev.get("type") == "plan_event"]
    assert plan_rows, "fixture must produce plan_events"

    ref_plan = plan(plan_session, agent="claude", bodies="final")
    inj_plan = plan(
        plan_session, agent="claude", bodies="final", _events=plan_rows
    )
    assert inj_plan == ref_plan

    ref_fb = plan_feedback(plan_session, agent="claude")
    inj_fb = plan_feedback(plan_session, agent="claude", _events=plan_rows)
    assert inj_fb == ref_fb
    assert ref_fb, "fixture must produce a feedback pair"


def test_audit_brief_sections_match_base_verbs(plan_session: str) -> None:
    """The digest's projections equal the BASE verbs' own output."""
    brief = audit_brief(plan_session, agent="claude", redact=False)

    rows = query(
        session=[plan_session], agent="claude", limit=0, redact=False
    )
    user_rows = [ev for ev in rows if ev.get("type") == "user_turn"]
    assert [t["id"] for t in brief["user_turns"]] == [
        ev["id"] for ev in user_rows
    ]

    ref_plan = plan(plan_session, agent="claude", bodies="final")
    assert [t["id"] for t in brief["plans"]["tasks"]] == [
        p["id"] for p in ref_plan
    ]
    assert [t.get("title") for t in brief["plans"]["tasks"]] == [
        p.get("title") for p in ref_plan
    ]

    ref_fb = plan_feedback(plan_session, agent="claude")
    assert brief["plans"]["feedback_count"] == len(ref_fb)
    assert brief["plans"]["feedback"] or ref_fb == []


def test_audit_brief_deterministic_cold_and_warm(plan_session: str) -> None:
    """Warm repeats (caches full) are byte-identical to the cold pass."""
    first = audit_brief(plan_session, agent="claude", redact=False)
    second = audit_brief(plan_session, agent="claude", redact=False)
    assert first == second
