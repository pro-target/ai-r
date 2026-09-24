"""Tests for the ZCode parser (SQLite store + rollout JSONL fallback)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ai_r.parsers import AgentName, zcode
from ai_r.parsers.models import Message


# ---------------------------------------------------------------------------
# SQLite layer (the canonical store)
# ---------------------------------------------------------------------------


def test_list_sessions_from_db(fake_zcode_db: Path) -> None:
    sessions = zcode.list_sessions(override=str(fake_zcode_db))
    by_id = {s.uuid: s for s in sessions}
    assert "sess_test-zc-1" in by_id
    assert "sess_test-zc-2-sub" in by_id

    main = by_id["sess_test-zc-1"]
    assert main.agent is AgentName.ZCODE
    assert main.title == "Add zcode support"
    assert main.parent_uuid is None
    assert main.kind == "agent"
    assert main.project_dir == "/home/user/proj"
    assert main.message_count == 3
    assert main.models == ("GLM-5.3",)

    child = by_id["sess_test-zc-2-sub"]
    assert child.parent_uuid == "sess_test-zc-1"
    assert child.kind == "subagent"


def test_list_sessions_default_path_via_ai_r_home(
    fake_zcode_db: Path,
) -> None:
    """With no override the parser resolves ``$AI_R_HOME/.zcode/cli/db``."""
    sessions = zcode.list_sessions()
    ids = {s.uuid for s in sessions}
    assert "sess_test-zc-1" in ids
    assert "sess_test-zc-2-sub" in ids


def test_read_session(fake_zcode_db: Path) -> None:
    s = zcode.read_session("sess_test-zc-1", override=str(fake_zcode_db))
    assert s.uuid == "sess_test-zc-1"
    assert s.agent is AgentName.ZCODE
    assert s.path == str(fake_zcode_db)
    # time_updated is ms-epoch → tz-aware datetime.
    assert s.date.tzinfo is not None


def test_read_session_invalid_uuid(fake_zcode_db: Path) -> None:
    with pytest.raises(ValueError):
        zcode.read_session("../escape", override=str(fake_zcode_db))
    with pytest.raises(ValueError):
        zcode.read_session("", override=str(fake_zcode_db))


def test_read_session_missing(fake_zcode_db: Path) -> None:
    with pytest.raises(FileNotFoundError):
        zcode.read_session("sess_nope", override=str(fake_zcode_db))


def test_read_messages_structures(fake_zcode_db: Path) -> None:
    messages = zcode.read_messages(
        "sess_test-zc-1", override=str(fake_zcode_db)
    )
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant", "assistant"]

    user = messages[0]
    assert user.text == "Add zcode support"

    first = messages[1]
    assert first.thinking == "Parse the db first."
    assert first.text == "Reading the store."
    assert len(first.tool_use) == 1
    call = first.tool_use[0]
    assert call["name"] == "Read"
    assert call["tool_use_id"] == "call_zc_1"
    assert "file_path" in call["input"]
    assert len(first.tool_result) == 1
    result = first.tool_result[0]
    assert result["content"] == "10 lines"
    assert result["is_error"] is False
    assert result["tool_use_id"] == "call_zc_1"
    assert first.model == "GLM-5.3"
    assert first.tokens == {
        "input": 100,
        "output": 20,
        "reasoning": 0,
        "cache_read": 40,
        "cache_write": 0,
        "total": 120,
    }

    # The errored call: no output → ``state.error`` becomes the result.
    second = messages[2]
    assert second.tool_result[0]["is_error"] is True
    assert second.tool_result[0]["content"] == "old_string not found"


def test_read_token_usage_sums_per_call_blocks(fake_zcode_db: Path) -> None:
    usage = zcode.read_token_usage(
        "sess_test-zc-1", override=str(fake_zcode_db)
    )
    assert usage is not None
    assert usage["input"] == 150  # 100 + 50
    assert usage["output"] == 30  # 20 + 10
    assert usage["cache_read"] == 40
    assert usage["total"] == 180  # recorded per-call totals: 120 + 60


def test_read_token_usage_missing_returns_none(fake_zcode_db: Path) -> None:
    """The child session has no usable tokens block → honest None."""
    assert (
        zcode.read_token_usage(
            "sess_test-zc-2-sub", override=str(fake_zcode_db)
        )
        is None
    )


def test_search(fake_zcode_db: Path) -> None:
    hits = zcode.search("zcode", override=str(fake_zcode_db))
    assert {h.uuid for h in hits} == {"sess_test-zc-1", "sess_test-zc-2-sub"}
    assert zcode.search("zzz", override=str(fake_zcode_db)) == []
    assert zcode.search("", override=str(fake_zcode_db)) == []


def test_session_exists(fake_zcode_db: Path) -> None:
    assert (
        zcode.session_exists("sess_test-zc-1", override=str(fake_zcode_db))
        is True
    )
    assert (
        zcode.session_exists("sess_nope", override=str(fake_zcode_db))
        is False
    )
    assert (
        zcode.session_exists("../escape", override=str(fake_zcode_db))
        is False
    )


def test_part_sequence_orders_same_ms_parts(fake_zcode_db: Path) -> None:
    """ reasoning/text/tool parts share one ms; ``sequence`` orders them."""
    messages = zcode.read_messages(
        "sess_test-zc-1", override=str(fake_zcode_db)
    )
    first = messages[1]
    # reasoning part (seq 0) fed ``thinking``, text part (seq 1) fed
    # ``text`` — both visible, neither dropped by ordering.
    assert first.thinking and first.text


def test_ask_user_question_surfaces_qa(fake_zcode_db: Path) -> None:
    """An AskUserQuestion part pairs into ``Message.qa`` (UX parity)."""
    messages = zcode.read_messages(
        "sess_test-zc-3-qa", override=str(fake_zcode_db)
    )
    assert len(messages) == 1
    msg = messages[0]
    assert msg.role == "assistant"
    # The combined call+result part still surfaces both halves.
    assert msg.tool_use[0]["name"] == "AskUserQuestion"
    assert msg.tool_result[0]["is_error"] is False
    assert '"Deploy now?"="yes"' in msg.tool_result[0]["content"]
    # …and the parsed question→answer pair lands on the message.
    assert msg.qa == (
        {"question": "Deploy now?", "options": ("yes", "no"),
         "answer": "yes"},
    )


def test_subagent_enrichment_from_agents_metadata(
    fake_zcode_db: Path,
) -> None:
    """profileId/parentToolUseId from metadata.json enrich the child."""
    child = zcode.read_session(
        "sess_test-zc-4-sub", override=str(fake_zcode_db)
    )
    assert child.parent_uuid == "sess_test-zc-1"
    assert child.kind == "subagent"
    assert child.extra["subagent_type"] == "explorer"
    assert child.extra["spawn_tool_use_id"] == "toolu_spawn_9"
    # Also visible through the listing (same enrichment path).
    listed = {
        s.uuid: s
        for s in zcode.list_sessions(override=str(fake_zcode_db))
    }
    assert listed["sess_test-zc-4-sub"].extra["subagent_type"] == "explorer"


# ---------------------------------------------------------------------------
# Bash → get_body bridge mapping
# ---------------------------------------------------------------------------


def test_bash_get_body_bridge_maps_parser_level(
    fake_zcode_db: Path,
) -> None:
    """A Bash call that is exactly ``ai-r get-body <id>`` surfaces as a
    ``get_body`` tool_use with ``input={"id": …}`` + ``tool_original``."""
    import json as _json

    messages = zcode.read_messages(
        "sess_test-zc-5-gb", override=str(fake_zcode_db)
    )
    by_call_id = {
        t["tool_use_id"]: t for t in messages[0].tool_use
    }
    mapped = by_call_id["call_zc_gb_1"]
    assert mapped["name"] == "get_body"
    assert _json.loads(mapped["input"]) == {"id": "sess_x:5"}
    assert mapped["tool_original"] == "Bash"
    # The ordinary ``ai-r read`` call keeps its Bash identity.
    plain = by_call_id["call_zc_gb_2"]
    assert plain["name"] == "Bash"
    assert "tool_original" not in plain


def test_find_tool_calls_sees_bash_get_body_bridge(
    fake_zcode_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge is machine-visible: find_tool_calls(get_body, zcode)
    finds the record with ``input.id`` (the audit checker's join)."""
    from ai_r.find_tool_calls import find_tool_calls
    from ai_r.parsers import _common

    _common._agent_sessions_cache.clear()
    _common._msg_cache.clear()
    monkeypatch.setenv("ZCODE_DB", str(fake_zcode_db))
    try:
        result = find_tool_calls(tool_name="get_body", agent="zcode")
        assert result["count"] == 1
        rec = result["records"][0]
        assert rec["input"] == {"id": "sess_x:5"}
    finally:
        _common._agent_sessions_cache.clear()
        _common._msg_cache.clear()


