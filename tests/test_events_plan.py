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

from ai_r.events import get_body, iter_events, plan, plan_feedback, query


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
# F3.4 v1 — final plan body inline + «quote → comment» feedback pairs
# ---------------------------------------------------------------------------


def test_plan_inlines_final_body_by_default(
    fake_claude_plan_redraft: str,
) -> None:
    plans = plan(session=fake_claude_plan_redraft)
    finals = [p for p in plans if p["kind"] == "final"]
    drafts = [p for p in plans if p["kind"] == "draft"]
    assert len(finals) == 1
    # The final's full text is inlined; source is the plan signal (no
    # approval carried an edited body in this fixture).
    assert "Final plan." in finals[0]["body"]
    assert finals[0]["body_source"] == "plan_signal"
    # Drafts stay references — no body key ever.
    assert all("body" not in d for d in drafts)


def test_plan_bodies_none_restores_reference_only_shape(
    fake_claude_plan_redraft: str,
) -> None:
    plans = plan(session=fake_claude_plan_redraft, bodies="none")
    assert all("body" not in p and "body_source" not in p for p in plans)


def test_plan_bodies_invalid_raises(fake_claude_plan_redraft: str) -> None:
    with pytest.raises(ValueError):
        plan(session=fake_claude_plan_redraft, bodies="all")


def test_plan_final_body_prefers_approval_edited_text(
    fake_claude_plan_feedback: str,
) -> None:
    # The approval tool_result carried "## Approved Plan (edited by user):"
    # — the AUTHORITATIVE text; it must override the ExitPlanMode input body.
    finals = plan(session=fake_claude_plan_feedback, kind="final")
    assert len(finals) == 1
    assert "EDITED final body by user." in finals[0]["body"]
    assert "Draft three body." not in finals[0]["body"]
    assert finals[0]["body_source"] == "approval_edited_by_user"


def test_plan_codex_final_body_is_honest_null(
    fake_codex_plan_session: str,
) -> None:
    # Codex update_plan carries steps, never a text body → honest None.
    final = plan(session=fake_codex_plan_session, kind="final")[0]
    assert final["body"] is None
    assert final["body_source"] is None
    assert final["steps"]  # steps still enriched


def test_plan_feedback_extracts_all_pairs_chronologically(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback)
    # pf0 (rejected): free-text preamble + 2 selections; pf1 (stay): 2 [Re:]
    # pairs.  The tu-plan-0 technical failure and the tu-plan-3 approval
    # contribute no pairs.
    assert [(p["quote"], p["verdict"]) for p in pairs] == [
        (None, "rejected"),
        ("Draft one body.", "rejected"),
        ("Feature Plan", "rejected"),
        ("Draft two body.", "stay_in_plan_mode"),
        ("rollout", "stay_in_plan_mode"),
    ]
    assert pairs[0]["comment"] == "Overall too vague."
    assert pairs[2]["comment"] == "Rename the feature."
    # Multi-line comment survives verbatim.
    assert pairs[4]["comment"] == (
        "Which rollout?\nMore thoughts on a second line."
    )
    # Every pair carries ts + agent.
    assert all(p["agent"] == "claude" and p["ts"] for p in pairs)


def test_plan_feedback_binds_pairs_to_the_answered_revision(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback)
    plans = plan(session=fake_claude_plan_feedback, bodies="none")
    # Plan events are ordered tu-plan-0..3; the rejection answered the
    # SECOND revision, the stay-in-plan-mode the THIRD.
    ids = [p["id"] for p in plans]
    assert {p["plan_id"] for p in pairs if p["verdict"] == "rejected"} == {
        ids[1]
    }
    assert {
        p["plan_id"] for p in pairs if p["verdict"] == "stay_in_plan_mode"
    } == {ids[2]}


def test_plan_feedback_refs_resolve_to_raw_response(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback)
    # Filtered responses shift the ordinals: the technical failure is NOT
    # pf0 — the rejection is.
    rejected_ref = pairs[0]["ref"]
    assert rejected_ref == f"{fake_claude_plan_feedback}:pf0"
    body = get_body(rejected_ref, redact=False)
    assert body["type"] == "plan_feedback"
    assert body["verdict"] == "rejected"
    # The RAW response is returned whole — boilerplate included.
    assert body["text"].startswith("The user doesn't want to proceed")
    assert "On selected text:" in body["text"]
    assert body["plan_id"] == pairs[0]["plan_id"]
    assert [p["quote"] for p in body["pairs"]] == [
        None, "Draft one body.", "Feature Plan",
    ]


def test_get_body_feedback_ref_redacts_secrets(
    fake_claude_plan_feedback: str,
) -> None:
    ref = f"{fake_claude_plan_feedback}:pf0"
    body = get_body(ref)  # redact=True default
    assert "abc12345secret" not in body["text"]
    assert "[REDACTED_GENERIC_SECRET]" in body["text"]
    assert body["redactions"]["GENERIC_SECRET"] >= 1


def test_get_body_feedback_ref_out_of_range_is_not_found(
    fake_claude_plan_feedback: str,
) -> None:
    res = get_body(f"{fake_claude_plan_feedback}:pf99")
    assert res["error"] == "not_found"


def test_get_body_feedback_ref_unknown_session_is_not_found() -> None:
    assert get_body("no-such-session:pf0")["error"] == "not_found"


