"""Shared pytest fixtures for ai-r tests.

Fixtures are deterministic: every fixture that touches the filesystem
creates a temporary directory and never writes outside ``tmp_path`` or
``AI_R_HOME`` (the latter only when explicitly overridden).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Iterator, List

import pytest


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------

# Markers that may be added later to the package's own pyproject
pytest_plugins: list[str] = []


# Fixtures that read the *real* user home.  A test requesting any of these
# is host-dependent and gets auto-tagged ``@pytest.mark.host`` so the
# hermetic CI job (``pytest -m "not host"``) deselects it.
_HOST_FIXTURES = frozenset(
    {
        "real_claude_dir",
        "real_claude_desktop_dir",
        "real_codex_dir",
        "real_opencode_db",
        "real_pi_dir",
        "real_antigravity_root",
        "real_claude_home",
        "frozen_claude_home",
    }
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-tag every test that depends on real host data with ``host``.

    Keeps the hermetic invariant honest without each author remembering to
    add the marker by hand: requesting a ``real_*`` fixture *is* the signal.
    """
    for item in items:
        fixturenames = getattr(item, "fixturenames", ())
        if _HOST_FIXTURES.intersection(fixturenames):
            item.add_marker(pytest.mark.host)


@pytest.fixture(autouse=True)
def _isolate_ai_r_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Force parsers to look at a per-test temp directory tree.

    The smoke step established that real ``~/.claude``, ``~/.codex``,
    ``~/.local/share/opencode`` and ``~/.gemini`` directories exist on
    this host.  We *want* a few integration tests to hit them
    (read-only), but the default behaviour for most tests must be
    hermetic.  By setting ``AI_R_HOME`` to a fresh temp dir the
    parsers fall back to it and find nothing.
    """
    monkeypatch.setenv("AI_R_HOME", str(tmp_path / "fake_home"))
    # OpenCode honours a separate env var.  Wipe it to avoid leaking the
    # real DB into parser-discovery tests.
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    yield


# ---------------------------------------------------------------------------
# Fake session data builders
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


@pytest.fixture
def tmp_sessions_dir(tmp_path: Path) -> Path:
    """A fresh root directory that mimics ``AI_R_HOME``.

    Sub-directories matching the parser layout are created but left
    empty unless the requesting test populates them.
    """
    home = tmp_path / "fake_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "projects" / "proj-a").mkdir(parents=True)
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".gemini" / "antigravity" / "brain").mkdir(parents=True)
    (home / ".gemini" / "antigravity-cli" / "brain").mkdir(parents=True)
    (home / ".pi" / "agent" / "sessions").mkdir(parents=True)
    return home


@pytest.fixture
def fake_claude_session(tmp_sessions_dir: Path) -> Path:
    """A single Claude session JSONL inside the fake projects tree."""
    session_id = "test-claude-1"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-a" / f"{session_id}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "Hello, world"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there!"}],
                },
                "timestamp": "2026-06-14T10:00:05Z",
                "sessionId": session_id,
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_claude_subagent(tmp_sessions_dir: Path) -> Path:
    """A Claude subagent session under ``<parent-uuid>/subagents/agent-*.jsonl``.

    Mirrors the real directory-form layout: the parent session has its own
    folder named after its uuid, and spawned subagents live in a
    ``subagents`` sub-directory.  Records carry ``isSidechain: True`` and a
    ``parentUuid`` pointing at the parent session (the directory name is the
    authoritative parent uuid).
    """
    parent_uuid = "parent-claude-1"
    agent_id = "agent-sub-1"
    jsonl = (
        tmp_sessions_dir
        / ".claude"
        / "projects"
        / "proj-a"
        / parent_uuid
        / "subagents"
        / f"{agent_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "Do the subtask"},
                "timestamp": "2026-06-14T11:00:00Z",
                "sessionId": agent_id,
                "parentUuid": parent_uuid,
                "isSidechain": True,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Subtask done."}],
                },
                "timestamp": "2026-06-14T11:00:05Z",
                "sessionId": agent_id,
                "parentUuid": parent_uuid,
                "isSidechain": True,
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_codex_session(tmp_sessions_dir: Path) -> Path:
    """A single Codex rollout file inside the fake sessions tree."""
    uuid = "test-codex-1"
    jsonl = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
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
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": "Roll out please"}],
                },
            },
            {
                "timestamp": "2026-06-14T10:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_codex_subagent(tmp_sessions_dir: Path) -> Path:
    """A Codex *subagent* rollout: ``session_meta.payload.thread_source ==
    "subagent"`` plus a flat ``parent_thread_id`` and the nested
    ``source.subagent.thread_spawn`` blob (mirrors real rollouts)."""
    uuid = "test-codex-sub-1"
    jsonl = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T11-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T11:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": uuid,
                    "cwd": "/tmp/work",
                    "timestamp": "2026-06-14T11:00:00Z",
                    "thread_source": "subagent",
                    "parent_thread_id": "test-codex-1",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "test-codex-1",
                                "depth": 1,
                                "agent_nickname": "Galileo",
                                "agent_role": "explorer",
                            }
                        }
                    },
                },
            },
            {
                "timestamp": "2026-06-14T11:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": "Explore the repo"}],
                },
            },
            {
                "timestamp": "2026-06-14T11:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Explored."}],
                },
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_codex_subagent_nested_only(tmp_sessions_dir: Path) -> Path:
    """A Codex subagent rollout WITHOUT the flat ``parent_thread_id`` —
    the parent lives only in ``source.subagent.thread_spawn`` (fallback
    branch of the parser)."""
    uuid = "test-codex-sub-2"
    jsonl = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T12-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": uuid,
                    "cwd": "/tmp/work",
                    "timestamp": "2026-06-14T12:00:00Z",
                    "thread_source": "subagent",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "test-codex-1",
                                "depth": 1,
                            }
                        }
                    },
                },
            },
            {
                "timestamp": "2026-06-14T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": "Nested spawn"}],
                },
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_pi_subagent(tmp_sessions_dir: Path) -> Path:
    """A Pi session whose header carries ``parentSession`` (spawned child)."""
    uuid = "test-pi-sub-1"
    jsonl = (
        tmp_sessions_dir
        / ".pi"
        / "agent"
        / "sessions"
        / "--tmp-work--"
        / f"2026-06-14T11-00-00-000Z_{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "session",
                "version": 3,
                "id": uuid,
                "timestamp": "2026-06-14T11:00:00.000Z",
                "cwd": "/tmp/work",
                "parentSession": "test-pi-1",
            },
            {
                "type": "message",
                "id": "user-1",
                "parentId": None,
                "timestamp": "2026-06-14T11:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Child task"}],
                    "timestamp": 1_718_363_602_000,
                },
            },
            {
                "type": "message",
                "id": "assistant-1",
                "parentId": "user-1",
                "timestamp": "2026-06-14T11:00:04.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Child done."}],
                    "timestamp": 1_718_363_604_000,
                },
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_pi_session(tmp_sessions_dir: Path) -> Path:
    """A single Pi JSONL session inside the fake sessions tree."""
    uuid = "test-pi-1"
    jsonl = (
        tmp_sessions_dir
        / ".pi"
        / "agent"
        / "sessions"
        / "--tmp-work--"
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
                "type": "model_change",
                "id": "model-1",
                "parentId": None,
                "timestamp": "2026-06-14T10:00:00.001Z",
                "provider": "openai-codex",
                "modelId": "gpt-test",
            },
            {
                "type": "message",
                "id": "user-1",
                "parentId": "model-1",
                "timestamp": "2026-06-14T10:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Add Pi support"}],
                    "timestamp": 1_718_360_002_000,
                },
            },
            {
                "type": "message",
                "id": "assistant-1",
                "parentId": "user-1",
                "timestamp": "2026-06-14T10:00:04.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "Done."},
                    ],
                    "timestamp": 1_718_360_004_000,
                },
            },
            {
                "type": "message",
                "id": "tool-1",
                "parentId": "assistant-1",
                "timestamp": "2026-06-14T10:00:05.000Z",
                "message": {"role": "toolResult", "content": "ignored"},
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_opencode_db(tmp_sessions_dir: Path) -> Path:
    """A minimal OpenCode SQLite database with one session + 2 messages.

    Mirrors the real schema: ``message`` rows carry metadata-only
    ``data`` (role/time) and the actual bodies live in the ``part``
    table linked by ``message_id``.  ``test-oc-1`` has a user message
    with a ``text`` part and an assistant message with a ``text`` part.
    ``test-oc-2`` has one message with NO parts (graceful-degradation
    case).
    """
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
        ("test-oc-1", None, "First OpenCode session",
         1_716_000_000_000, 1_716_000_500_000),
    )
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
        ("test-oc-2", "test-oc-1", "Child session",
         1_716_000_600_000, 1_716_000_900_000),
    )
    # test-oc-1: user msg (text part) + assistant msg (text part)
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("m-0", "test-oc-1", 1_716_000_100_000, 1_716_000_100_000,
         json.dumps({"role": "user", "time": {"created": 1_716_000_100_000}})),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("m-1", "test-oc-1", 1_716_000_200_000, 1_716_000_200_000,
         json.dumps({"role": "assistant", "time": {"created": 1_716_000_200_000}})),
    )
    # test-oc-2: one message with NO parts
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("m-2", "test-oc-2", 1_716_000_700_000, 1_716_000_700_000,
         json.dumps({"role": "assistant", "time": {"created": 1_716_000_700_000}})),
    )
    conn.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("p-0", "m-0", "test-oc-1", 1_716_000_100_000, 1_716_000_100_000,
             json.dumps({"type": "text", "text": "Hello"})),
            ("p-1", "m-1", "test-oc-1", 1_716_000_200_000, 1_716_000_200_000,
             json.dumps({"type": "text", "text": "Hi there"})),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def fake_antigravity_brain(tmp_sessions_dir: Path) -> Path:
    """A single brain directory with a minimal overview.txt + transcript."""
    brain = tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / "test-ag-1"
    (brain / ".system_generated" / "logs").mkdir(parents=True)
    overview = brain / ".system_generated" / "logs" / "overview.txt"
    _write_jsonl(
        overview,
        [
            {
                "timestamp": "2026-06-14T10:00:00Z",
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Set up the lab</USER_REQUEST>",
            },
            {
                "timestamp": "2026-06-14T10:00:05Z",
                "source": "MODEL",
                "type": "MODEL_OUTPUT",
                "content": "ok",
            },
        ],
    )
    return brain


# ---------------------------------------------------------------------------
# Plan-signal fixtures (Phase 2: plan_event + Plan atom + get_body)
# ---------------------------------------------------------------------------


def _claude_exit_plan(plan_text: str, ts: str) -> dict:
    """A Claude assistant record carrying one ``ExitPlanMode`` tool_use."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "ExitPlanMode",
                    "input": {"plan": plan_text},
                }
            ],
        },
        "timestamp": ts,
    }


