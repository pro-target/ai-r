"""Tests for the Phase-2 plan surface (``ai_r.events``).

Covers plan_event emission per-agent (internal normalization), the Plan atom
+ task grouping (draft/final/completed_major), the ``plan`` preset and
``get_body`` (incl. ``shallow`` for the S6 subagent-gets-one-plan scenario).

Hermetic by default via the autouse ``_isolate_ai_r_home`` fixture; the two
host-marked tests request real ``real_claude_dir`` data and *skip* (never
fail) when absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_r.events import get_body, iter_events, plan, query


# ---------------------------------------------------------------------------
# plan_event emission (per-agent, internal signal table)
# ---------------------------------------------------------------------------


def test_claude_plan_signals_emit_plan_events(
    fake_claude_plan_redraft: str,
) -> None:
    events = list(iter_events("claude", session=fake_claude_plan_redraft))
    plan_events = [e for e in events if e.type == "plan_event"]
    # One iteration chain: 2 Write + 2 ExitPlanMode = 4 plan_events.
    assert len(plan_events) == 4
    signals = [
        {k: v for r in e.refs for k, v in r.items()}["agent_signal"]
        for e in plan_events
    ]
    assert signals == [
        "claude:Write(plans/*.md)",
        "claude:ExitPlanMode",
        "claude:ExitPlanMode",
        "claude:Write(plans/*.md)",
    ]
    # Body/steps are NOT inlined; refs carry title + agent_signal + task_key.
    refs0 = {k: v for r in plan_events[0].refs for k, v in r.items()}
    assert refs0["title"] == "Build Feature X"
    assert refs0["task_key"] == "plans/build-feature.md"
    assert "body" not in refs0 and "plan" not in refs0


def test_claude_exitplanmode_inherits_preceding_slug(
    fake_claude_plan_redraft: str,
) -> None:
    # The ExitPlanMode signals (path=None) must inherit the slug of the
    # nearest preceding plan-file Write, so all 4 events share one task_key
    # despite the drifting titles.
    events = list(iter_events("claude", session=fake_claude_plan_redraft))
    keys = {
        {k: v for r in e.refs for k, v in r.items()}.get("task_key")
        for e in events
        if e.type == "plan_event"
    }
    assert keys == {"plans/build-feature.md"}


def test_claude_write_plans_md_emits_plan_event(
    fake_claude_plan_write: str,
) -> None:
    events = list(iter_events("claude", session=fake_claude_plan_write))
    plan_events = [e for e in events if e.type == "plan_event"]
    assert len(plan_events) == 1
    refs = {k: v for r in plan_events[0].refs for k, v in r.items()}
    assert refs["agent_signal"] == "claude:Write(plans/*.md)"
    assert refs["title"] == "Written Plan"
    assert refs["path"] == "/repo/plans/feature.md"


def test_codex_update_plan_emits_plan_event(
    fake_codex_plan_session: str,
) -> None:
    events = list(iter_events("codex", session=fake_codex_plan_session))
    plan_events = [e for e in events if e.type == "plan_event"]
    assert len(plan_events) == 3
    refs = {k: v for r in plan_events[0].refs for k, v in r.items()}
    assert refs["agent_signal"] == "codex:update_plan"
    assert refs["title"] == "ship the feature"


def test_antigravity_implementation_plan_emits_plan_event(
    fake_antigravity_plan_brain: str,
) -> None:
    events = list(iter_events("antigravity", session=fake_antigravity_plan_brain))
    plan_events = [e for e in events if e.type == "plan_event"]
    assert len(plan_events) == 1
    refs = {k: v for r in plan_events[0].refs for k, v in r.items()}
    assert refs["agent_signal"] == "antigravity:implementation_plan.md"
    assert refs["title"] == "Antigravity Implementation Plan"


def test_opencode_pi_emit_no_plan_event(
    fake_opencode_db: Path, fake_pi_session: Path
) -> None:
    # Neither OpenCode nor Pi have a plan signal → nothing emitted.
    oc = [e for e in iter_events("opencode") if e.type == "plan_event"]
    pi = [e for e in iter_events("pi") if e.type == "plan_event"]
    assert oc == []
    assert pi == []


# ---------------------------------------------------------------------------
# Task grouping + kind assignment
# ---------------------------------------------------------------------------


def test_redraft_yields_one_final_and_drafts(
    fake_claude_plan_redraft: str,
) -> None:
    plans = plan(session=fake_claude_plan_redraft)
    kinds = [p["kind"] for p in plans]
    # ONE slug despite drifting titles → 1 final + 3 draft, 0 completed_major.
    assert kinds.count("final") == 1
    assert kinds.count("draft") == 3
    assert kinds.count("completed_major") == 0
    # All share one task_id (grouped by SLUG, not by title/call).
    assert {p["task_id"] for p in plans} == {"plans/build-feature.md"}


def test_multitask_separates_tasks_by_slug(
    fake_claude_plan_multitask: str,
) -> None:
    plans = plan(session=fake_claude_plan_multitask)
    # Split is by plan-file slug, NOT title.
    assert {p["task_id"] for p in plans} == {
        "plans/task-a.md", "plans/task-b.md",
    }
    by_slug = {p["task_id"]: p["kind"] for p in plans}
    # Earlier slug = completed_major; the most recent slug keeps final.
    assert by_slug["plans/task-a.md"] == "completed_major"
    assert by_slug["plans/task-b.md"] == "final"


def test_codex_last_update_plan_is_final(
    fake_codex_plan_session: str,
) -> None:
    plans = plan(session=fake_codex_plan_session)
    assert [p["kind"] for p in plans] == ["draft", "draft", "final"]
    final = plans[-1]
    # The final carries the rolled-up steps + status (all completed).
    assert final["status"] == "completed"
    assert [s["status"] for s in final["steps"]] == ["completed", "completed"]


def test_antigravity_single_plan_is_final(
    fake_antigravity_plan_brain: str,
) -> None:
    plans = plan(session=fake_antigravity_plan_brain)
    assert len(plans) == 1
    assert plans[0]["kind"] == "final"
    assert plans[0]["path"].endswith("implementation_plan.md")


def test_plan_kind_filter(fake_claude_plan_redraft: str) -> None:
    finals = plan(session=fake_claude_plan_redraft, kind="final")
    drafts = plan(session=fake_claude_plan_redraft, kind="draft")
    assert len(finals) == 1
    assert len(drafts) == 3
    assert all(p["kind"] == "final" for p in finals)


def test_plan_invalid_group_raises(fake_claude_plan_redraft: str) -> None:
    with pytest.raises(ValueError):
        plan(session=fake_claude_plan_redraft, group="slug")


def test_plan_invalid_kind_raises(fake_claude_plan_redraft: str) -> None:
    with pytest.raises(ValueError):
        plan(session=fake_claude_plan_redraft, kind="bogus")


# ---------------------------------------------------------------------------
# get_body — bodies on demand; shallow drops draft bodies (S6)
# ---------------------------------------------------------------------------


def test_get_body_returns_full_plan_text(
    fake_claude_plan_redraft: str,
) -> None:
    final = plan(session=fake_claude_plan_redraft, kind="final")[0]
    body = get_body(final["id"])
    assert body["type"] == "plan_event"
    # Title drifted across the chain; the FINAL revision's body is returned.
    assert "Final plan." in body["body"]


def test_get_body_codex_steps(fake_codex_plan_session: str) -> None:
    final = plan(session=fake_codex_plan_session, kind="final")[0]
    body = get_body(final["id"])
    assert body["status"] == "completed"
    assert len(body["steps"]) == 2


def test_get_body_turn_text(fake_claude_plan_multitask: str) -> None:
    # A user_turn id resolves to its plain text.
    user_ev = query(type="user_turn", session=fake_claude_plan_multitask)[0]
    body = get_body(user_ev["id"])
    assert body["type"] == "user_turn"
    assert body["text"] == "do task A"


def test_get_body_bad_id_returns_error() -> None:
    assert get_body("")["error"] == "invalid_argument"
    assert get_body("no-such:99")["error"] == "not_found"


def test_get_body_shallow_returns_final_without_draft_bodies(
    fake_claude_plan_redraft: str,
) -> None:
    # S6: ask for a DRAFT id with shallow=True → get the FINAL plan's body,
    # and the draft bodies are elided (listed in dropped_drafts).
    drafts = plan(session=fake_claude_plan_redraft, kind="draft")
    final = plan(session=fake_claude_plan_redraft, kind="final")[0]
    shallow = get_body(drafts[0]["id"], shallow=True)
    assert shallow["id"] == final["id"]
    assert "Final plan." in shallow["body"]
    # Both draft ids were dropped (none of their bodies surfaced).
    dropped = set(shallow["dropped_drafts"])
    assert {d["id"] for d in drafts} == dropped


# ---------------------------------------------------------------------------
# Cross-agent (S5): plan_events normalize across agents in one call.
# ---------------------------------------------------------------------------


def test_plan_events_normalized_across_agents(
    fake_claude_plan_redraft: str,
    fake_codex_plan_session: str,
    fake_antigravity_plan_brain: str,
) -> None:
    events = query(type="plan_event")
    signals = {
        v
        for e in events
        for r in e["refs"]
        for k, v in r.items()
        if k == "agent_signal"
    }
    # All three agent signals surface under the ONE unified plan_event type.
    assert "claude:ExitPlanMode" in signals
    assert "codex:update_plan" in signals
    assert "antigravity:implementation_plan.md" in signals
    assert all(e["type"] == "plan_event" for e in events)


# ---------------------------------------------------------------------------
# Host-marked: real Claude data (skips when absent, but really asserts when
# present — the ``real_claude_home`` fixture unsets the fake AI_R_HOME so the
# parser reads the real ~/.claude).
# ---------------------------------------------------------------------------


from collections import Counter  # noqa: E402


_PROUD_UUID = "d61def2a-ccf8-4081-974f-bcc450c40ca0"
_FC_PREFIX = "fc1fdcf9"


def test_real_claude_proud_snacking_ritchie(real_claude_home: Path) -> None:
    """`proud-snacking-ritchie` (d61def2a): one slug, drifting titles.

    7 plan_events all write ``plans/proud-snacking-ritchie.md`` — grouping by
    slug must collapse them into ONE task: 1 final + 6 draft, 0 major.
    """
    from ai_r.parsers import claude as claude_parser

    if not claude_parser.session_exists(_PROUD_UUID):
        pytest.skip(f"{_PROUD_UUID} not present on this host")
    plans = plan(session=_PROUD_UUID, agent="claude")
    if not plans:
        pytest.skip("session present but carries no plan signals")
    kinds = Counter(p["kind"] for p in plans)
    assert kinds["final"] == 1
    assert kinds["draft"] == 6
    assert kinds["completed_major"] == 0
    # All 7 plan_events belong to the one plan-file slug.
    assert len({p["task_id"] for p in plans}) == 1


def test_real_claude_fc1fdcf9_single_slug(real_claude_home: Path) -> None:
    """`fc1fdcf9…` (serialized-shimmying-boot): one slug → one task.

    Reported to have a single plan-file slug, so all its plan_events group
    into ONE task (1 final + N drafts, 0 major).
    """
    from ai_r.parsers import claude as claude_parser

    target = None
    for sess in claude_parser.list_sessions():
        if sess.uuid.startswith(_FC_PREFIX):
            target = sess.uuid
            break
    if target is None:
        pytest.skip(f"{_FC_PREFIX} session not present on this host")
    plans = plan(session=target, agent="claude")
    if not plans:
        pytest.skip("session present but carries no plan signals")
    kinds = Counter(p["kind"] for p in plans)
    # One slug → one task: exactly one final and no superseded-major plans.
    assert kinds["final"] == 1
    assert kinds["completed_major"] == 0
    assert len({p["task_id"] for p in plans}) == 1