def test_plan_feedback_empty_for_agents_without_signal(
    fake_codex_plan_session: str,
    fake_antigravity_plan_brain: str,
) -> None:
    # Codex update_plan is fire-and-forget; Antigravity's plan is a file —
    # no approval flow, no feedback signal → honest empty, not fabricated.
    assert plan_feedback(session=fake_codex_plan_session) == []
    assert plan_feedback(session=fake_antigravity_plan_brain) == []


def test_plan_feedback_empty_without_responses(
    fake_claude_plan_redraft: str,
) -> None:
    # A claude session whose ExitPlanMode calls got no recorded verdicts.
    assert plan_feedback(session=fake_claude_plan_redraft) == []


# ---------------------------------------------------------------------------
# F3.4 v2 — draft numbering v1…vN + quote→section anchoring + rounds
# ---------------------------------------------------------------------------


def test_plan_atoms_carry_chronological_versions(
    fake_claude_plan_redraft: str,
) -> None:
    plans = plan(session=fake_claude_plan_redraft, bodies="none")
    # One task, 4 revisions → v1..v4 in (ts, seq) order; the final is vN.
    assert [p["version"] for p in plans] == [1, 2, 3, 4]
    final = [p for p in plans if p["kind"] == "final"][0]
    assert final["version"] == 4
    assert all(p["version"] < 4 for p in plans if p["kind"] == "draft")


def test_plan_versions_restart_per_task(
    fake_claude_plan_multitask: str,
) -> None:
    plans = plan(session=fake_claude_plan_multitask)
    # Two single-revision tasks → each task numbers from v1.
    versions = {p["task_id"]: p["version"] for p in plans}
    assert versions == {"plans/task-a.md": 1, "plans/task-b.md": 1}


def test_feedback_pairs_carry_plan_version(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback)
    # The rejection answered revision 2 (tu-plan-1), the stay-in-plan-mode
    # answered revision 3 (tu-plan-2) — versions ride on every pair.
    assert {
        p["plan_version"] for p in pairs if p["verdict"] == "rejected"
    } == {2}
    assert {
        p["plan_version"] for p in pairs
        if p["verdict"] == "stay_in_plan_mode"
    } == {3}


def test_feedback_quote_anchors_through_render_markup(
    fake_claude_plan_sections: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_sections)
    by_comment = {p["comment"]: p for p in pairs}
    # Rendered quote (bold/backticks stripped by the UI) anchors to the
    # section whose SOURCE carries the markup.
    assert by_comment["Why canary?"]["section"] == "Rollout Strategy"
    # A bullet-list quote (marker rendered away) anchors too.
    assert by_comment["Too slow."]["section"] == "Rollout Strategy"


def test_feedback_quote_anchor_ambiguous_is_null(
    fake_claude_plan_sections: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_sections)
    by_comment = {p["comment"]: p for p in pairs}
    # The phrase lives in BOTH "Testing" and "Cleanup" — ambiguity is an
    # honest null, not a first-match guess.
    assert by_comment["Which one?"]["section"] is None


def test_feedback_quote_anchor_miss_is_null(
    fake_claude_plan_sections: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_sections)
    by_comment = {p["comment"]: p for p in pairs}
    # A quote absent from the plan body → null anchor, never the nearest.
    assert by_comment["Anchor me if you can."]["section"] is None


def test_feedback_free_text_pair_has_null_anchor(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback)
    free_text = [p for p in pairs if p["quote"] is None]
    assert free_text
    assert all(p["section"] is None for p in free_text)


def test_feedback_heading_quote_anchors_to_its_section(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback)
    by_quote = {p["quote"]: p for p in pairs}
    # Quoting the heading itself anchors to that section (the heading line
    # belongs to its section).
    assert by_quote["Feature Plan"]["section"] == "Feature Plan"
    assert by_quote["Draft one body."]["section"] == "Feature Plan"
    # "rollout" is not in revision 3's body → honest miss.
    assert by_quote["rollout"]["section"] is None


def test_feedback_pairs_grouped_by_rounds(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback)
    # Round 1 = the rejection (3 pairs), round 2 = stay-in-plan-mode (2).
    assert [p["round"] for p in pairs] == [1, 1, 1, 2, 2]


def test_feedback_rounds_last_keeps_final_round_only(
    fake_claude_plan_feedback: str,
) -> None:
    pairs = plan_feedback(session=fake_claude_plan_feedback, rounds="last")
    assert len(pairs) == 2
    assert all(p["round"] == 2 for p in pairs)
    assert all(p["verdict"] == "stay_in_plan_mode" for p in pairs)


def test_feedback_rounds_invalid_raises(
    fake_claude_plan_feedback: str,
) -> None:
    with pytest.raises(ValueError):
        plan_feedback(session=fake_claude_plan_feedback, rounds="first")


def test_rejected_write_plan_correlates_to_its_revision(
    fake_claude_plan_write_rejected: str,
) -> None:
    # v1 boundary fix: plan call-ids come from the plan-signal SSOT, so a
    # rejected ``Write plans/*.md`` correlates like an ExitPlanMode verdict.
    pairs = plan_feedback(session=fake_claude_plan_write_rejected)
    assert len(pairs) == 1
    p = pairs[0]
    assert p["verdict"] == "rejected"
    assert p["quote"] is None  # free-text rejection, no selection UI
    assert p["comment"] == "Don't write plan files, refine the plan first."
    plans = plan(session=fake_claude_plan_write_rejected, bodies="none")
    assert p["plan_id"] == plans[0]["id"]
    assert p["plan_version"] == 1
    assert p["round"] == 1


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
