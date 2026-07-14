"""Claude subagent cost sidecar — ``toolUseResult`` on a ``Task`` call.

When Claude Code spawns a subagent, the parent transcript records the outcome
in a record-level ``toolUseResult`` that carries what the child cost: which
model actually ran it (``resolvedModel`` — a subagent may be pinned to a
cheaper tier than its parent), the billed ``usage`` block, how long it took,
how many tools it used, and whether it succeeded.

ai-r used to drop all of it: the ``tool_result`` entry kept only
``{content, is_error, tool_use_id}``, so "which subagent burned the budget"
was unanswerable without hand-parsing JSONL. The parser now normalises that
sidecar onto the EXISTING ``tool_result`` entry (no second taxonomy — the call
is already classified ``tool_kind=task`` by ``resolve_tool``), and
``find_tool_calls`` surfaces it alongside ``tool_use_id``.

Two guards matter and are asserted below:

* ``toolUseResult`` is **record-level** while ``content`` may hold SEVERAL
  ``tool_result`` parts. With more than one part the sidecar cannot be
  attributed to a specific call, so it is dropped rather than guessed.
* Ordinary tools also carry ``toolUseResult`` (often a plain string). Only a
  subagent-shaped payload is lifted; everything else is left alone.

Hermetic: the autouse ``_isolate_ai_r_home`` fixture points parsers at a
per-test temp home; the fixture monkeypatches ``_resolve_base_dir`` at the
temp projects tree.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.find_tool_calls import find_tool_calls
from ai_r.parsers import claude
from ai_r.tokens import TOKEN_FIELDS


UUID = "subagent-cost-1"

USAGE = {
    "input_tokens": 12,
    "output_tokens": 3_400,
    "cache_creation_input_tokens": 40_000,
    "cache_read_input_tokens": 160_000,
}


def _task_call(
    tuid: str, subagent_type: str, ts: str, *, name: str = "Agent"
) -> dict:
    """A spawn call. Claude Code names the tool ``Agent`` in current
    transcripts and ``Task`` in older ones; ``resolve_tool`` classifies both
    as ``tool_kind=task``, and the sidecar attaches regardless of the name —
    both spellings appear in the seed so neither can silently regress."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tuid,
                    "name": name,
                    "input": {
                        "subagent_type": subagent_type,
                        "prompt": "find the thing",
                        "description": "scout",
                    },
                },
            ],
        },
        "timestamp": ts,
        "sessionId": UUID,
    }


def _task_result(tuid: str, ts: str, *, sidecar: dict) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tuid, "content": "done"},
            ],
        },
        "timestamp": ts,
        "sessionId": UUID,
        "toolUseResult": sidecar,
    }