# ---------------------------------------------------------------------------
# Rollout model-io JSONL fallback (DB-unknown sessions)
# ---------------------------------------------------------------------------

_SUB_ID = "sess_subagent_agent_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PARENT_ID = "sess_11111111-2222-3333-4444-555555555555"


def test_rollout_session_discovered_with_parent(fake_zcode_rollout: Path) -> None:
    sessions = zcode.list_sessions()
    by_id = {s.uuid: s for s in sessions}
    assert _SUB_ID in by_id
    sub = by_id[_SUB_ID]
    assert sub.agent is AgentName.ZCODE
    assert sub.kind == "subagent"
    # Parent link resolved from agents/sess_*/agent_*/metadata.json.
    assert sub.parent_uuid == _PARENT_ID
    # Title = first user message of the earliest full snapshot.
    assert sub.title == "Scan the fixture tree for jsonl files"
    assert sub.path == str(fake_zcode_rollout)
    assert sub.models == ("GLM-5.3",)
    # Last completedAt of the file.
    assert sub.date == datetime.fromisoformat(
        "2026-09-23T04:26:01.000+00:00"
    )


def test_rollout_read_messages_replay(fake_zcode_rollout: Path) -> None:
    messages = zcode.read_messages(_SUB_ID)
    roles = [m.role for m in messages]
    # system skipped; full snapshot + last response appended.
    assert roles == ["user", "assistant", "tool", "assistant"]

    replayed = messages[1]
    assert replayed.thinking == "A glob is the cheapest first step."
    assert replayed.text == "Listing the tree."
    assert replayed.tool_use[0]["name"] == "Bash"
    assert replayed.tool_use[0]["tool_use_id"] == "call_zc_r1"

    tool_msg = messages[2]
    assert tool_msg.tool_result[0]["content"] == "2 files"
    assert tool_msg.tool_result[0]["is_error"] is False

    final = messages[3]
    assert final.text == "Done: two fixtures."
    assert final.thinking == "Nothing left to check."
    assert final.model == "GLM-5.3"


