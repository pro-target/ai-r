"""F3.1 — wrapper-aware tool classification (``tool_kind`` / ``tool_resolved``).

Covers:

* :func:`ai_r.events.resolve_tool` — the pure classifier (unit,
  parametrized over every wrapper signal + honest no-signal fallbacks);
* ``iter_events`` refs — every ``tool_call`` event carries ``tool_kind``,
  wrappers with a name signal additionally carry ``tool_resolved``, and the
  event ``type`` stays the classify_tool subtype (backward-compat);
* the ``query`` ``tool_kind`` facet (exact, fail-loud on unknown values) and
  the widened ``tool`` facet (matches resolved names too);
* dict hoisting (``aggregate(group_by="tool_kind")`` works on query rows);
* Codex ``spawn_agent`` resolution (JSON-string arguments);
* ``find_tool_calls`` records carrying both new fields;
* the MCP ``query`` wrapper passing ``tool_kind`` through;
* emission-time redaction of ``tool_resolved``.

All hermetic — fixtures write fake session trees under ``AI_R_HOME``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.events import (
    TOOL_KIND,
    aggregate,
    iter_events,
    query,
    resolve_tool,
)
from ai_r.find_tool_calls import find_tool_calls
from ai_r.mcp_server import query as mcp_query


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# resolve_tool — pure classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,payload,expected_kind,expected_resolved",
    [
        # MCP wrappers (Claude-style naming): server:tool split on the
        # FIRST ``__`` after the prefix — server names with single
        # underscores survive, tool names may contain ``__``.
        ("mcp__ai-r__query", None, "mcp", "ai-r:query"),
        ("mcp__ccd_session__mark_chapter", None, "mcp",
         "ccd_session:mark_chapter"),
        ("mcp__srv__tool__extra", None, "mcp", "srv:tool__extra"),
        # Subagent spawns: Claude Task/Agent, OpenCode task, Codex
        # spawn_agent — resolved from the input's type key.
        ("Task", {"subagent_type": "Explore", "prompt": "x"},
         "task", "Explore"),
        ("Agent", {"subagent_type": "general-purpose"},
         "task", "general-purpose"),
        ("task", {"subagent_type": "cavecrew-builder"},
         "task", "cavecrew-builder"),
        ("spawn_agent", {"agent_type": "explorer", "message": "go"},
         "task", "explorer"),
        # No name signal in the input → kind known, resolved honest None.
        ("Task", {"prompt": "just a prompt"}, "task", None),
        ("spawn_agent", None, "task", None),
        # Skills: Claude Skill (skill), OpenCode skill (name),
        # SlashCommand (command "/cmd args" → bare token).
        ("Skill", {"skill": "ai-local-reader"}, "skill", "ai-local-reader"),
        ("skill", {"name": "orchestrator"}, "skill", "orchestrator"),
        ("SlashCommand", {"command": "/commit -m msg"}, "skill", "commit"),
        ("Skill", {}, "skill", None),
        # Web tools (name-based groundwork for the web-audit phase).
        ("WebFetch", {"url": "https://example.com"}, "web", None),
        ("websearch", None, "web", None),
        ("webfetch", None, "web", None),
        # Base categories fall through to classify_tool, never resolved.
        ("Bash", {"command": "ls"}, "bash", None),
        ("Edit", {"file_path": "/a.py"}, "edit", None),
        ("SomethingWeird", None, "other", None),
        ("", None, "other", None),
    ],
)
def test_resolve_tool(
    name: str,
    payload: object,
    expected_kind: str,
    expected_resolved: object,
) -> None:
    kind, resolved = resolve_tool(name, payload)
    assert kind == expected_kind
    assert resolved == expected_resolved
    assert kind in TOOL_KIND


def test_tool_kind_vocabulary_is_superset_of_subtypes() -> None:
    from ai_r.events import TOOL_SUBTYPE

    assert TOOL_SUBTYPE <= TOOL_KIND
    assert {"task", "skill", "mcp", "web"} <= TOOL_KIND


# ---------------------------------------------------------------------------
# Fixtures — fake sessions with wrapper calls
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_wrappers(tmp_sessions_dir: Path) -> str:
    """Claude session with one call of each wrapper flavour + a plain Bash."""
    session_id = "wrappers-claude-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-w"
        / f"{session_id}.jsonl"
    )

    def _asst(idx: int, name: str, tool_input: dict) -> dict:
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": f"tu_{idx}", "name": name,
                     "input": tool_input},
                ],
            },
            "timestamp": f"2026-06-14T10:00:0{idx}Z",
            "sessionId": session_id,
        }

    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "do the thing"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            _asst(1, "Task",
                  {"subagent_type": "Explore", "prompt": "look around"}),
            _asst(2, "Skill", {"skill": "ai-local-reader", "args": ""}),
            _asst(3, "SlashCommand", {"command": "/commit -m fix"}),
            _asst(4, "mcp__ai-r__query", {"type": "user_turn"}),
            _asst(5, "WebFetch", {"url": "https://example.com"}),
            _asst(6, "Bash", {"command": "echo hi"}),
            # A wrapper WITHOUT the name signal — resolved must be absent.
            _asst(7, "Task", {"prompt": "anonymous spawn"}),
        ],
    )
    return session_id


@pytest.fixture
def codex_spawn(tmp_sessions_dir: Path) -> str:
    """Codex rollout with a ``spawn_agent`` call (JSON-string arguments)."""
    uuid = "wrappers-codex-1"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T10-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": uuid,
                    "cwd": "/tmp/work",
                    "timestamp": "2026-06-14T10:00:00Z",
                },
            },
            {
                "timestamp": "2026-06-14T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "arguments": json.dumps(
                        {"agent_type": "explorer", "message": "scan repo"}
                    ),
                },
            },
        ],
    )
    return uuid


# ---------------------------------------------------------------------------
# iter_events refs
# ---------------------------------------------------------------------------


def _ref(event, key):
    vals = [r[key] for r in event.refs if key in r]
    return vals[0] if vals else None


def test_every_tool_call_event_carries_tool_kind(claude_wrappers: str) -> None:
    events = [
        e for e in iter_events("claude", session=claude_wrappers)
        if e.type.startswith("tool_call(")
    ]
    assert len(events) == 7
    assert all(_ref(e, "tool_kind") in TOOL_KIND for e in events)


def test_wrapper_events_carry_resolved_names(claude_wrappers: str) -> None:
    events = list(iter_events("claude", session=claude_wrappers))
    by_tool: dict = {}
    for e in events:
        name = _ref(e, "tool")
        if name and name not in by_tool:  # first occurrence wins
            by_tool[name] = e

    task = by_tool["Task"]
    assert _ref(task, "tool_kind") == "task"
    assert _ref(task, "tool_resolved") == "Explore"
    # Backward-compat: the event ``type`` keeps the classify_tool subtype.
    assert task.type == "tool_call(other)"

    skill = by_tool["Skill"]
    assert _ref(skill, "tool_kind") == "skill"
    assert _ref(skill, "tool_resolved") == "ai-local-reader"

    slash = by_tool["SlashCommand"]
    assert _ref(slash, "tool_kind") == "skill"
    assert _ref(slash, "tool_resolved") == "commit"

    mcp_call = by_tool["mcp__ai-r__query"]
    assert _ref(mcp_call, "tool_kind") == "mcp"
    assert _ref(mcp_call, "tool_resolved") == "ai-r:query"

    web = by_tool["WebFetch"]
    assert _ref(web, "tool_kind") == "web"
    assert _ref(web, "tool_resolved") is None

    bash = by_tool["Bash"]
    assert _ref(bash, "tool_kind") == "bash"
    assert bash.type == "tool_call(bash)"
    assert _ref(bash, "tool_resolved") is None


def test_wrapper_without_signal_has_no_resolved(claude_wrappers: str) -> None:
    """A Task whose input names no subagent stays honest: kind, no name."""
    events = [
        e for e in iter_events("claude", session=claude_wrappers)
        if _ref(e, "tool") == "Task"
    ]
    anonymous = [e for e in events if _ref(e, "tool_resolved") is None]
    assert len(anonymous) == 1
    assert _ref(anonymous[0], "tool_kind") == "task"


def test_codex_spawn_agent_resolves(codex_spawn: str) -> None:
    events = [
        e for e in iter_events("codex", session=codex_spawn)
        if e.type.startswith("tool_call(")
    ]
    assert len(events) == 1
    assert _ref(events[0], "tool_kind") == "task"
    assert _ref(events[0], "tool_resolved") == "explorer"


# ---------------------------------------------------------------------------
# query facets + dict shape
# ---------------------------------------------------------------------------


def test_query_tool_kind_facet(claude_wrappers: str) -> None:
    rows = query(agent="claude", session=claude_wrappers, tool_kind="task")
    assert len(rows) == 2
    assert all(row["tool_kind"] == "task" for row in rows)
    mcp_rows = query(agent="claude", session=claude_wrappers, tool_kind="mcp")
    assert [row["tool_resolved"] for row in mcp_rows] == ["ai-r:query"]
    web_rows = query(agent="claude", session=claude_wrappers, tool_kind="web")
    assert len(web_rows) == 1
    assert "tool_resolved" not in web_rows[0]


def test_query_tool_kind_unknown_value_fails_loud(claude_wrappers: str) -> None:
    with pytest.raises(ValueError, match="tool_kind"):
        query(agent="claude", session=claude_wrappers, tool_kind="banana")


def test_query_tool_facet_matches_resolved_name(claude_wrappers: str) -> None:
    """``tool="commit"`` finds the SlashCommand that ran the commit skill."""
    rows = query(agent="claude", session=claude_wrappers, tool="commit")
    assert len(rows) == 1
    assert rows[0]["tool_kind"] == "skill"
    assert rows[0]["tool_resolved"] == "commit"
    # The raw-name path still works unchanged.
    raw = query(agent="claude", session=claude_wrappers, tool="slashcommand")
    assert [r["id"] for r in raw] == [rows[0]["id"]]


def test_non_tool_events_unchanged(claude_wrappers: str) -> None:
    rows = query(agent="claude", session=claude_wrappers, type="user_turn")
    assert rows
    for row in rows:
        assert "tool_kind" not in row
        assert "tool_resolved" not in row


def test_aggregate_by_tool_kind_over_query_rows(claude_wrappers: str) -> None:
    rows = query(agent="claude", session=claude_wrappers, type="tool_call")
    rollup = aggregate(rows, group_by="tool_kind", metrics=("count",))
    counts = {g["group"]: g["count"] for g in rollup["groups"]}
    assert counts == {
        "task": 2, "skill": 2, "mcp": 1, "web": 1, "bash": 1,
    }


# ---------------------------------------------------------------------------
# find_tool_calls records
# ---------------------------------------------------------------------------


def test_find_tool_calls_records_carry_kind_and_resolved(
    claude_wrappers: str,
) -> None:
    res = find_tool_calls(tool_name="Skill", agent="claude")
    assert res["count"] == 1
    rec = res["records"][0]
    assert rec["tool_kind"] == "skill"
    assert rec["tool_resolved"] == "ai-local-reader"

    res_bash = find_tool_calls(tool_name="Bash", agent="claude")
    recs = [
        r for r in res_bash["records"]
        if r["session_uuid"] == claude_wrappers
    ]
    assert recs and all(r["tool_kind"] == "bash" for r in recs)
    assert all(r["tool_resolved"] is None for r in recs)


def test_find_tool_calls_resolves_mcp_pattern(claude_wrappers: str) -> None:
    res = find_tool_calls(tool_name_pattern="mcp__", agent="claude")
    assert res["count"] == 1
    rec = res["records"][0]
    assert rec["tool_kind"] == "mcp"
    assert rec["tool_resolved"] == "ai-r:query"


# ---------------------------------------------------------------------------
# MCP wrapper
# ---------------------------------------------------------------------------


def test_mcp_query_tool_kind_facet(claude_wrappers: str) -> None:
    res = mcp_query(agent="claude", session=claude_wrappers, tool_kind="skill")
    assert res["count"] == 2
    assert {e["tool_resolved"] for e in res["events"]} == {
        "ai-local-reader", "commit",
    }


def test_mcp_query_tool_kind_invalid_is_error_dict(
    claude_wrappers: str,
) -> None:
    res = mcp_query(agent="claude", session=claude_wrappers, tool_kind="nope")
    assert res["error"] == "invalid_argument"
    assert "tool_kind" in res["message"]


# ---------------------------------------------------------------------------
# Redaction of tool_resolved (F2.1 wiring)
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_secret_skill(tmp_sessions_dir: Path) -> str:
    """Pathological wrapper whose resolved name contains a secret token."""
    session_id = "wrappers-secret-1"
    token = "ghp_0123456789abcdef0123456789abcdef0123"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-w"
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
                        {"type": "tool_use", "id": "tu_1", "name": "Skill",
                         "input": {"skill": token}},
                    ],
                },
                "timestamp": "2026-06-14T10:00:01Z",
                "sessionId": session_id,
            },
        ],
    )
    return session_id


def test_tool_resolved_is_redacted_on_emission(
    claude_secret_skill: str,
) -> None:
    redactions: dict[str, int] = {}
    rows = query(
        agent="claude", session=claude_secret_skill, tool_kind="skill",
        redactions_out=redactions,
    )
    assert len(rows) == 1
    assert "ghp_" not in rows[0]["tool_resolved"]
    assert "[REDACTED_" in rows[0]["tool_resolved"]
    # The refs mirror is masked too — no raw secret on any surface.
    ref_vals = [
        r["tool_resolved"] for r in rows[0]["refs"] if "tool_resolved" in r
    ]
    assert ref_vals and "ghp_" not in ref_vals[0]
    assert redactions

    raw = query(
        agent="claude", session=claude_secret_skill, tool_kind="skill",
        redact=False,
    )
    assert raw[0]["tool_resolved"].startswith("ghp_")