def _write_session(tmp_sessions_dir: Path) -> None:
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "go"},
            "timestamp": "2026-07-14T10:00:00Z",
            "sessionId": UUID,
        },
        # (a) a Task whose subagent ran on a PINNED model cheaper than the parent
        _task_call("tu-task-haiku", "explorer", "2026-07-14T10:00:01Z"),
        _task_result(
            "tu-task-haiku",
            "2026-07-14T10:00:02Z",
            sidecar={
                "agentType": "explorer",
                "resolvedModel": "claude-haiku-4-5",
                "status": "completed",
                "totalTokens": 203_412,
                "totalDurationMs": 122_996,
                "totalToolUseCount": 15,
                "usage": USAGE,
                "content": [{"type": "text", "text": "the map"}],
            },
        ),
        # (b) a spawn that inherited the parent's 1M-window model, and FAILED.
        # Spelled with the legacy ``Task`` name to keep both spellings covered.
        _task_call("tu-task-opus", "auditor", "2026-07-14T10:00:03Z",
                   name="Task"),
        _task_result(
            "tu-task-opus",
            "2026-07-14T10:00:04Z",
            sidecar={
                "agentType": "auditor",
                "resolvedModel": "claude-opus-4-8[1m]",
                "status": "error",
                "totalTokens": 47_596,
                "totalDurationMs": 104_147,
                "totalToolUseCount": 4,
                "usage": {"input_tokens": 5, "output_tokens": 900},
            },
        ),
        # (c') a BACKGROUND spawn: the sidecar is written at LAUNCH, so it
        # names a model but has no usage yet — and, in real vaults, no
        # agentType either. The majority of spawns look like this.
        _task_call("tu-task-async", "researcher", "2026-07-14T10:00:09Z"),
        _task_result(
            "tu-task-async",
            "2026-07-14T10:00:10Z",
            sidecar={
                "resolvedModel": "claude-haiku-4-5",
                "status": "async_launched",
            },
        ),
        # (c'') a sidecar whose numbers are JSON ``true``. A bool IS an int in
        # Python, so an unguarded ``isinstance(x, int)`` would bill this spawn
        # 1 token / 1 ms / 1 tool use. Nothing here is a number → nothing is
        # reported.
        _task_call("tu-task-bool", "explorer", "2026-07-14T10:00:11Z"),
        _task_result(
            "tu-task-bool",
            "2026-07-14T10:00:12Z",
            sidecar={
                "agentType": "explorer",
                "resolvedModel": "claude-haiku-4-5",
                "status": "completed",
                "totalTokens": True,
                "totalDurationMs": True,
                "totalToolUseCount": True,
                "usage": {"input_tokens": True, "output_tokens": False},
            },
        ),
        # (c''') usage recorded, no ``totalTokens``: the documented fallback is
        # the sum of the components (the harness figure is preferred, not
        # required).
        _task_call("tu-task-nototal", "researcher", "2026-07-14T10:00:13Z"),
        _task_result(
            "tu-task-nototal",
            "2026-07-14T10:00:14Z",
            sidecar={
                "agentType": "researcher",
                "resolvedModel": "claude-haiku-4-5",
                "status": "completed",
                "usage": {"input_tokens": 7, "output_tokens": 11},
            },
        ),
        # (c) an ORDINARY tool — record-level toolUseResult is a plain string
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu-bash", "name": "Bash",
                     "input": {"command": "true"}},
                ],
            },
            "timestamp": "2026-07-14T10:00:05Z",
            "sessionId": UUID,
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu-bash",
                     "content": "ok-stdout"},
                ],
            },
            "timestamp": "2026-07-14T10:00:06Z",
            "sessionId": UUID,
            "toolUseResult": "ok-stdout",
        },
        # (d) AMBIGUOUS: two tool_result parts under ONE record-level sidecar
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu-amb-a", "name": "Task",
                     "input": {"subagent_type": "explorer", "prompt": "a"}},
                    {"type": "tool_use", "id": "tu-amb-b", "name": "Task",
                     "input": {"subagent_type": "researcher", "prompt": "b"}},
                ],
            },
            "timestamp": "2026-07-14T10:00:07Z",
            "sessionId": UUID,
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu-amb-a",
                     "content": "a done"},
                    {"type": "tool_result", "tool_use_id": "tu-amb-b",
                     "content": "b done"},
                ],
            },
            "timestamp": "2026-07-14T10:00:08Z",
            "sessionId": UUID,
            "toolUseResult": {
                "agentType": "explorer",
                "resolvedModel": "claude-haiku-4-5",
                "usage": USAGE,
            },
        },
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-sub" / f"{UUID}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def subagent_session(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    _write_session(tmp_sessions_dir)
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    return UUID


def _sidecars_by_id(uuid: str) -> dict[str, dict]:
    messages = claude.read_messages(uuid)
    out: dict[str, dict] = {}
    for msg in messages:
        for entry in msg.tool_result:
            if "subagent" in entry:
                out[entry["tool_use_id"]] = entry["subagent"]
    return out


# --- parser ---------------------------------------------------------------


def test_sidecar_normalised_onto_tool_result(subagent_session: str) -> None:
    """The cheap-tier Task keeps its real model and its EXACT billed tokens."""
    side = _sidecars_by_id(subagent_session)["tu-task-haiku"]

    assert side["agent_type"] == "explorer"
    assert side["model"] == "claude-haiku-4-5"
    assert side["status"] == "completed"
    assert side["duration_ms"] == 122_996
    assert side["tool_uses"] == 15

    # usage is normalised to ai_r.tokens field names, not Claude's wire names,
    # and carries the full block shape (reasoning is not recorded for a spawn)
    assert side["tokens"] == {
        "input": 12,
        "output": 3_400,
        "reasoning": None,
        "cache_write": 40_000,
        "cache_read": 160_000,
        "total": 203_412,
        "source": "exact",
    }


def test_sidecar_keeps_parent_inherited_model_and_failure(
    subagent_session: str,
) -> None:
    """A subagent that inherited the parent's model is reported as such —
    that is the signal an unpinned persona is burning the expensive tier."""
    side = _sidecars_by_id(subagent_session)["tu-task-opus"]

    assert side["agent_type"] == "auditor"
    assert side["model"] == "claude-opus-4-8[1m]"
    assert side["status"] == "error"
    # a partial usage block must still total honestly, not crash
    assert side["tokens"]["input"] == 5
    assert side["tokens"]["output"] == 900
    assert side["tokens"]["source"] == "exact"


def test_total_comes_from_the_harness_not_a_local_sum(
    subagent_session: str,
) -> None:
    """``total`` is the harness's own ``totalTokens`` — the number it bills.

    Seed (b) makes the two disagree on purpose (usage sums to 905, the harness
    reports 47_596). Without this test, replacing ``totalTokens`` with
    ``sum(components)`` would leave the suite green while silently changing
    what every cost report means.
    """
    side = _sidecars_by_id(subagent_session)["tu-task-opus"]
    assert side["tokens"]["total"] == 47_596
    assert side["tokens"]["total"] != 5 + 900


def test_token_block_carries_the_full_shape(subagent_session: str) -> None:
    """A consumer indexes the block like any other token block.

    Seed (b) has a PARTIAL usage (no cache fields). Those keys must still be
    present and ``None`` — absent keys would make ``block["cache_read"]``
    raise KeyError on exactly the sessions a cost audit cares about.
    """
    side = _sidecars_by_id(subagent_session)["tu-task-opus"]
    assert set(side["tokens"]) == set(TOKEN_FIELDS) | {"source"}
    assert side["tokens"]["cache_read"] is None
    assert side["tokens"]["reasoning"] is None


def test_subagent_token_block_matches_tokens_ssot() -> None:
    """The parser restates TOKEN_FIELDS (importing ai_r.tokens there would
    close an import cycle). Guard the restatement against drift."""
    assert claude._TOKEN_FIELDS == TOKEN_FIELDS


def test_background_spawn_reports_model_but_no_fabricated_tokens(
    subagent_session: str,
) -> None:
    """A background spawn has no usage at launch. Absence must stay absence —
    a zero here would read as "this subagent was free"."""
    side = _sidecars_by_id(subagent_session)["tu-task-async"]

    assert side["model"] == "claude-haiku-4-5"
    assert side["status"] == "async_launched"
    assert "tokens" not in side, "no usage recorded yet — do not invent a zero"
    # Its real cost is recovered from the child's own transcript by
    # read_session(include_subagents=True) — see the rollup, and SUB-2.


def test_json_true_is_not_a_token_count(subagent_session: str) -> None:
    """``True`` is an ``int`` in Python — a bool must not become a number.

    Seed (c'') sets every numeric field to a JSON bool. An unguarded
    ``isinstance(x, int)`` would report 1 token, 1 ms, 1 tool use: a
    fabricated cost that looks like a real (cheap) run.
    """
    side = _sidecars_by_id(subagent_session)["tu-task-bool"]

    assert side["model"] == "claude-haiku-4-5"  # the string fields still land
    assert "tokens" not in side, "a bool usage block is no usage at all"
    assert "duration_ms" not in side
    assert "tool_uses" not in side


def test_total_falls_back_to_the_component_sum(subagent_session: str) -> None:
    """No ``totalTokens`` in the sidecar → the sum of the components.

    The harness figure is PREFERRED (see the test above), not required: a
    sidecar that records usage without a total must still price the spawn,
    and the fallback is the documented sum — not ``None``, not a drop.
    """
    side = _sidecars_by_id(subagent_session)["tu-task-nototal"]
    assert side["tokens"]["total"] == 7 + 11
    assert side["tokens"]["source"] == "exact"


def test_ordinary_tool_gets_no_sidecar(subagent_session: str) -> None:
    """A plain-string toolUseResult is not a subagent payload — leave it alone."""
    assert "tu-bash" not in _sidecars_by_id(subagent_session)


def test_ambiguous_record_drops_the_sidecar(subagent_session: str) -> None:
    """One record-level sidecar, two tool_result parts: attribution is a guess.
    Fail closed — report nothing rather than bill the wrong subagent."""
    side = _sidecars_by_id(subagent_session)
    assert "tu-amb-a" not in side
    assert "tu-amb-b" not in side


# --- find_tool_calls ------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
def test_find_tool_calls_surfaces_id_and_subagent(
    subagent_session: str, tool_name: str
) -> None:
    """The join key (tool_use_id) and the cost sidecar reach the public verb,
    so "which subagent type on which model cost what" is one call — under
    either spelling of the spawn tool."""
    calls = find_tool_calls(
        session=subagent_session, tool_name=tool_name
    )["records"]
    by_id = {c["tool_use_id"]: c for c in calls if c.get("tool_use_id")}

    assert by_id, f"{tool_name} calls must expose tool_use_id"
    for call in by_id.values():
        assert call["tool_kind"] == "task"

    # Every unambiguous spawn carries exact cost; the ambiguous pair (seeded
    # under the ``Task`` spelling) carries none — see the parser test above.
    priced = {i: c for i, c in by_id.items() if "subagent" in c}
    assert priced, f"{tool_name} spawns must carry the cost sidecar"
    for call in priced.values():
        side = call["subagent"]
        assert side["model"]
        # A background spawn has no usage yet — model without tokens is the
        # honest shape there; where tokens ARE reported they are exact.
        if "tokens" in side:
            assert side["tokens"]["source"] == "exact"
    assert not any(i.startswith("tu-amb-") for i in priced)

    if tool_name == "Agent":
        call = by_id["tu-task-haiku"]
        assert call["subagent"]["agent_type"] == "explorer"
        assert call["subagent"]["model"] == "claude-haiku-4-5"
        assert call["subagent"]["tokens"]["total"] == 203_412


# --- read_session(include_subagents=True) rollup ---------------------------
#
# The parent-side sidecar is NOT the source of truth for a spawn's cost: the
# majority of real spawns are backgrounded, and their sidecar is written at
# launch (``status: async_launched``) — before any usage or persona exists.
# The rollup therefore prices each child from the child's OWN transcript and
# names it from the child's OWN ``agent-*.meta.json``, keeping the parent
# sidecar as a fallback.  These tests pin that direction.

ROLL_PARENT = "roll-parent-1"
CHILD_BG = "agent-roll-bg"
CHILD_FG = "agent-roll-fg"
CHILD_CONFLICT = "agent-roll-conflict"


def _child_records(uuid: str, model: str, usage: dict) -> list[dict]:
    return [
        {
            "type": "user",
            "message": {"role": "user", "content": "do the subtask"},
            "timestamp": "2026-07-14T11:00:00Z",
            "sessionId": uuid,
            "isSidechain": True,
            "parentUuid": ROLL_PARENT,
        },
        {
            "type": "assistant",
            "message": {
                "id": f"m-{uuid}",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "subtask done"}],
                "usage": usage,
            },
            "timestamp": "2026-07-14T11:00:05Z",
            "sessionId": uuid,
            "isSidechain": True,
            "parentUuid": ROLL_PARENT,
        },
    ]