def test_rollout_token_usage(fake_zcode_rollout: Path) -> None:
    usage = zcode.read_token_usage(_SUB_ID)
    assert usage is not None
    assert usage["input"] == 600  # 100 + 200 + 300
    assert usage["output"] == 90  # 20 + 30 + 40
    assert usage["cache_read"] == 390
    assert usage["cache_write"] == 0
    # No per-call reasoning counter in the rollout usage block.
    assert usage["reasoning"] is None


def test_rollout_session_exists_and_missing(fake_zcode_rollout: Path) -> None:
    assert zcode.session_exists(_SUB_ID) is True
    assert zcode.session_exists("sess_not-anywhere") is False


def test_db_session_wins_over_rollout_duplicate(
    fake_zcode_db: Path, tmp_path: Path
) -> None:
    """A rollout file whose sessionId the DB already knows is skipped."""
    sid = "sess_test-zc-1"
    dup = tmp_path / f"model-io-{sid}.jsonl"
    # Point the rollout discovery at a dir containing only the duplicate.
    import json as _json

    dup.write_text(
        _json.dumps({
            "type": "model_io", "sessionId": sid,
            "completedAt": "2026-09-23T04:26:01.000Z",
            "request": {"messages": [], "messagesKind": "full",
                        "messageCount": 0},
            "response": {"text": "dup"},
        }) + "\n",
        encoding="utf-8",
    )
    from ai_r.parsers import zcode as zcode_mod

    original = zcode_mod._rollout_dir
    try:
        zcode_mod._rollout_dir = lambda: tmp_path  # type: ignore[assignment]
        sessions = zcode.list_sessions(override=str(fake_zcode_db))
    finally:
        zcode_mod._rollout_dir = original  # type: ignore[assignment]
    assert len([s for s in sessions if s.uuid == sid]) == 1


# ---------------------------------------------------------------------------
# Host-data integration (read-only; auto-tagged ``host``)
# ---------------------------------------------------------------------------


def test_list_sessions_real(real_zcode_db: Path) -> None:
    sessions = zcode.list_sessions(override=str(real_zcode_db))
    assert sessions, "expected at least one ZCode session on this host"
    for s in sessions[:3]:
        assert s.agent is AgentName.ZCODE
        assert s.title
        assert s.message_count >= 0
    ids = [s.uuid for s in sessions]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Plan signals (events layer, zcode mirrors the Claude shapes)
# ---------------------------------------------------------------------------


def test_zcode_plan_signals_exit_plan_mode() -> None:
    import json as _json

    from ai_r.events.model import _plan_signals_for_session

    messages = [
        Message(role="user", text="make a plan"),
        Message(
            role="assistant",
            text="",
            tool_use=(
                {
                    "name": "ExitPlanMode",
                    "input": _json.dumps(
                        {"plan": "# Plan: do the thing\n- step one"}
                    ),
                    "tool_use_id": "call_plan_1",
                },
            ),
        ),
    ]
    signals = _plan_signals_for_session(
        messages, agent="zcode", session_path=""
    )
    assert len(signals) == 1
    assert signals[0].agent_signal == "zcode:ExitPlanMode"
    assert signals[0].title == "Plan: do the thing"
    assert signals[0].tool_use_id == "call_plan_1"
    assert "step one" in (signals[0].body or "")


def test_zcode_plan_signals_plan_file_write() -> None:
    import json as _json

    from ai_r.events.model import _plan_signals_for_session

    messages = [
        Message(
            role="assistant",
            text="",
            tool_use=(
                {
                    "name": "Write",
                    "input": _json.dumps(
                        {
                            "file_path": "/w/.zcode/plans/plan-sess_x.md",
                            "content": "# Plan: file plan\nbody",
                        }
                    ),
                    "tool_use_id": "call_write_1",
                },
            ),
        ),
    ]
    signals = _plan_signals_for_session(
        messages, agent="zcode", session_path=""
    )
    assert len(signals) == 1
    assert signals[0].agent_signal == "zcode:Write(plans/*.md)"
    # task_key = the plans/<slug>.md slug (shared Claude logic).
    assert signals[0].task_key == "plans/plan-sess_x.md"
