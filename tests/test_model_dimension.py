"""Model dimension — «which model did what» over the existing taxonomy.

Covers the per-agent ``Message.model`` / ``Session.models`` extraction:
Claude (assistant ``message.model``, ``<synthetic>`` → None, Desktop
``extra["model"]``), Codex (``turn_context.model`` inherited by the turn's
assistant items), OpenCode (``message.data.modelID``), Pi (assistant
``message.model``) and Antigravity (no structured signal — honest absence).
All fixtures are hermetic (temp ``AI_R_HOME`` via the autouse conftest
isolation); no host data is read.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_r.parsers import antigravity, claude, codex, opencode, pi
from ai_r.parsers.claude import _desktop_extra


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def _claude_assistant(text: str, ts: str, session_id: str, model=None) -> dict:
    message: dict = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }
    if model is not None:
        message["model"] = model
    return {
        "type": "assistant",
        "message": message,
        "timestamp": ts,
        "sessionId": session_id,
    }


@pytest.fixture
def claude_two_models(tmp_sessions_dir: Path) -> str:
    """A Claude session whose assistant turns come from TWO models,
    plus a ``<synthetic>`` stub and a model-less record.

    The uuid is UUID-shaped so the ``detect_current`` cascade (which
    validates Claude ids against the UUID pattern) accepts it too.
    """
    session_id = "cafe0001-2222-4333-8444-555555555555"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-m"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "first ask"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            _claude_assistant(
                "alpha answer", "2026-06-14T10:00:05Z", session_id,
                model="model-alpha-1",
            ),
            # Locally-generated stub: ``<synthetic>`` is NOT a model.
            _claude_assistant(
                "interrupted", "2026-06-14T10:00:06Z", session_id,
                model="<synthetic>",
            ),
            {
                "type": "user",
                "message": {"role": "user", "content": "second ask"},
                "timestamp": "2026-06-14T10:00:10Z",
                "sessionId": session_id,
            },
            _claude_assistant(
                "beta answer", "2026-06-14T10:00:15Z", session_id,
                model="model-beta-2",
            ),
            # Same model again — Session.models stays unique.
            _claude_assistant(
                "beta again", "2026-06-14T10:00:20Z", session_id,
                model="model-beta-2",
            ),
            # No model field at all — honest None.
            _claude_assistant(
                "modelless", "2026-06-14T10:00:25Z", session_id,
            ),
        ],
    )
    return session_id


def test_claude_message_model(claude_two_models: str) -> None:
    messages = claude.read_messages(claude_two_models)
    by_text = {m.text: m for m in messages}
    assert by_text["alpha answer"].model == "model-alpha-1"
    assert by_text["beta answer"].model == "model-beta-2"
    # ``<synthetic>`` is a placeholder, not a model.
    assert by_text["interrupted"].model is None
    # Absent field → honest None.
    assert by_text["modelless"].model is None
    # User messages never carry a model.
    assert by_text["first ask"].model is None


def test_claude_session_models_unique_in_order(claude_two_models: str) -> None:
    session = claude.read_session(claude_two_models)
    assert session.models == ("model-alpha-1", "model-beta-2")


def test_claude_no_signal_is_empty(fake_claude_session: Path) -> None:
    # The plain conftest session records no ``message.model`` at all.
    messages = claude.read_messages("test-claude-1")
    assert all(m.model is None for m in messages)
    session = claude.read_session("test-claude-1")
    assert session.models == ()


def test_claude_desktop_extra_lifts_model() -> None:
    extra = _desktop_extra(
        {"sessionId": "local_x", "model": "model-desktop-9"}
    )
    assert extra["model"] == "model-desktop-9"
    # No model in the metadata → no key (never fabricated).
    assert "model" not in _desktop_extra({"sessionId": "local_y"})


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


@pytest.fixture
def codex_two_models(tmp_sessions_dir: Path) -> str:
    """A Codex rollout whose two turns run under different models."""
    uuid = "codex-models-1"
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
                "payload": {"id": uuid, "cwd": "/tmp/work"},
            },
            {
                "timestamp": "2026-06-14T10:00:01Z",
                "type": "turn_context",
                "payload": {"model": "model-alpha-1"},
            },
            {
                "timestamp": "2026-06-14T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "first ask here"}],
                },
            },
            {
                "timestamp": "2026-06-14T10:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "alpha answer"}],
                },
            },
            {
                "timestamp": "2026-06-14T10:00:04Z",
                "type": "turn_context",
                "payload": {"model": "model-beta-2"},
            },
            {
                "timestamp": "2026-06-14T10:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": json.dumps({"command": ["echo", "hi"]}),
                },
            },
            {
                "timestamp": "2026-06-14T10:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "beta answer"}],
                },
            },
        ],
    )
    return uuid


def test_codex_messages_inherit_turn_context_model(codex_two_models: str) -> None:
    messages = codex.read_messages(codex_two_models)
    by_text = {m.text: m for m in messages if m.text}
    assert by_text["alpha answer"].model == "model-alpha-1"
    assert by_text["beta answer"].model == "model-beta-2"
    # User messages never carry a model.
    assert by_text["first ask here"].model is None
    # The tool call belongs to the SECOND turn → the second model.
    tool_msgs = [m for m in messages if m.tool_use]
    assert tool_msgs and tool_msgs[0].model == "model-beta-2"


def test_codex_session_models_unique_in_order(codex_two_models: str) -> None:
    session = codex.read_session(codex_two_models)
    assert session.models == ("model-alpha-1", "model-beta-2")


def test_codex_no_turn_context_is_none(tmp_sessions_dir: Path) -> None:
    uuid = "codex-nomodel-1"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T11-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T11:00:00Z",
                "type": "session_meta",
                "payload": {"id": uuid},
            },
            {
                "timestamp": "2026-06-14T11:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "no ctx"}],
                },
            },
        ],
    )
    messages = codex.read_messages(uuid)
    assert all(m.model is None for m in messages)
    assert codex.read_session(uuid).models == ()


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


@pytest.fixture
def opencode_models_db(tmp_sessions_dir: Path) -> Path:
    """An OpenCode DB whose assistant rows carry two distinct ``modelID``s."""
    db_path = tmp_sessions_dir / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id           TEXT PRIMARY KEY,
            parent_id    TEXT,
            title        TEXT,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE message (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES session(id),
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data         TEXT
        );
        CREATE TABLE part (
            id           TEXT PRIMARY KEY,
            message_id   TEXT NOT NULL REFERENCES message(id),
            session_id   TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data         TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
        ("oc-models-1", None, "Model session",
         1_716_000_000_000, 1_716_000_500_000),
    )
    rows = [
        ("m-0", 1_716_000_100_000, {"role": "user"}),
        ("m-1", 1_716_000_200_000,
         {"role": "assistant", "modelID": "model-alpha-1"}),
        ("m-2", 1_716_000_300_000,
         {"role": "assistant", "modelID": "model-beta-2"}),
        # Repeat — Session.models stays unique.
        ("m-3", 1_716_000_400_000,
         {"role": "assistant", "modelID": "model-alpha-1"}),
        # Assistant row without a modelID — honest None.
        ("m-4", 1_716_000_450_000, {"role": "assistant"}),
    ]
    for mid, ts, data in rows:
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            (mid, "oc-models-1", ts, ts, json.dumps(data)),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (f"p-{mid}", mid, "oc-models-1", ts, ts,
             json.dumps({"type": "text", "text": f"text {mid}"})),
        )
    conn.commit()
    conn.close()
    return db_path


def test_opencode_message_model(
    opencode_models_db: Path, tmp_sessions_dir: Path
) -> None:
    messages = opencode.read_messages(
        "oc-models-1", base_dir=str(tmp_sessions_dir)
    )
    models = [m.model for m in messages]
    assert models == [None, "model-alpha-1", "model-beta-2",
                      "model-alpha-1", None]


def test_opencode_session_models_unique_in_order(
    opencode_models_db: Path, tmp_sessions_dir: Path
) -> None:
    session = opencode.read_session(
        "oc-models-1", base_dir=str(tmp_sessions_dir)
    )
    assert session.models == ("model-alpha-1", "model-beta-2")
    listed = opencode.list_sessions(base_dir=str(tmp_sessions_dir))
    assert [s.models for s in listed if s.uuid == "oc-models-1"] == [
        ("model-alpha-1", "model-beta-2")
    ]


# ---------------------------------------------------------------------------
# Pi
# ---------------------------------------------------------------------------


@pytest.fixture
def pi_two_models(tmp_sessions_dir: Path) -> str:
    uuid = "pi-models-1"
    jsonl = (
        tmp_sessions_dir / ".pi" / "agent" / "sessions" / "--tmp-work--"
        / f"2026-06-14T10-00-00-000Z_{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "session",
                "version": 3,
                "id": uuid,
                "timestamp": "2026-06-14T10:00:00.000Z",
                "cwd": "/tmp/work",
            },
            {
                "type": "message",
                "id": "u-1",
                "timestamp": "2026-06-14T10:00:01.000Z",
                "message": {"role": "user", "content": "ask"},
            },
            {
                "type": "message",
                "id": "a-1",
                "timestamp": "2026-06-14T10:00:02.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "alpha answer"}],
                    "model": "model-alpha-1",
                },
            },
            {
                "type": "message",
                "id": "a-2",
                "timestamp": "2026-06-14T10:00:03.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "beta answer"}],
                    "model": "model-beta-2",
                },
            },
        ],
    )
    return uuid


def test_pi_message_model(pi_two_models: str, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    messages = pi.read_messages(pi_two_models, base_dir=base)
    by_text = {m.text: m for m in messages if m.text}
    assert by_text["alpha answer"].model == "model-alpha-1"
    assert by_text["beta answer"].model == "model-beta-2"
    assert by_text["ask"].model is None


def test_pi_session_models_unique_in_order(
    pi_two_models: str, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    session = pi.read_session(pi_two_models, base_dir=base)
    assert session.models == ("model-alpha-1", "model-beta-2")


def test_pi_no_model_field_is_none(
    fake_pi_session: Path, tmp_sessions_dir: Path
) -> None:
    # The conftest Pi fixture records a ``model_change`` entry but NO
    # per-message ``model`` — only the per-message field is a model fact.
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    messages = pi.read_messages("test-pi-1", base_dir=base)
    assert all(m.model is None for m in messages)
    assert pi.read_session("test-pi-1", base_dir=base).models == ()


# ---------------------------------------------------------------------------
# Antigravity — no structured model signal, honest absence
# ---------------------------------------------------------------------------


def test_antigravity_has_no_model_signal(fake_antigravity_brain: Path) -> None:
    base = str(fake_antigravity_brain.parent)
    session = antigravity.read_session("test-ag-1", base_dir=base)
    assert session.models == ()
    messages = antigravity.read_messages("test-ag-1", base_dir=base)
    assert messages, "fixture should yield messages"
    assert all(m.model is None for m in messages)


# ---------------------------------------------------------------------------
# Event layer — events inherit the producing message's model
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_model_events(tmp_sessions_dir: Path) -> str:
    """A Claude session mixing two models across a turn, a tool call and
    a plan signal — the event-inheritance fixture."""
    session_id = "claude-model-events-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-m"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "please edit"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "model-alpha-1",
                    "content": [
                        {"type": "text", "text": "editing now"},
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
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "model-beta-2",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "# Beta Plan\n\n- step"},
                        },
                    ],
                },
                "timestamp": "2026-06-14T10:00:10Z",
                "sessionId": session_id,
            },
        ],
    )
    return session_id


def test_events_inherit_message_model(claude_model_events: str) -> None:
    from ai_r.events.model import iter_events

    events = list(iter_events("claude", session=claude_model_events))
    by_type = {}
    for ev in events:
        by_type.setdefault(ev.type, []).append(ev)
    # assistant_turn + its tool_call carry the producing model.
    assert [e.model for e in by_type["assistant_turn"]] == ["model-alpha-1"]
    assert [e.model for e in by_type["tool_call(edit)"]] == ["model-alpha-1"]
    # The plan tool_use AND the derived plan_event carry the second model.
    assert [e.model for e in by_type["tool_call(other)"]] == ["model-beta-2"]
    assert [e.model for e in by_type["plan_event"]] == ["model-beta-2"]
    # User turns have no producing model — honest None.
    assert [e.model for e in by_type["user_turn"]] == [None]


def test_events_model_none_without_signal(fake_claude_session: Path) -> None:
    from ai_r.events.model import iter_events

    events = list(iter_events("claude", session="test-claude-1"))
    assert events
    assert all(ev.model is None for ev in events)


# ---------------------------------------------------------------------------
# Surface — query facet, event dicts, aggregate, summary, detect_current
# ---------------------------------------------------------------------------


def test_query_rows_carry_model_only_when_present(
    claude_model_events: str,
) -> None:
    from ai_r.events import query

    rows = query(session=claude_model_events)
    by_type = {r["type"]: r for r in rows}
    assert by_type["assistant_turn"]["model"] == "model-alpha-1"
    assert by_type["tool_call(edit)"]["model"] == "model-alpha-1"
    assert by_type["plan_event"]["model"] == "model-beta-2"
    # No signal → no key at all (base event shape unchanged).
    assert "model" not in by_type["user_turn"]


def test_query_model_facet_exact_case_insensitive(
    claude_model_events: str,
) -> None:
    from ai_r.events import query

    hits = query(session=claude_model_events, model="MODEL-ALPHA-1")
    assert hits and all(r["model"] == "model-alpha-1" for r in hits)
    # Exact match, not substring: a prefix must not match.
    assert query(session=claude_model_events, model="model-alpha") == []
    # Unknown model → honest empty.
    assert query(session=claude_model_events, model="no-such-model") == []


def test_query_model_facet_empty_string_fails_loud() -> None:
    from ai_r.events import query

    with pytest.raises(ValueError, match="model"):
        query(model="   ")


def test_aggregate_group_by_model(claude_model_events: str) -> None:
    from ai_r.events import aggregate, query

    rows = query(session=claude_model_events)
    result = aggregate(rows, group_by="model", metrics=("count",))
    counts = {g["group"]: g["count"] for g in result["groups"]}
    # alpha: assistant_turn + tool_call(edit); beta: tool_call + plan_event;
    # the user_turn has no model → the honest "(unknown)" bucket.
    assert counts["model-alpha-1"] == 2
    assert counts["model-beta-2"] == 2
    assert counts["(unknown)"] == 1
    assert sum(counts.values()) == len(rows)


def test_mcp_session_summary_carries_models(claude_two_models: str) -> None:
    from ai_r.mcp_server import list_sessions, read_session

    listed = list_sessions(agent="claude")
    by_uuid = {s["uuid"]: s for s in listed["sessions"]}
    assert by_uuid[claude_two_models]["models"] == [
        "model-alpha-1", "model-beta-2",
    ]
    read = read_session(claude_two_models, agent="claude")
    assert read["models"] == ["model-alpha-1", "model-beta-2"]


def test_mcp_session_summary_models_empty_without_signal(
    fake_claude_session: Path,
) -> None:
    from ai_r.mcp_server import list_sessions

    listed = list_sessions(agent="claude")
    by_uuid = {s["uuid"]: s for s in listed["sessions"]}
    assert by_uuid["test-claude-1"]["models"] == []


def test_mcp_query_model_facet(claude_model_events: str) -> None:
    from ai_r.mcp_server import query as mcp_query

    result = mcp_query(session=claude_model_events, model="model-beta-2")
    assert result["count"] == 2
    assert all(e["model"] == "model-beta-2" for e in result["events"])
    bad = mcp_query(model="")
    assert bad["error"] == "invalid_argument"


@pytest.fixture
def _clean_detect_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Blank every env var + flag dir the detect cascade reads."""
    for var in (
        "AI_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
        "OPENCODE_SESSION_ID", "AGENT_NAME", "AI_AGENT", "CODING_AGENT",
        "CODEX_HOME", "CLAUDECODE", "OPENCODE", "AI_SESSION_OUTPUT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AI_R_SESSION_IDENTITY_DIR", str(tmp_path / "identity"))


def test_detect_current_reads_last_assistant_model(
    _clean_detect_env: None,
    monkeypatch: pytest.MonkeyPatch,
    claude_two_models: str,
) -> None:
    from ai_r.events import detect_current

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", claude_two_models)
    r = detect_current()
    assert r["session_id"] == claude_two_models
    # The LAST assistant message carrying a model (the trailing model-less
    # stub is skipped, never guessed into a value).
    assert r["model"] == "model-beta-2"


def test_detect_current_model_none_when_unresolvable(
    _clean_detect_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_r.events import detect_current

    # No session detected at all → model is honestly None.
    assert detect_current()["model"] is None
    # A detected id whose transcript does not exist → still None, no crash.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "no-such-session-1")
    assert detect_current()["model"] is None