@pytest.fixture
def rollup_session(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """A parent that spawned two children: one BACKGROUND, one completed.

    The two differ in exactly the ways that make the join load-bearing:

    * the background child's parent-side sidecar has NO usage and NO
      ``agentType`` (that is what ``async_launched`` looks like on disk), but
      its own meta names the persona and its own transcript carries the
      billed usage;
    * the completed child's meta happens to carry no ``agentType`` (an older
      meta), so its persona must come from the parent sidecar — the fallback.

    Their statuses also differ, so a rollup that joined children to sidecars
    by position instead of by ``tool_use_id`` would swap them and go red.
    """
    from ai_r.parsers import PARSERS, AgentName

    proj = tmp_sessions_dir / ".claude" / "projects" / "proj-roll"
    parent_records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "spawn two agents"},
            "timestamp": "2026-07-14T10:59:00Z",
            "sessionId": ROLL_PARENT,
        },
        _task_call("tu-roll-bg", "explorer", "2026-07-14T10:59:01Z"),
        _task_result(
            "tu-roll-bg",
            "2026-07-14T10:59:02Z",
            # Written at LAUNCH: a model, a status, nothing else.
            sidecar={
                "resolvedModel": "claude-haiku-4-5",
                "status": "async_launched",
            },
        ),
        _task_call("tu-roll-fg", "auditor", "2026-07-14T10:59:03Z"),
        _task_result(
            "tu-roll-fg",
            "2026-07-14T10:59:04Z",
            sidecar={
                "agentType": "auditor",
                "resolvedModel": "claude-opus-4-8[1m]",
                "status": "completed",
                "totalTokens": 999_999,
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        ),
        # The two persona sources DISAGREE: the child's own meta is the source
        # of truth, the spawning call's sidecar is only the fallback.  Without
        # a case where the two differ, either precedence would pass.
        _task_call("tu-roll-conflict", "explorer", "2026-07-14T10:59:05Z"),
        _task_result(
            "tu-roll-conflict",
            "2026-07-14T10:59:06Z",
            sidecar={
                "agentType": "persona-from-parent",
                "resolvedModel": "claude-haiku-4-5",
                "status": "completed",
                "totalTokens": 30,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        ),
    ]
    for record in parent_records:
        record["sessionId"] = ROLL_PARENT
    path = proj / f"{ROLL_PARENT}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in parent_records)
        + "\n",
        encoding="utf-8",
    )

    subagents = proj / ROLL_PARENT / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    children = {
        CHILD_BG: (
            {"toolUseId": "tu-roll-bg", "spawnDepth": 1,
             "agentType": "explorer"},
            "claude-haiku-4-5",
            {"input_tokens": 11, "output_tokens": 22,
             "cache_creation_input_tokens": 33,
             "cache_read_input_tokens": 44},
        ),
        # No ``agentType`` in the meta → the parent sidecar is the fallback.
        CHILD_FG: (
            {"toolUseId": "tu-roll-fg", "spawnDepth": 1},
            "claude-opus-4-8[1m]",
            {"input_tokens": 5, "output_tokens": 6},
        ),
        # Both sources name a persona, and they disagree — the child wins.
        CHILD_CONFLICT: (
            {"toolUseId": "tu-roll-conflict", "spawnDepth": 1,
             "agentType": "persona-from-child"},
            "claude-haiku-4-5",
            {"input_tokens": 3, "output_tokens": 4},
        ),
    }
    for uuid, (meta, model, usage) in children.items():
        (subagents / f"{uuid}.jsonl").write_text(
            "\n".join(
                json.dumps(r, ensure_ascii=False)
                for r in _child_records(uuid, model, usage)
            )
            + "\n",
            encoding="utf-8",
        )
        (subagents / f"{uuid}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )

    # Pin the unscoped child lookup to the hermetic Claude tree: other parsers
    # can still reach real host data through their own discovery.
    for agent_name, parser in PARSERS.items():
        if agent_name is not AgentName.CLAUDE:
            monkeypatch.setattr(parser, "list_sessions", lambda *a, **k: [])
    return ROLL_PARENT


def _children_by_uuid(parent: str) -> dict[str, dict]:
    from ai_r.mcp_server import read_session

    result = read_session(parent, agent="claude", include_subagents=True)
    assert "error" not in result
    return {c["uuid"]: c for c in result["subagent_rollup"]["children"]}


def test_rollup_prices_a_background_child_from_its_own_transcript(
    rollup_session: str,
) -> None:
    """The background child's cost exists only in the child's transcript.

    Its parent-side sidecar carries no usage at all — reading the price from
    there would report nothing (or, worse, a zero) for the majority of real
    spawns.
    """
    child = _children_by_uuid(rollup_session)[CHILD_BG]

    assert child["status"] == "async_launched"
    assert child["tokens"]["source"] == "exact"
    assert child["tokens"]["total"] == 11 + 22 + 33 + 44
    assert child["models"] == ["claude-haiku-4-5"]


def test_rollup_names_a_background_child_from_its_own_meta(
    rollup_session: str,
) -> None:
    """Persona from the CHILD's meta — the background sidecar names none."""
    child = _children_by_uuid(rollup_session)[CHILD_BG]
    assert child["subagent_type"] == "explorer"


def test_rollup_persona_prefers_the_child_over_the_parent_sidecar(
    rollup_session: str,
) -> None:
    """When BOTH name a persona and they disagree, the child's meta wins.

    The direction is the whole point of the design (a background spawn's
    sidecar names no persona at all, so a parent-first rollup leaves the
    majority of children anonymous) — and only a conflicting seed can pin it:
    with agreeing sources, either precedence would pass.
    """
    child = _children_by_uuid(rollup_session)[CHILD_CONFLICT]
    assert child["subagent_type"] == "persona-from-child"
    assert child["subagent_type"] != "persona-from-parent"


def test_rollup_falls_back_to_the_parent_sidecar_persona(
    rollup_session: str,
) -> None:
    """A child whose meta carries no persona is still named — from the sidecar
    of the call that spawned it (the fallback direction of the same join)."""
    child = _children_by_uuid(rollup_session)[CHILD_FG]
    assert child["subagent_type"] == "auditor"
    assert child["status"] == "completed"


def test_rollup_child_tokens_come_from_the_child_not_the_sidecar(
    rollup_session: str,
) -> None:
    """One source for every child, background or not: its own transcript.

    The completed child's sidecar claims 999_999 tokens; its transcript bills
    11. The rollup reports the transcript figure — mixing the two sources per
    child would make the children incomparable.
    """
    child = _children_by_uuid(rollup_session)[CHILD_FG]
    assert child["tokens"]["total"] == 5 + 6
    assert child["tokens"]["total"] != 999_999


def test_rollup_joins_each_child_to_its_own_spawn_call(
    rollup_session: str,
) -> None:
    """The join is by ``tool_use_id``, not by position.

    Billing a sidecar to the wrong child is the exact failure the record-level
    ambiguity guard exists to prevent; here two children with two different
    statuses prove the join key is honoured.
    """
    children = _children_by_uuid(rollup_session)
    assert {u: c["status"] for u, c in children.items()} == {
        CHILD_BG: "async_launched",
        CHILD_FG: "completed",
        CHILD_CONFLICT: "completed",
    }


# --- find_tool_calls(with_subagent_cost=True) -------------------------------
#
# The rollup above answers "what did this session's children cost" for ONE
# parent.  A cost audit asks the cross-session question ("which persona on
# which model burned the budget this week"), and that is ``find_tool_calls``
# territory.  Without the flag the verb reports the parent's sidecar verbatim —
# which, for a BACKGROUND spawn, names neither the persona nor the tokens.  The
# flag runs the SAME child join the rollup uses (SSOT ``ai_r.subagents``), and
# is opt-in because it reads one small file per spawn.


def _spawns(parent: str, **kwargs: object) -> dict[str, dict]:
    records = find_tool_calls(
        session=parent, tool_name="Agent", **kwargs  # type: ignore[arg-type]
    )["records"]
    return {r["tool_use_id"]: r for r in records if r.get("tool_use_id")}


def test_default_leaves_the_background_spawn_unjoined(
    rollup_session: str,
) -> None:
    """Default (no flag) = the parent's sidecar verbatim — unchanged.

    This is the shape the cross-corpus callers already pay for: no per-spawn
    disk read, and therefore no persona and no cost for a background spawn.
    """
    side = _spawns(rollup_session)["tu-roll-bg"]["subagent"]

    assert side["status"] == "async_launched"
    assert "agent_type" not in side, "the launch-time sidecar names no persona"
    assert "tokens" not in side, "no usage recorded yet — do not invent a zero"
    assert "child_uuid" not in side, "no join ran"


def test_with_subagent_cost_prices_a_background_spawn(
    rollup_session: str,
) -> None:
    """With the flag, a background spawn is named and priced from the CHILD.

    This is the whole point: in a real vault most spawns are backgrounded, so a
    per-persona cost table built on the parent sidecar alone is ~empty.
    """
    side = _spawns(rollup_session, with_subagent_cost=True)["tu-roll-bg"][
        "subagent"
    ]

    assert side["agent_type"] == "explorer"       # child's agent-*.meta.json
    assert side["child_uuid"] == CHILD_BG         # provenance of the join
    assert side["models"] == ["claude-haiku-4-5"]
    assert side["tokens"]["source"] == "exact"    # billing tier only
    assert side["tokens"]["total"] == 11 + 22 + 33 + 44
    assert side["status"] == "async_launched"     # parent-side signal survives


def test_with_subagent_cost_prefers_the_child_over_the_sidecar(
    rollup_session: str,
) -> None:
    """Child > parent sidecar on a conflict — for the persona AND the tokens.

    The seed makes both disagree on purpose (sidecar: ``persona-from-parent``,
    30 tokens; child: ``persona-from-child``, 7). With agreeing sources either
    precedence would pass.
    """
    side = _spawns(rollup_session, with_subagent_cost=True)[
        "tu-roll-conflict"
    ]["subagent"]

    assert side["agent_type"] == "persona-from-child"
    assert side["agent_type"] != "persona-from-parent"
    assert side["tokens"]["total"] == 3 + 4
    assert side["tokens"]["total"] != 30


def test_with_subagent_cost_keeps_the_sidecar_persona_as_fallback(
    rollup_session: str,
) -> None:
    """A child whose meta names no persona is still named — from the sidecar."""
    side = _spawns(rollup_session, with_subagent_cost=True)["tu-roll-fg"][
        "subagent"
    ]

    assert side["agent_type"] == "auditor"     # only the sidecar knows
    assert side["tokens"]["total"] == 5 + 6    # …but the cost is the child's
    assert side["tokens"]["total"] != 999_999


def test_with_subagent_cost_leaves_ordinary_calls_alone(
    subagent_session: str,
) -> None:
    """The join only fires on a spawn: a Bash call gains nothing."""
    records = find_tool_calls(
        session=subagent_session, tool_name="Bash", with_subagent_cost=True
    )["records"]

    assert records
    for record in records:
        assert "subagent" not in record


def test_with_subagent_cost_rejects_a_non_bool() -> None:
    with pytest.raises(ValueError, match="with_subagent_cost"):
        find_tool_calls(tool_name="Agent", with_subagent_cost="yes")  # type: ignore[arg-type]


# --- honest gaps: the child is not (yet) readable ---------------------------

GAP_PARENT = "gap-parent-1"
CHILD_BROKEN = "agent-gap-broken"


@pytest.fixture
def gap_session(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """A parent whose children cannot be joined. Three gaps, three shapes:

    * ``tu-gap-missing``  — the child is not on disk at all (a background spawn
      still starting up, or one whose transcript was pruned);
    * ``tu-gap-broken``   — the child IS on disk but its ``agent-*.meta.json``
      is corrupt, so it carries no join key;
    * ``tu-gap-fallback`` — the child is not on disk either, but the spawn
      COMPLETED, so the parent sidecar's own exact block stands as the
      fallback.

    The first two must report NO tokens (absence — "not measured" is not
    "free") and must not raise; the third must keep the sidecar's numbers.
    """
    from ai_r.parsers import PARSERS, AgentName

    proj = tmp_sessions_dir / ".claude" / "projects" / "proj-gap"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "spawn"},
            "timestamp": "2026-07-14T12:00:00Z",
            "sessionId": GAP_PARENT,
        },
        _task_call("tu-gap-missing", "explorer", "2026-07-14T12:00:01Z"),
        _task_result(
            "tu-gap-missing",
            "2026-07-14T12:00:02Z",
            sidecar={
                "resolvedModel": "claude-haiku-4-5",
                "status": "async_launched",
            },
        ),
        _task_call("tu-gap-broken", "explorer", "2026-07-14T12:00:03Z"),
        _task_result(
            "tu-gap-broken",
            "2026-07-14T12:00:04Z",
            sidecar={
                "resolvedModel": "claude-haiku-4-5",
                "status": "async_launched",
            },
        ),
        _task_call("tu-gap-fallback", "auditor", "2026-07-14T12:00:05Z"),
        _task_result(
            "tu-gap-fallback",
            "2026-07-14T12:00:06Z",
            sidecar={
                "agentType": "auditor",
                "resolvedModel": "claude-opus-4-8[1m]",
                "status": "completed",
                "totalTokens": 4_242,
                "usage": {"input_tokens": 40, "output_tokens": 2},
            },
        ),
    ]
    for record in records:
        record["sessionId"] = GAP_PARENT
    path = proj / f"{GAP_PARENT}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )

    subagents = proj / GAP_PARENT / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    (subagents / f"{CHILD_BROKEN}.jsonl").write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False)
            for r in _child_records(
                CHILD_BROKEN, "claude-haiku-4-5", {"input_tokens": 9}
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (subagents / f"{CHILD_BROKEN}.meta.json").write_text(
        '{"toolUseId": "tu-gap-brok', encoding="utf-8"  # truncated mid-write
    )

    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    for agent_name, parser in PARSERS.items():
        if agent_name is not AgentName.CLAUDE:
            monkeypatch.setattr(parser, "list_sessions", lambda *a, **k: [])
    return GAP_PARENT


def test_missing_child_reports_no_tokens_not_a_zero(gap_session: str) -> None:
    """The child never landed on disk: nothing to bill, and nothing invented."""
    side = _spawns(gap_session, with_subagent_cost=True)["tu-gap-missing"][
        "subagent"
    ]

    assert side["status"] == "async_launched"
    assert "tokens" not in side, "not measured is not free"
    assert "child_uuid" not in side, "no child was joined — say so by absence"


def test_corrupt_child_meta_reports_no_tokens_and_does_not_raise(
    gap_session: str,
) -> None:
    """A half-written ``agent-*.meta.json`` carries no join key.

    The child's transcript exists, but nothing links it to THIS spawn — guessing
    (e.g. by position, or by the only child in the folder) would bill a real
    cost to a possibly wrong call. Absence, again.
    """
    side = _spawns(gap_session, with_subagent_cost=True)["tu-gap-broken"][
        "subagent"
    ]

    assert "tokens" not in side
    assert "child_uuid" not in side


def test_unjoinable_child_keeps_the_sidecar_cost_as_fallback(
    gap_session: str,
) -> None:
    """The parent sidecar stays the FALLBACK: a completed spawn keeps its
    exact numbers even when its child transcript cannot be read."""
    side = _spawns(gap_session, with_subagent_cost=True)["tu-gap-fallback"][
        "subagent"
    ]

    assert side["agent_type"] == "auditor"
    assert side["tokens"]["total"] == 4_242
    assert side["tokens"]["source"] == "exact"


# --- CLI -------------------------------------------------------------------


def test_cli_with_subagent_cost_flag(
    rollup_session: str, monkeypatch: pytest.MonkeyPatch, tmp_sessions_dir: Path
) -> None:
    """``ai-r find-tool-calls --with-subagent-cost`` runs the same join."""
    import contextlib
    import io

    from ai_r import cli as cli_module

    monkeypatch.setenv("AI_R_HOME", str(tmp_sessions_dir))
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = cli_module.main([
            "find-tool-calls", "Agent", "--agent", "claude",
            "--session", rollup_session, "--with-subagent-cost", "--json",
        ])
    assert rc == 0
    payload = json.loads(stdout.getvalue())
    by_id = {r["tool_use_id"]: r for r in payload["records"]}

    side = by_id["tu-roll-bg"]["subagent"]
    assert side["agent_type"] == "explorer"
    assert side["tokens"]["total"] == 11 + 22 + 33 + 44
    assert side["tokens"]["source"] == "exact"


# --- honest source ladder: exact is NOT guaranteed per child ----------------
#
# A child transcript may record no ``usage`` at all (a truncated or
# reference-only run). ``session_tokens`` then falls to a labeled ``estimate``,
# NOT ``exact`` — and certainly not a fabricated zero. The rollup must surface
# that honest label. Claiming a blanket ``exact`` (the pre-fix docs) or zeroing
# an unmeasured child would both be a lie for a tool that sells honest cost.

NOUSAGE_PARENT = "nousage-parent-1"
CHILD_NOUSAGE = "agent-nousage"


def _child_records_no_usage(uuid: str, parent: str) -> list[dict]:
    """A child whose assistant turn carries text but NO ``usage`` block."""
    return [
        {
            "type": "user",
            "message": {"role": "user", "content": "do the subtask"},
            "timestamp": "2026-07-14T13:00:00Z",
            "sessionId": uuid,
            "isSidechain": True,
            "parentUuid": parent,
        },
        {
            "type": "assistant",
            "message": {
                "id": f"m-{uuid}",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "some words here"}],
            },
            "timestamp": "2026-07-14T13:00:05Z",
            "sessionId": uuid,
            "isSidechain": True,
            "parentUuid": parent,
        },
    ]


