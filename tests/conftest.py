"""Shared pytest fixtures for ai-r tests.

Fixtures are deterministic: every fixture that touches the filesystem
creates a temporary directory and never writes outside ``tmp_path`` or
``AI_R_HOME`` (the latter only when explicitly overridden).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator, List

import pytest


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------

# Markers that may be added later to the package's own pyproject
pytest_plugins: list[str] = []


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
_REAL_CODEX_DIR = Path("~/.codex/sessions").expanduser()
_REAL_OPENCODE_DB = Path("~/.local/share/opencode/opencode.db")
_REAL_PI_DIR = Path("~/.pi/agent/sessions").expanduser()
_REAL_ANTIGRAVITY_DIRS: List[Path] = [
    Path("~/.gemini/antigravity/brain").expanduser(),
    Path("~/.gemini/antigravity-cli/brain").expanduser(),
]


@pytest.fixture(scope="session")
def real_claude_dir() -> Path | None:
    return _REAL_CLAUDE_DIR if _REAL_CLAUDE_DIR.is_dir() else None


@pytest.fixture(scope="session")
def real_codex_dir() -> Path | None:
    return _REAL_CODEX_DIR if _REAL_CODEX_DIR.is_dir() else None


@pytest.fixture(scope="session")
def real_opencode_db() -> Path | None:
    return _REAL_OPENCODE_DB if _REAL_OPENCODE_DB.is_file() else None


@pytest.fixture(scope="session")
def real_pi_dir() -> Path | None:
    return _REAL_PI_DIR if _REAL_PI_DIR.is_dir() else None


@pytest.fixture(scope="session")
def real_antigravity_root() -> Path | None:
    for root in _REAL_ANTIGRAVITY_DIRS:
        if root.is_dir() and any(root.iterdir()):
            return root
    return None


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
