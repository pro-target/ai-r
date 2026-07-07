"""Tests for the Phase-1 Event model + ``query`` core (``ai_r.events``).

Hermetic by default: the autouse ``_isolate_ai_r_home`` fixture (see
``conftest.py``) points every parser at a per-test temp ``$HOME`` so no
real session data leaks in.  The one host-dependent test requests the
``real_claude_dir`` fixture and is therefore auto-tagged ``@pytest.mark.host``
— it *skips* (never fails) when the host carries no Claude data.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from ai_r.events import (
    Event,
    classify_tool,
    intent,
    iter_events,
    query,
    reaction,
)


# ---------------------------------------------------------------------------
# Fixture: a Claude session with several user turns interleaved with an
# assistant turn + a tool_use edit, so prev/next walks have something to
# traverse.  Timeline (parse order):
#   0 user_turn        "first request"
#   1 assistant_turn   "on it"
#   2 tool_call(edit)  Edit -> /repo/a.py
#   3 user_turn        "second request please"
#   4 assistant_turn   "sure"
#   5 user_turn        "third and final"
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


@pytest.fixture
def multi_turn_claude(tmp_sessions_dir: Path) -> str:
    session_id = "events-multi-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-e"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "first request"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "on it"},
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": "/repo/a.py",
                                "old_string": "x",
                                "new_string": "y",
                            },
                        },
                    ],
                },
                "timestamp": "2026-06-14T10:00:05Z",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "second request please"},
                "timestamp": "2026-06-14T10:00:10Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "sure"}],
                },
                "timestamp": "2026-06-14T10:00:15Z",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "third and final"},
                "timestamp": "2026-06-14T10:00:20Z",
                "sessionId": session_id,
            },
        ],
    )
    return session_id


@pytest.fixture
def second_claude_session(tmp_sessions_dir: Path) -> str:
    """A second, later Claude session next to ``multi_turn_claude`` (F3.2)."""
    session_id = "events-multi-2"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-e"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "later session ask"},
                "timestamp": "2026-06-15T09:00:00Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "later on it"}],
                },
                "timestamp": "2026-06-15T09:00:05Z",
                "sessionId": session_id,
            },
        ],
    )
    return session_id


# ---------------------------------------------------------------------------
# classify_tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Edit", "edit"),
        ("MultiEdit", "edit"),
        ("apply_patch", "edit"),
        ("Write", "write"),
        ("create_file", "write"),
        ("Read", "read"),
        ("Bash", "bash"),
        ("shell", "bash"),
        ("local_shell_call", "bash"),
        ("SomethingWeird", "other"),
        ("", "other"),
    ],
)
def test_classify_tool(name: str, expected: str) -> None:
    assert classify_tool(name) == expected


# ---------------------------------------------------------------------------
# Normalization: iter_events
# ---------------------------------------------------------------------------


def test_iter_events_normalizes_each_type(multi_turn_claude: str) -> None:
    events = list(iter_events("claude", session=multi_turn_claude))
    types = [e.type for e in events]
    assert types == [
        "user_turn",
        "assistant_turn",
        "tool_call(edit)",
        "user_turn",
        "assistant_turn",
        "user_turn",
    ]
    # Every event is a frozen Event with a stable, session-scoped id + hash.
    assert all(isinstance(e, Event) for e in events)
    ids = [e.id for e in events]
    assert ids == [f"{multi_turn_claude}:{i}" for i in range(len(events))]
    assert all(e.session_id == multi_turn_claude for e in events)
    assert all(e.agent == "claude" for e in events)
    assert all(e.source == "parser:claude" for e in events)
    assert all(len(e.sha256) == 64 for e in events)


def test_iter_events_tool_call_refs(multi_turn_claude: str) -> None:
    events = list(iter_events("claude", session=multi_turn_claude))
    edit = next(e for e in events if e.type == "tool_call(edit)")
    assert edit.text == "Edit"
    files = [r["file"] for r in edit.refs if "file" in r]
    tools = [r["tool"] for r in edit.refs if "tool" in r]
    assert files == ["/repo/a.py"]
    assert tools == ["Edit"]


@pytest.fixture
def claude_tool_outcomes(tmp_sessions_dir: Path) -> str:
    """Claude session: one failing Bash call + one succeeding Edit call.

    Each call's outcome arrives in a following user record as a
    ``tool_result`` block carrying ``is_error`` and the matching
    ``tool_use_id`` (Claude's real correlation key).
    """
    session_id = "events-outcome-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-e"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_bad", "name": "Bash",
                         "input": {"command": "pytest"}},
                    ],
                },
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu_bad",
                     "is_error": True, "content": "boom: command failed"},
                ]},
                "timestamp": "2026-06-14T10:00:01Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_ok", "name": "Edit",
                         "input": {"file_path": "/repo/a.py",
                                   "old_string": "x", "new_string": "y"}},
                    ],
                },
                "timestamp": "2026-06-14T10:00:02Z",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu_ok",
                     "is_error": False, "content": "applied"},
                ]},
                "timestamp": "2026-06-14T10:00:03Z",
                "sessionId": session_id,
            },
        ],
    )
    return session_id


def test_tool_call_events_carry_is_error(claude_tool_outcomes: str) -> None:
    """Success/error is visible on the existing ``tool_call`` events — the
    outcome is correlated by ``tool_use_id`` and attached as an
    ``is_error`` ref (no new event type is introduced)."""
    events = list(iter_events("claude", session=claude_tool_outcomes))
    # No new event type: only user_turn / tool_call(*) appear here.
    assert {e.type for e in events} <= {
        "tool_call(bash)", "tool_call(edit)",
    }
    bash = next(e for e in events if e.type == "tool_call(bash)")
    edit = next(e for e in events if e.type == "tool_call(edit)")
    bash_err = [r["is_error"] for r in bash.refs if "is_error" in r]
    edit_err = [r["is_error"] for r in edit.refs if "is_error" in r]
    assert bash_err == [True]
    assert edit_err == [False]


def test_bare_tool_call_filter_unaffected_by_outcomes(
    claude_tool_outcomes: str,
) -> None:
    """A ``tool_call``-prefixed type filter still sees every call — attaching
    outcomes did not change event ``type`` values or counts."""
    events = list(iter_events("claude", session=claude_tool_outcomes))
    calls = [e for e in events if e.type.startswith("tool_call(")]
    assert len(calls) == 2


def test_sha256_deterministic(multi_turn_claude: str) -> None:
    a = list(iter_events("claude", session=multi_turn_claude))
    b = list(iter_events("claude", session=multi_turn_claude))
    assert [e.sha256 for e in a] == [e.sha256 for e in b]


# ---------------------------------------------------------------------------
# relative_to / direction / n  (prev + next, n=1 and n=all)
# ---------------------------------------------------------------------------


def _anchor(session: str, want_type: str, occurrence: int = 0) -> str:
    """Return the event id of the ``occurrence``-th ``want_type`` event."""
    events = list(iter_events("claude", session=session))
    hits = [e for e in events if e.type == want_type]
    return hits[occurrence].id


def test_relative_prev_n1_matches_previous_user_intent(
    multi_turn_claude: str,
) -> None:
    # Anchor on the edit tool_call; prev/1 = the request behind the edit,
    # exactly what ``previous_user_intent`` returns for find_file_edits.
    anchor = _anchor(multi_turn_claude, "tool_call(edit)")
    got = query(relative_to=anchor, direction="prev", n=1)
    assert [e["text"] for e in got] == ["first request"]


def test_relative_next_n1(multi_turn_claude: str) -> None:
    # Anchor on the edit; next/1 user turn is "second request please".
    anchor = _anchor(multi_turn_claude, "tool_call(edit)")
    got = query(relative_to=anchor, direction="next", n=1)
    assert [e["text"] for e in got] == ["second request please"]


def test_relative_prev_n_all(multi_turn_claude: str) -> None:
    # Anchor on the LAST user turn; prev/all user turns (timeline order).
    anchor = _anchor(multi_turn_claude, "user_turn", occurrence=2)
    got = query(relative_to=anchor, direction="prev", n="all")
    assert [e["text"] for e in got] == [
        "first request",
        "second request please",
    ]


def test_relative_next_n_all(multi_turn_claude: str) -> None:
    # Anchor on the FIRST user turn; next/all user turns (timeline order).
    anchor = _anchor(multi_turn_claude, "user_turn", occurrence=0)
    got = query(relative_to=anchor, direction="next", n="all")
    assert [e["text"] for e in got] == [
        "second request please",
        "third and final",
    ]


def test_intent_preset_equals_query_prev(multi_turn_claude: str) -> None:
    anchor = _anchor(multi_turn_claude, "tool_call(edit)")
    assert intent(anchor, 1) == query(
        relative_to=anchor, direction="prev", n=1
    )


def test_reaction_preset_equals_query_next(multi_turn_claude: str) -> None:
    anchor = _anchor(multi_turn_claude, "tool_call(edit)")
    assert reaction(anchor, 1) == query(
        relative_to=anchor, direction="next", n=1
    )


def test_relative_to_unknown_anchor_is_empty(multi_turn_claude: str) -> None:
    assert query(relative_to="no-such:99", direction="prev") == []


# ---------------------------------------------------------------------------
# Facet filters
# ---------------------------------------------------------------------------


def test_type_facet_bare_tool_call_matches_subtype(
    multi_turn_claude: str,
) -> None:
    got = query(type="tool_call", session=multi_turn_claude)
    assert [e["type"] for e in got] == ["tool_call(edit)"]
    # Exact subtype also matches.
    got2 = query(type="tool_call(edit)", session=multi_turn_claude)
    assert len(got2) == 1


def test_file_facet(multi_turn_claude: str) -> None:
    got = query(file="/repo/a.py", session=multi_turn_claude)
    assert len(got) == 1
    assert got[0]["type"] == "tool_call(edit)"


def test_tool_facet_pattern(multi_turn_claude: str) -> None:
    got = query(tool="edi", session=multi_turn_claude)  # substring of "Edit"
    assert len(got) == 1


def test_text_facet_substring(multi_turn_claude: str) -> None:
    got = query(text="request", session=multi_turn_claude)
    texts = sorted(e["text"] for e in got)
    assert texts == ["first request", "second request please"]


def test_date_sort_ascending(multi_turn_claude: str) -> None:
    got = query(type="user_turn", session=multi_turn_claude, sort="date")
    ts = [e["ts"] for e in got]
    assert ts == sorted(ts)


def test_invalid_direction_raises() -> None:
    with pytest.raises(ValueError):
        query(relative_to="x:0", direction="sideways")


def test_invalid_sort_raises() -> None:
    with pytest.raises(ValueError):
        query(sort="magic")


def test_invalid_n_raises() -> None:
    with pytest.raises(ValueError):
        query(relative_to="x:0", n="two")
    with pytest.raises(ValueError):
        query(relative_to="x:0", n=0)


def test_numeric_string_n_warns_deprecation(multi_turn_claude: str) -> None:
    # A numeric string ("3") still walks (accept preserved), but the surface is
    # moving to int | "all": the call must emit a DeprecationWarning naming the
    # 0.6.0 removal, while the walk result is byte-for-byte what int 3 returns.
    anchor = _anchor(multi_turn_claude, "user_turn", occurrence=2)
    with pytest.warns(DeprecationWarning, match=r"0\.6\.0"):
        got_str = query(relative_to=anchor, direction="prev", n="3")
    # accept unchanged: "3" collects the same turns as int 3.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        got_int = query(relative_to=anchor, direction="prev", n=3)
    assert [e["text"] for e in got_str] == [e["text"] for e in got_int]
    assert [e["text"] for e in got_str] == ["first request", "second request please"]


def test_int_and_all_n_do_not_warn(multi_turn_claude: str) -> None:
    # The clean arms (int 1, int 3, and the "all" sentinel) must stay silent —
    # only the numeric-string spelling is deprecated.
    anchor = _anchor(multi_turn_claude, "user_turn", occurrence=2)
    for nv in (1, 3, "all"):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            query(relative_to=anchor, direction="prev", n=nv)


def test_query_kind_facet_removed() -> None:
    # ``kind`` was a duplicate of ``noise`` (noise=exclude≡top-level,
    # noise=only≡subagents) — it is gone from the ``query`` signature.
    with pytest.raises(TypeError):
        query(kind="subagent")  # type: ignore[call-arg]


def test_query_parent_empty_string_rejected() -> None:
    # A non-None parent must be a non-empty uuid; an empty string is a caller
    # mistake, fail loud (not a silent "match nothing").
    with pytest.raises(ValueError, match="parent"):
        query(parent="")


def test_query_group_empty_string_rejected() -> None:
    with pytest.raises(ValueError, match="group"):
        query(group="")


# ---------------------------------------------------------------------------
# ``parent`` — session-level subtree filter (spawned-subagent tree)
# ---------------------------------------------------------------------------


def _session_ids(events: list[dict]) -> set[str]:
    return {e["session_id"] for e in events}


def test_query_parent_returns_direct_children(
    fake_codex_session: Path, fake_codex_subagent: Path
) -> None:
    # Tree: test-codex-1 (root) → test-codex-sub-1 (direct child).
    # parent=root returns the child's events; the root itself is excluded.
    got = query(agent="codex", parent="test-codex-1")
    ids = _session_ids(got)
    assert "test-codex-sub-1" in ids
    assert "test-codex-1" not in ids  # the root's own events → session=root


def test_query_parent_returns_full_subtree_transitively(
    fake_codex_session: Path,
    fake_codex_subagent: Path,
    fake_codex_subagent_depth2: Path,
) -> None:
    # Tree: test-codex-1 → test-codex-sub-1 → test-codex-sub-3.
    # parent=root returns BOTH the direct child and the nested grandchild.
    got = query(agent="codex", parent="test-codex-1")
    ids = _session_ids(got)
    assert {"test-codex-sub-1", "test-codex-sub-3"} <= ids
    assert "test-codex-1" not in ids
    # parent=direct-child returns only its own subtree (the grandchild),
    # never the sibling root — the closure is anchored, not global.
    nested = query(agent="codex", parent="test-codex-sub-1")
    nids = _session_ids(nested)
    assert nids == {"test-codex-sub-3"}


def test_query_parent_unknown_uuid_is_empty(
    fake_codex_session: Path, fake_codex_subagent: Path
) -> None:
    # An unknown / childless parent matches nothing — an honest empty result,
    # never an error.
    assert query(agent="codex", parent="no-such-session") == []


# ---------------------------------------------------------------------------
# ``group`` — event-level plan-task filter (plan_event only)
# ---------------------------------------------------------------------------


def _task_ids(session_id: str) -> dict[str, str]:
    """Map plan_event id → task_id for a session (via the SSOT grouper)."""
    from ai_r.events.plan import _assign_plan_kinds

    events = query(type="plan_event", session=session_id, redact=False)
    return {p.id: p.task_id for p in _assign_plan_kinds(events)}


def test_query_group_keeps_only_matching_task(
    fake_claude_plan_multitask: str,
) -> None:
    # Two distinct plan-file slugs → two task_ids; group=<one> returns only
    # that task's plan_events, the other task is excluded.
    tasks = _task_ids(fake_claude_plan_multitask)
    distinct = set(tasks.values())
    assert len(distinct) >= 2  # multitask fixture yields >1 task
    target = sorted(distinct)[0]
    got = query(type="plan_event", session=fake_claude_plan_multitask,
                group=target)
    got_ids = {e["id"] for e in got}
    assert got_ids  # non-empty
    assert got_ids == {pid for pid, tid in tasks.items() if tid == target}
    # every excluded plan_event belonged to a different task
    excluded_tasks = {tasks[e["id"]] for e in got}
    assert excluded_tasks == {target}


def test_query_group_all_revisions_of_one_task(
    fake_claude_plan_redraft: str,
) -> None:
    # A single-slug redraft chain is ONE task — group=<its task_id> returns
    # every revision (drafts + final), and there is exactly one task.
    tasks = _task_ids(fake_claude_plan_redraft)
    (only_task,) = set(tasks.values())
    got = query(type="plan_event", session=fake_claude_plan_redraft,
                group=only_task)
    assert {e["id"] for e in got} == set(tasks)


def test_query_group_excludes_non_plan_events(
    fake_claude_plan_multitask: str,
) -> None:
    # group is a plan-only facet: with a task_id set, non-plan_event events
    # are dropped even without an explicit type filter.
    tasks = _task_ids(fake_claude_plan_multitask)
    target = sorted(set(tasks.values()))[0]
    got = query(session=fake_claude_plan_multitask, group=target)
    assert got  # the plan_events of the task survive
    assert all(e["type"] == "plan_event" for e in got)


def test_query_group_with_non_plan_type_is_empty(
    fake_claude_plan_multitask: str,
) -> None:
    # Contradictory facets (group + a non-plan type) → honest empty result.
    tasks = _task_ids(fake_claude_plan_multitask)
    target = sorted(set(tasks.values()))[0]
    got = query(type="user_turn", session=fake_claude_plan_multitask,
                group=target)
    assert got == []
def test_negative_limit_rejected(multi_turn_claude: str) -> None:
    # Regression (audit PR#4 defect #5): a negative ``limit`` used to slip
    # through the ``if limit and len(survivors) > limit`` guard and silently
    # slice ``survivors[:-1]`` — dropping the NEWEST event without a word.
    # It must now fail loud, symmetric with network/incidents/find_file_edits.
    for bad in (-1, -25):
        with pytest.raises(ValueError, match="non-negative integer"):
            query(type="user_turn", session=multi_turn_claude, limit=bad)
    # bool is not a valid int limit either (True == 1 would be a silent cap).
    with pytest.raises(ValueError, match="non-negative integer"):
        query(type="user_turn", session=multi_turn_claude, limit=True)  # type: ignore[arg-type]


def test_valid_limit_unchanged(multi_turn_claude: str) -> None:
    # The fix must NOT alter valid values.  Three user turns in date order:
    # "first request" / "second request please" / "third and final".
    all_turns = query(type="user_turn", session=multi_turn_claude, limit=0)
    texts = [e["text"] for e in all_turns]
    assert texts == ["first request", "second request please", "third and final"]
    # limit=0 == no cap (returns all three).
    assert query(type="user_turn", session=multi_turn_claude) == all_turns
    # A positive cap keeps the oldest N; the newest is what falls off — the
    # exact opposite of what limit=-1 silently did (it dropped the newest).
    capped = query(type="user_turn", session=multi_turn_claude, limit=2)
    assert [e["text"] for e in capped] == ["first request", "second request please"]


# ---------------------------------------------------------------------------
# F3.2: the ``session`` facet accepts a LIST of uuids (union of sessions)
# ---------------------------------------------------------------------------


def test_session_list_returns_union_of_sessions(
    multi_turn_claude: str, second_claude_session: str
) -> None:
    got = query(
        type="user_turn",
        session=[multi_turn_claude, second_claude_session],
    )
    # Union of both sessions' user turns, chronological across sessions.
    assert {e["session_id"] for e in got} == {
        multi_turn_claude, second_claude_session
    }
    texts = [e["text"] for e in got]
    assert texts == [
        "first request",
        "second request please",
        "third and final",
        "later session ask",  # 2026-06-15 > 2026-06-14 → last
    ]
    ts = [e["ts"] for e in got]
    assert ts == sorted(ts)


def test_session_list_single_element_equals_scalar(
    multi_turn_claude: str, second_claude_session: str
) -> None:
    # A one-element list is exactly the historical single-uuid filter.
    assert query(session=[multi_turn_claude]) == query(
        session=multi_turn_claude
    )


def test_session_list_dedups_and_ignores_unknown_uuid(
    multi_turn_claude: str, second_claude_session: str
) -> None:
    # Duplicates collapse; an unknown uuid contributes nothing (the same
    # honest empty-miss semantics as the single-uuid form) — no invented
    # events, no error.
    got = query(
        session=[multi_turn_claude, multi_turn_claude, "no-such-session"]
    )
    assert got == query(session=multi_turn_claude)


def test_session_list_empty_raises() -> None:
    # [] is ambiguous ("no filter" vs "match nothing") → fail-loud, never
    # a silent unfiltered scan.
    with pytest.raises(ValueError, match="session list must not be empty"):
        query(session=[])


def test_session_list_bad_items_raise() -> None:
    for bad in ([123], ["ok-uuid", ""], ["ok-uuid", "   "], [None]):
        with pytest.raises(ValueError, match="session list items"):
            query(session=bad)


def test_session_non_string_scalar_raises() -> None:
    with pytest.raises(ValueError, match="session must be"):
        query(session=123)


def test_session_list_validated_on_relative_walk_too() -> None:
    # Like tool_kind/sort, the session facet is validated even when the
    # relative_to walk would otherwise ignore it.
    with pytest.raises(ValueError, match="session list must not be empty"):
        query(relative_to="x:0", session=[])


def test_iter_events_accepts_session_list(
    multi_turn_claude: str, second_claude_session: str
) -> None:
    events = list(
        iter_events(
            "claude", session=[multi_turn_claude, second_claude_session]
        )
    )
    assert {e.session_id for e in events} == {
        multi_turn_claude, second_claude_session
    }
    # Scalar fast-path unchanged.
    only_first = list(iter_events("claude", session=multi_turn_claude))
    assert {e.session_id for e in only_first} == {multi_turn_claude}


# ---------------------------------------------------------------------------
# BM25 parity: text+sort=relevance re-uses the search_sessions scorer.
# ---------------------------------------------------------------------------


def test_relevance_reuses_search_sessions_bm25() -> None:
    """``query(text, sort=relevance)`` orders by the SAME BM25 scorer.

    We feed a synthetic corpus of event texts through both the public
    ``ai_r.ranking.bm25_scores`` (what ``search_sessions`` calls) and the
    ordering produced inside ``query`` to assert they agree — proving no
    algorithm was re-implemented.
    """
    from ai_r.ranking import bm25_scores, tokenize

    docs = [
        "alpha beta gamma",
        "alpha alpha delta",  # highest tf for "alpha"
        "beta only here",
    ]
    q = "alpha"
    scores = bm25_scores(tokenize(q), [tokenize(d) for d in docs])
    expected_order = sorted(
        range(len(docs)), key=lambda i: scores[i], reverse=True
    )
    # The two docs containing "alpha" must outrank the one that doesn't,
    # and the double-"alpha" doc must be first.
    assert expected_order[0] == 1
    assert 2 == expected_order[-1]


# ---------------------------------------------------------------------------
# Host-dependent parity: a real relevance query returns events ordered by
# the same BM25 signal search_sessions uses.  Skips when no Claude data.
# ---------------------------------------------------------------------------


def test_relevance_order_on_real_data(real_claude_dir: Path) -> None:
    from ai_r.ranking import bm25_scores, tokenize

    term = "the"
    events = query(
        text=term, agent="claude", sort="relevance", type="user_turn",
        limit=25,
    )
    if len(events) < 2:
        pytest.skip("not enough matching real user turns for a parity check")
    # Recompute BM25 over the returned texts; the returned order must be
    # non-increasing in score (identical scorer, identical inputs).
    docs = [(e["text"] or "").lower() for e in events]
    scores = bm25_scores(tokenize(term), [tokenize(d) for d in docs])
    assert scores == sorted(scores, reverse=True)