def _claude_plan_write(file_path: str, content: str, ts: str) -> dict:
    """A Claude assistant record writing a ``plans/<slug>.md`` file."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": file_path, "content": content},
                }
            ],
        },
        "timestamp": ts,
    }


@pytest.fixture
def fake_claude_plan_redraft(tmp_sessions_dir: Path) -> str:
    """Claude session: one plan-file slug + DRIFTING titles → 1 final + N-1 draft.

    Mirrors the real ``proud-snacking-ritchie`` defect: a single
    ``plans/build-feature.md`` iteration chain whose title drifts as it gets
    decorated ("Build Feature X" → "…(session…)" → "…final").  Grouping keys
    on the slug (Write signals carry it; the interleaved ``ExitPlanMode``
    calls, which have no path, inherit the nearest preceding Write's slug), so
    despite the title drift this is ONE task: 1 final + 3 draft, 0 major.
    """
    session_id = "plan-redraft-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-plan"
        / f"{session_id}.jsonl"
    )
    slug = "/repo/plans/build-feature.md"
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "build feature X"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            # Write establishes the slug; title = "Build Feature X".
            _claude_plan_write(slug, "# Build Feature X\n\nDraft one.",
                               "2026-06-14T10:00:05Z"),
            # ExitPlanMode (no path) inherits the slug; title drifts.
            _claude_exit_plan("# Build Feature X (session…)\n\nDraft two.",
                              "2026-06-14T10:00:10Z"),
            _claude_exit_plan("# Build Feature (внутр…)\n\nDraft three.",
                              "2026-06-14T10:00:15Z"),
            # Final Write to the SAME slug; title drifted further still.
            _claude_plan_write(slug, "# Build Feature + смеж…\n\nFinal plan.",
                               "2026-06-14T10:00:20Z"),
        ],
    )
    return session_id


@pytest.fixture
def fake_claude_plan_multitask(tmp_sessions_dir: Path) -> str:
    """Claude session: two DIFFERENT plan-file slugs → two separate tasks.

    Split is by slug, not by title.  ``plans/task-a.md`` (earlier) becomes
    ``completed_major``; ``plans/task-b.md`` (later) keeps ``final``.
    """
    session_id = "plan-multitask-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-plan"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "do task A"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            _claude_plan_write("/repo/plans/task-a.md",
                               "# Task A\n\nPlan A body.",
                               "2026-06-14T10:00:05Z"),
            {
                "type": "user",
                "message": {"role": "user", "content": "now task B"},
                "timestamp": "2026-06-14T10:00:10Z",
            },
            _claude_plan_write("/repo/plans/task-b.md",
                               "# Task B\n\nPlan B body.",
                               "2026-06-14T10:00:15Z"),
        ],
    )
    return session_id


@pytest.fixture
def fake_claude_plan_write(tmp_sessions_dir: Path) -> str:
    """Claude session whose plan signal is a ``Write`` to ``plans/*.md``."""
    session_id = "plan-write-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-plan"
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
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": "/repo/plans/feature.md",
                                "content": "# Written Plan\n\nDetails.",
                            },
                        }
                    ],
                },
                "timestamp": "2026-06-14T10:00:05Z",
            },
        ],
    )
    return session_id


def _claude_exit_plan_with_id(plan_text: str, tool_use_id: str, ts: str) -> dict:
    """A Claude assistant record: one ``ExitPlanMode`` tool_use with a call id."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "ExitPlanMode",
                    "input": {"plan": plan_text},
                }
            ],
        },
        "timestamp": ts,
    }


def _claude_tool_result(tool_use_id: str, content: str, ts: str) -> dict:
    """A Claude user record carrying one ``tool_result`` block."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ],
        },
        "timestamp": ts,
    }


@pytest.fixture
def fake_claude_plan_feedback(tmp_sessions_dir: Path) -> str:
    """Claude plan-iteration session with the four real response formats (F3.4).

    Four ``ExitPlanMode`` revisions of ONE task ("Feature Plan"):

    * tu-plan-0 — technical failure result (permission stream) → filtered;
    * tu-plan-1 — rejection with a free-text preamble + two
      "On selected text:" quote→comment pairs (one comment carries a
      redactable secret);
    * tu-plan-2 — stay-in-plan-mode with two ``[Re: "…"]`` pairs (the second
      comment is multi-line);
    * tu-plan-3 — approval carrying the AUTHORITATIVE user-edited plan text
      ("## Approved Plan (edited by user):"), which must override the
      signal body of the final revision.
    """
    session_id = "plan-feedback-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-plan"
        / f"{session_id}.jsonl"
    )
    reject_boiler = (
        "The user doesn't want to proceed with this tool use. "
        "The tool use was rejected (eg. if it was a file edit, the "
        "new_string was NOT written to the file). To tell you how to "
        "proceed, the user said:\n"
    )
    rejected = (
        reject_boiler
        + "Overall too vague.\n"
        + "On selected text:\n"
        + "> Draft one body.\n"
        + "Use token=abc12345secret here.\n"
        + "\n"
        + "On selected text:\n"
        + "> Feature Plan\n"
        + "> \n"
        + "Rename the feature.\n"
    )
    stay = (
        "User chose to stay in plan mode and continue planning\n"
        "\n"
        "Comments on the plan:\n"
        '[Re: "Draft two body."] Split into two phases.\n'
        '[Re: "rollout"] Which rollout?\n'
        "More thoughts on a second line.\n"
    )
    approved = (
        "User has approved your plan. You can now start coding. "
        "Start with updating your todo list if applicable\n"
        "\n"
        "Your plan has been saved to: /home/u/.claude/plans/feature.md\n"
        "You can refer back to it if needed during implementation.\n"
        "\n"
        "## Approved Plan (edited by user):\n"
        "# Feature Plan\n"
        "\n"
        "EDITED final body by user.\n"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "plan the feature"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            _claude_exit_plan_with_id(
                "# Feature Plan\n\nDraft zero body.", "tu-plan-0",
                "2026-06-14T10:00:05Z"),
            _claude_tool_result(
                "tu-plan-0",
                "Tool permission request failed: Error: Stream closed",
                "2026-06-14T10:00:06Z"),
            _claude_exit_plan_with_id(
                "# Feature Plan\n\nDraft one body.", "tu-plan-1",
                "2026-06-14T10:00:10Z"),
            _claude_tool_result("tu-plan-1", rejected,
                                "2026-06-14T10:00:11Z"),
            _claude_exit_plan_with_id(
                "# Feature Plan\n\nDraft two body.", "tu-plan-2",
                "2026-06-14T10:00:20Z"),
            _claude_tool_result("tu-plan-2", stay, "2026-06-14T10:00:21Z"),
            _claude_exit_plan_with_id(
                "# Feature Plan\n\nDraft three body.", "tu-plan-3",
                "2026-06-14T10:00:30Z"),
            _claude_tool_result("tu-plan-3", approved,
                                "2026-06-14T10:00:31Z"),
        ],
    )
    return session_id


@pytest.fixture
def fake_codex_plan_session(tmp_sessions_dir: Path) -> str:
    """Codex rollout with several ``update_plan`` calls → last one is final."""
    uuid = "codex-plan-1"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T13-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T13:00:00Z",
                "type": "session_meta",
                "payload": {"id": uuid, "cwd": "/tmp/work"},
            },
            {
                "timestamp": "2026-06-14T13:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "arguments": json.dumps(
                        {"name": "ship the feature",
                         "plan": [{"step": "a", "status": "pending"},
                                  {"step": "b", "status": "pending"}]}
                    ),
                },
            },
            {
                "timestamp": "2026-06-14T13:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "arguments": json.dumps(
                        {"name": "ship the feature",
                         "plan": [{"step": "a", "status": "completed"},
                                  {"step": "b", "status": "in_progress"}]}
                    ),
                },
            },
            {
                "timestamp": "2026-06-14T13:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "arguments": json.dumps(
                        {"name": "ship the feature",
                         "plan": [{"step": "a", "status": "completed"},
                                  {"step": "b", "status": "completed"}]}
                    ),
                },
            },
        ],
    )
    return uuid


@pytest.fixture
def fake_antigravity_plan_brain(tmp_sessions_dir: Path) -> str:
    """Antigravity brain dir with an ``implementation_plan.md`` → one plan."""
    uuid = "ag-plan-1"
    brain = tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / uuid
    (brain / ".system_generated" / "logs").mkdir(parents=True)
    _write_jsonl(
        brain / ".system_generated" / "logs" / "overview.txt",
        [
            {
                "timestamp": "2026-06-14T10:00:00Z",
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>build the thing</USER_REQUEST>",
            },
        ],
    )
    (brain / "implementation_plan.md").write_text(
        "# Antigravity Implementation Plan\n\nStep one.\nStep two.\n",
        encoding="utf-8",
    )
    return uuid


# ---------------------------------------------------------------------------
# Fixtures carrying tool calls (for read_messages tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_claude_session_with_tools(tmp_sessions_dir: Path) -> Path:
    """A Claude session JSONL containing a tool_use + tool_result exchange."""
    session_id = "claude-tools-1"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-t" / f"{session_id}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "Run the tests"},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll run them now."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pytest"},
                        },
                    ],
                },
                "timestamp": "2026-06-14T10:00:05Z",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "5 passed",
                        }
                    ],
                },
                "timestamp": "2026-06-14T10:00:10Z",
                "sessionId": session_id,
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_codex_session_with_tools(tmp_sessions_dir: Path) -> Path:
    """A Codex rollout with a function_call + function_call_output pair."""
    uuid = "codex-tools-1"
    jsonl = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T11-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T11:00:00Z",
                "type": "session_meta",
                "payload": {"id": uuid, "cwd": "/tmp/work"},
            },
            {
                "timestamp": "2026-06-14T11:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": "Run pytest"}],
                },
            },
            {
                "timestamp": "2026-06-14T11:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": "pytest",
                },
            },
            {
                "timestamp": "2026-06-14T11:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": "5 passed",
                },
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_pi_session_with_tools(tmp_sessions_dir: Path) -> Path:
    """A Pi JSONL with an assistant toolCall + a toolResult record."""
    uuid = "pi-tools-1"
    jsonl = (
        tmp_sessions_dir
        / ".pi"
        / "agent"
        / "sessions"
        / "--tmp-work--"
        / f"2026-06-14T11-00-00-000Z_{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "session",
                "id": uuid,
                "timestamp": "2026-06-14T11:00:00.000Z",
                "cwd": "/tmp/work",
            },
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Run pytest"}],
                    "timestamp": 1_718_360_002_000,
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Running now"},
                        {
                            "type": "toolCall",
                            "name": "shell",
                            "arguments": "pytest",
                        },
                    ],
                    "timestamp": 1_718_360_004_000,
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "content": "5 passed",
                    "timestamp": 1_718_360_005_000,
                },
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_opencode_db_with_tools(tmp_sessions_dir: Path) -> Path:
    """OpenCode DB with realistic ``part`` rows for read_messages tests.

    Seeds (session ``oc-tools-1``):
      * ``u1`` user msg        — one ``text`` part.
      * ``a1`` assistant msg   — multi-part ordered:
          ``step-start`` → ``reasoning`` → ``text`` → ``tool`` (call+result
          combined, status=completed) → ``tool`` (error, no output) →
          ``file`` → ``patch`` → ``step-finish``.
    Covers: text, reasoning inlined, tool-call, tool-result, tool-error
    (no output), metadata-only file/patch parts, step-* boundary markers
    skipped, multi-part ordering.
    """
    db_path = tmp_sessions_dir / "opencode_tools.db"
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
        "INSERT INTO session VALUES (?, NULL, ?, ?, ?)",
        ("oc-tools-1", "Tool session", 1_716_000_000_000, 1_716_000_500_000),
    )
    conn.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        [
            ("u1", "oc-tools-1", 1_716_000_100_000, 1_716_000_100_000,
             json.dumps({"role": "user"})),
            ("a1", "oc-tools-1", 1_716_000_200_000, 1_716_000_200_000,
             json.dumps({"role": "assistant"})),
        ],
    )
    t0 = 1_716_000_200_000
    conn.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        [
            # user text
            ("u1-p0", "u1", "oc-tools-1", t0 - 100_000, t0 - 100_000,
             json.dumps({"type": "text", "text": "run tests"})),
            # assistant multi-part, ordered by time_created
            ("a1-p0", "a1", "oc-tools-1", t0 + 0, t0 + 0,
             json.dumps({"type": "step-start", "snapshot": "abc"})),
            ("a1-p1", "a1", "oc-tools-1", t0 + 1, t0 + 1,
             json.dumps({"type": "reasoning", "text": "thinking..."})),
            ("a1-p2", "a1", "oc-tools-1", t0 + 2, t0 + 2,
             json.dumps({"type": "text", "text": "okay"})),
            ("a1-p3", "a1", "oc-tools-1", t0 + 3, t0 + 3,
             json.dumps({
                 "type": "tool", "tool": "shell", "callID": "c1",
                 "state": {"status": "completed",
                           "input": {"command": "pytest"},
                           "output": "5 passed"},
             })),
            ("a1-p4", "a1", "oc-tools-1", t0 + 4, t0 + 4,
             json.dumps({
                 "type": "tool", "tool": "write", "callID": "c2",
                 "state": {"status": "error",
                           "input": {"path": "/x"}},
             })),
            ("a1-p5", "a1", "oc-tools-1", t0 + 5, t0 + 5,
             json.dumps({
                 "type": "file",
                 "mime": "image/png",
                 "filename": "manifest.png",
                 "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA",
             })),
            ("a1-p6", "a1", "oc-tools-1", t0 + 6, t0 + 6,
             json.dumps({
                 "type": "patch",
                 "hash": "abc123",
                 "files": [
                     {"path": "src/app.py", "added": 3, "removed": 1},
                 ],
             })),
            ("a1-p7", "a1", "oc-tools-1", t0 + 7, t0 + 7,
             json.dumps({"type": "step-finish", "tokens": {"total": 1}})),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def fake_claude_session_with_ask(tmp_sessions_dir: Path) -> Path:
    """A Claude session with an AskUserQuestion call + answered tool_result.

    Mirrors the real wire shape: an assistant ``tool_use`` named
    ``AskUserQuestion`` carrying ``input.questions`` and a following
    user-role record holding the matching ``tool_result`` whose content
    is the human-readable ``"question"="answer"`` string.
    """
    session_id = "claude-ask-1"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-q" / f"{session_id}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "Plan the work"},
                "timestamp": "2026-06-14T10:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_ask1",
                            "name": "AskUserQuestion",
                            "input": {
                                "questions": [
                                    {
                                        "question": "Which approach?",
                                        "header": "Approach",
                                        "multiSelect": False,
                                        "options": [
                                            {"label": "Option A",
                                             "description": "first"},
                                            {"label": "Option B",
                                             "description": "second"},
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                },
                "timestamp": "2026-06-14T10:00:05Z",
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_ask1",
                            "content": (
                                'Your questions have been answered: '
                                '"Which approach?"="Option B". '
                                "You can now continue with these answers in mind."
                            ),
                        }
                    ],
                },
                "timestamp": "2026-06-14T10:00:10Z",
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_codex_session_with_ask(tmp_sessions_dir: Path) -> Path:
    """A Codex rollout with a request_user_input call + answered output.

    Codex stores the call as a ``function_call`` named
    ``request_user_input`` (args carry ``questions`` with per-question
    ``id``) and the answer as the matching ``function_call_output``
    whose ``output`` is ``{"answers": {"<id>": {"answers": [...]}}}``.
    """
    uuid = "codex-ask-1"
    jsonl = (
        tmp_sessions_dir
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "14"
        / f"rollout-2026-06-14T12-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "timestamp": "2026-06-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": uuid, "cwd": "/tmp/work"},
            },
            {
                "timestamp": "2026-06-14T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "request_user_input",
                    "call_id": "call_ask1",
                    "arguments": json.dumps(
                        {
                            "questions": [
                                {
                                    "id": "mode",
                                    "header": "Mode",
                                    "question": "Which mode?",
                                    "options": [
                                        {"label": "Fast", "description": "f"},
                                        {"label": "Safe", "description": "s"},
                                    ],
                                }
                            ]
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-06-14T12:00:08Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_ask1",
                    "output": json.dumps(
                        {"answers": {"mode": {"answers": ["Safe"]}}}
                    ),
                },
            },
        ],
    )
    return jsonl


@pytest.fixture
def fake_opencode_db_with_ask(tmp_sessions_dir: Path) -> Path:
    """OpenCode DB whose assistant message carries a ``question`` tool part.

    The question tool stores the offered questions in ``state.input`` and
    the chosen answers in ``state.metadata.answers`` (a list parallel to
    the questions; multi-select yields more than one label).
    """
    db_path = tmp_sessions_dir / "opencode_ask.db"
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
        "INSERT INTO session VALUES (?, NULL, ?, ?, ?)",
        ("oc-ask-1", "Ask session", 1_716_000_000_000, 1_716_000_500_000),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("aq", "oc-ask-1", 1_716_000_200_000, 1_716_000_200_000,
         json.dumps({"role": "assistant"})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        ("aq-p0", "aq", "oc-ask-1", 1_716_000_200_000, 1_716_000_200_000,
         json.dumps({
             "type": "tool",
             "tool": "question",
             "callID": "c1",
             "state": {
                 "status": "completed",
                 "input": {
                     "questions": [
                         {
                             "question": "Scope?",
                             "header": "Scope",
                             "options": [
                                 {"label": "Small", "description": "s"},
                                 {"label": "Big", "description": "b"},
                             ],
                         },
                         {
                             "question": "Extras?",
                             "header": "Extras",
                             "options": [
                                 {"label": "Tests", "description": "t"},
                                 {"label": "Docs", "description": "d"},
                             ],
                         },
                     ]
                 },
                 "output": (
                     'User has answered your questions: '
                     '"Scope?"="Small", "Extras?"="Tests | Docs".'
                 ),
                 "metadata": {
                     "answers": [["Small"], ["Tests", "Docs"]],
                     "truncated": False,
                 },
             },
         })),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def fake_antigravity_brain_with_transcript(tmp_sessions_dir: Path) -> Path:
    """A brain directory whose transcript_full.jsonl carries user/model records."""
    brain = tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / "ag-tools-1"
    (brain / ".system_generated" / "logs").mkdir(parents=True)
    transcript = brain / ".system_generated" / "logs" / "transcript_full.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": "2026-06-14T10:00:00Z",
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "Set up the lab",
            },
            {
                "timestamp": "2026-06-14T10:00:05Z",
                "source": "MODEL",
                "type": "MODEL_OUTPUT",
                "content": "Lab is ready",
            },
        ],
    )
    return brain


# ---------------------------------------------------------------------------
# Real-data probes (read-only, used by integration-style tests)
# ---------------------------------------------------------------------------


_REAL_CLAUDE_DIR = Path("~/.claude/projects").expanduser()
_REAL_CLAUDE_DESKTOP_DIR = Path(
    "~/.config/Claude/claude-code-sessions"
).expanduser()
_REAL_CODEX_DIR = Path("~/.codex/sessions").expanduser()
_REAL_OPENCODE_DB = Path("~/.local/share/opencode/opencode.db")
_REAL_PI_DIR = Path("~/.pi/agent/sessions").expanduser()
_REAL_ANTIGRAVITY_DIRS: List[Path] = [
    Path("~/.gemini/antigravity/brain").expanduser(),
    Path("~/.gemini/antigravity-cli/brain").expanduser(),
]


# These probes intentionally read the *real* user home.  Each one
# ``pytest.skip``s its requesting test when the host has no data, so the
# absence of local sessions can NEVER turn a run red — it only skips.
# Any test that takes one of these fixtures is auto-tagged ``@pytest.mark.host``
# (see ``pytest_collection_modifyitems`` below) and is therefore excluded
# from the hermetic CI job.  This is the single source of truth for the
# "host data missing -> skip, never fail" invariant.


@pytest.fixture(scope="session")
def real_claude_dir() -> Path:
    if not _REAL_CLAUDE_DIR.is_dir():
        pytest.skip("no real Claude sessions on this host")
    return _REAL_CLAUDE_DIR


@pytest.fixture(scope="session")
def real_claude_desktop_dir() -> Path:
    """The real Claude *Desktop* metadata root (F1.3), skip when absent."""
    if not _REAL_CLAUDE_DESKTOP_DIR.is_dir():
        pytest.skip("no real Claude Desktop session store on this host")
    return _REAL_CLAUDE_DESKTOP_DIR


@pytest.fixture
def frozen_claude_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Snapshot the REAL ``~/.claude/projects`` into a temp ``AI_R_HOME``.

    A byte-parity test that reads the *live* ``~/.claude`` twice (once per
    side) can flake: the vault mutates between the two reads — most acutely the
    session the test itself runs inside, which the harness is actively writing.
    This fixture copies the projects tree into an immutable temp home and points
    every parser there via ``AI_R_HOME``, so both sides of a comparison read
    identical frozen bytes.  Skips (never fails) when the host has no Claude
    data.  Auto-tagged ``host`` via :data:`_HOST_FIXTURES`.
    """
    import os
    import shutil

    if not _REAL_CLAUDE_DIR.is_dir():
        pytest.skip("no real Claude sessions on this host")
    dst = tmp_path / "frozen" / ".claude" / "projects"
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Hardlink the files instead of byte-copying: the vault is read-only for
    # these tests, so hardlinks give an immutable *view* at near-zero cost and
    # avoid a multi-hundred-MB copy.  Fall back to a real copy if the temp dir
    # is on a different filesystem (cross-device link → OSError).
    try:
        shutil.copytree(_REAL_CLAUDE_DIR, dst, copy_function=os.link)
    except OSError:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(_REAL_CLAUDE_DIR, dst)
    monkeypatch.setenv("AI_R_HOME", str(tmp_path / "frozen"))
    return dst


@pytest.fixture
def real_claude_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Let a host-marked test read the REAL ``~/.claude`` (not the fake home).

    The autouse ``_isolate_ai_r_home`` fixture points every parser at a fake
    temp ``AI_R_HOME`` — great for hermetic tests, but it means a test that
    merely requests ``real_claude_dir`` still reads NOTHING (the parser
    resolves ``AI_R_HOME`` first).  This fixture deletes ``AI_R_HOME`` so the
    parser falls back to the real ``~/.claude/projects``.  It runs AFTER the
    autouse fixture (it is explicitly requested), so its ``delenv`` wins.

    Skips (never fails) when the host has no Claude data.  Auto-tagged
    ``host`` via :data:`_HOST_FIXTURES`.
    """
    if not _REAL_CLAUDE_DIR.is_dir():
        pytest.skip("no real Claude sessions on this host")
    monkeypatch.delenv("AI_R_HOME", raising=False)
    return _REAL_CLAUDE_DIR


@pytest.fixture(scope="session")
def real_codex_dir() -> Path:
    if not _REAL_CODEX_DIR.is_dir():
        pytest.skip("no real Codex sessions on this host")
    return _REAL_CODEX_DIR


@pytest.fixture(scope="session")
def real_opencode_db() -> Path:
    if not _REAL_OPENCODE_DB.is_file():
        pytest.skip("no real OpenCode DB on this host")
    return _REAL_OPENCODE_DB


@pytest.fixture(scope="session")
def real_pi_dir() -> Path:
    if not _REAL_PI_DIR.is_dir():
        pytest.skip("no real Pi sessions on this host")
    return _REAL_PI_DIR


@pytest.fixture(scope="session")
def real_antigravity_root() -> Path:
    for root in _REAL_ANTIGRAVITY_DIRS:
        if root.is_dir() and any(root.iterdir()):
            return root
    pytest.skip("no real Antigravity brain dirs on this host")


# ---------------------------------------------------------------------------
# Sample fixtures copied to a writable location for parser-specific tests
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_sample_jsonl(tmp_path: Path) -> Path:
    src = Path(__file__).parent / "fixtures" / "claude_sample.jsonl"
    dest = tmp_path / "claude_sample.jsonl"
    shutil.copyfile(src, dest)
    return dest


@pytest.fixture
def codex_sample_jsonl(tmp_path: Path) -> Path:
    src = Path(__file__).parent / "fixtures" / "codex_sample.jsonl"
    dest = tmp_path / "codex_sample.jsonl"
    shutil.copyfile(src, dest)
    return dest


@pytest.fixture
def codex_event_msg_jsonl(tmp_path: Path) -> Path:
    src = Path(__file__).parent / "fixtures" / "codex_event_msg.jsonl"
    dest = tmp_path / "codex_event_msg.jsonl"
    shutil.copyfile(src, dest)
    return dest