@pytest.fixture
def nousage_rollup_session(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    from ai_r.parsers import PARSERS, AgentName

    proj = tmp_sessions_dir / ".claude" / "projects" / "proj-nousage"
    parent_records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "spawn one"},
            "timestamp": "2026-07-14T12:59:00Z",
            "sessionId": NOUSAGE_PARENT,
        },
        _task_call("tu-nousage", "explorer", "2026-07-14T12:59:01Z"),
        _task_result(
            "tu-nousage",
            "2026-07-14T12:59:02Z",
            sidecar={
                "resolvedModel": "claude-haiku-4-5",
                "status": "async_launched",
            },
        ),
    ]
    for record in parent_records:
        record["sessionId"] = NOUSAGE_PARENT
    path = proj / f"{NOUSAGE_PARENT}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in parent_records)
        + "\n",
        encoding="utf-8",
    )

    subagents = proj / NOUSAGE_PARENT / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    (subagents / f"{CHILD_NOUSAGE}.jsonl").write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False)
            for r in _child_records_no_usage(CHILD_NOUSAGE, NOUSAGE_PARENT)
        )
        + "\n",
        encoding="utf-8",
    )
    (subagents / f"{CHILD_NOUSAGE}.meta.json").write_text(
        json.dumps(
            {"toolUseId": "tu-nousage", "spawnDepth": 1,
             "agentType": "explorer"}
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    for agent_name, parser in PARSERS.items():
        if agent_name is not AgentName.CLAUDE:
            monkeypatch.setattr(parser, "list_sessions", lambda *a, **k: [])
    return NOUSAGE_PARENT


def test_rollup_child_without_usage_is_not_stamped_exact(
    nousage_rollup_session: str,
) -> None:
    """A usage-less child transcript → an HONEST label, never a blanket exact.

    Mutation guard: forcing the child block to ``source="exact"`` (the pre-fix
    docs' claim) turns this red. So does zeroing an unmeasured child.
    """
    child = _children_by_uuid(nousage_rollup_session)[CHILD_NOUSAGE]

    assert child["tokens"]["source"] != "exact", (
        "no usage recorded — reporting exact would be a lie"
    )
    # It is measured HONESTLY (a labeled estimate over the transcript volume),
    # not zeroed and not dropped.
    assert child["tokens"]["source"] in ("estimate", None)
    assert child["tokens"].get("total") != 0


def test_find_tool_calls_does_not_lift_a_non_exact_child_block(
    nousage_rollup_session: str,
) -> None:
    """``with_subagent_cost`` lifts a child's tokens ONLY when they are exact.

    The usage-less child yields an estimate; it must NOT land in the billing
    field. The parent sidecar here had no tokens either, so absence stands.
    """
    records = find_tool_calls(
        session=nousage_rollup_session,
        tool_name="Agent",
        with_subagent_cost=True,
    )["records"]
    side = {r["tool_use_id"]: r for r in records}["tu-nousage"]["subagent"]

    assert side["agent_type"] == "explorer"   # persona still joins
    assert side["child_uuid"] == CHILD_NOUSAGE
    assert "tokens" not in side, "an estimate must not enter the billing field"
