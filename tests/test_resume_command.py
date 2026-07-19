"""F2.2 resume-command tests: ``resume_command`` in the session summary.

Hermetic by construction: :class:`Session` objects are built in-memory
(no host vault, no CLI is ever executed — the field is text only).

Covers:

* the per-agent command shape (Claude / Codex / OpenCode / Pi) and the
  honest ``None`` cases (Antigravity, subagent sessions, reference-only
  Claude Desktop sessions);
* the ``cd <project_dir> && `` prefix when the project dir is known and
  its absence when it is not;
* shell-quoting of interpolated values (dir with a space);
* the MCP summary projection: ``resume_command`` is a top-level field
  next to ``project_dir`` / ``launch_surface``;
* the ``detect_current`` verb: the detected session's ``resume_command``
  is reported (honest ``None`` when identity is incomplete or the
  session store has no such session);
* the CLI ``ai-r list --json`` summary carries the same field.
"""
from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_r import cli as cli_module
from ai_r.mcp_server import _session_summary
from ai_r.parsers import AgentName, Session
from ai_r.resume import resume_command

_DATE = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)


def _session(**overrides) -> Session:
    base = dict(
        uuid="11111111-2222-3333-4444-555555555555",
        agent=AgentName.CLAUDE,
        title="t",
        date=_DATE,
        path="/data/.claude/projects/-home-u-dev-x/11111111.jsonl",
        message_count=3,
    )
    base.update(overrides)
    return Session(**base)


class TestClaude:
    def test_with_project_dir_cd_prefix(self) -> None:
        sess = _session(project_dir="/home/u/dev/x")
        assert resume_command(sess) == (
            "cd /home/u/dev/x && claude --resume "
            "11111111-2222-3333-4444-555555555555"
        )

    def test_without_project_dir_bare_command(self) -> None:
        # No dir signal → bare command (only works from the project dir,
        # documented) — better than fabricating a directory.
        sess = _session(project_dir=None)
        assert resume_command(sess) == (
            "claude --resume 11111111-2222-3333-4444-555555555555"
        )

    def test_reference_only_desktop_session_is_none(self) -> None:
        # Transcript deleted, path points at the Desktop metadata JSON
        # (F1.3 reference-only) → nothing to resume.
        sess = _session(
            path="/data/.config/Claude/claude-code-sessions/d/w/local_1.json",
            message_count=0,
            project_dir="/home/u/dev/x",
        )
        assert resume_command(sess) is None

    def test_project_dir_with_space_is_quoted(self) -> None:
        sess = _session(project_dir="/home/u/my project")
        cmd = resume_command(sess)
        assert cmd is not None and cmd.startswith("cd '/home/u/my project' && ")


class TestCodex:
    def test_resume_by_uuid(self) -> None:
        sess = _session(
            agent=AgentName.CODEX,
            uuid="abc-123",
            path="/data/.codex/sessions/2026/07/01/rollout-abc-123.jsonl",
            project_dir="/home/u/dev/x",
        )
        assert resume_command(sess) == "cd /home/u/dev/x && codex resume abc-123"

    def test_without_project_dir(self) -> None:
        sess = _session(agent=AgentName.CODEX, uuid="abc-123", project_dir=None)
        assert resume_command(sess) == "codex resume abc-123"


class TestOpenCode:
    def test_session_flag_by_id(self) -> None:
        sess = _session(
            agent=AgentName.OPENCODE,
            uuid="ses_123",
            path="/data/.local/share/opencode/opencode.db",
            project_dir="/home/u/dev/x",
        )
        assert resume_command(sess) == (
            "cd /home/u/dev/x && opencode --session ses_123"
        )


class TestPi:
    def test_session_flag_by_path(self) -> None:
        # Pi's ``--session`` takes a path|id; the recorded file path is
        # unambiguous from any cwd → the path form is emitted.
        sess = _session(
            agent=AgentName.PI,
            uuid="pi-1",
            path="/data/.pi/agent/sessions/--home--u--dev--x/pi-1.jsonl",
            project_dir="/home/u/dev/x",
        )
        assert resume_command(sess) == (
            "cd /home/u/dev/x && pi --session "
            "/data/.pi/agent/sessions/--home--u--dev--x/pi-1.jsonl"
        )


class TestNoneCases:
    def test_antigravity_always_none(self) -> None:
        # IDE brain dirs have no CLI resume verb — absence is honest.
        sess = _session(
            agent=AgentName.ANTIGRAVITY,
            path="/data/.gemini/antigravity/brain/uuid-1",
            project_dir=None,
        )
        assert resume_command(sess) is None

    def test_subagent_kind_is_none(self) -> None:
        sess = _session(kind="subagent", project_dir="/home/u/dev/x")
        assert resume_command(sess) is None

    def test_parent_uuid_is_none(self) -> None:
        sess = _session(parent_uuid="parent-1", project_dir="/home/u/dev/x")
        assert resume_command(sess) is None


class TestSummaryProjection:
    def test_top_level_field_next_to_origin_fields(self) -> None:
        sess = _session(project_dir="/home/u/dev/x", launch_surface="claude-cli")
        summary = _session_summary(sess)
        assert summary["resume_command"] == (
            "cd /home/u/dev/x && claude --resume "
            "11111111-2222-3333-4444-555555555555"
        )
        assert summary["project_dir"] == "/home/u/dev/x"

    def test_null_is_projected_not_omitted(self) -> None:
        sess = _session(agent=AgentName.ANTIGRAVITY, path="/brain/u1")
        summary = _session_summary(sess)
        assert "resume_command" in summary
        assert summary["resume_command"] is None


# ---------------------------------------------------------------------------
# detect_current + CLI surface (hermetic: fake AI_R_HOME store)
# ---------------------------------------------------------------------------

_RESUMABLE_UUID = "aefa0001-2222-4333-8444-555555555555"


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


@pytest.fixture
def claude_resumable_session(tmp_sessions_dir: Path) -> str:
    """A Claude session in the fake store with a record-level ``cwd``.

    The uuid is UUID-shaped so the ``detect_current`` cascade accepts it;
    the ``cwd`` gives a deterministic ``project_dir`` → a deterministic
    ``cd`` prefix in the expected resume command.
    """
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-r"
        / f"{_RESUMABLE_UUID}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
            "timestamp": "2026-07-01T10:00:00Z",
            "sessionId": _RESUMABLE_UUID,
            "cwd": "/tmp/work-x",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
            "timestamp": "2026-07-01T10:00:05Z",
            "sessionId": _RESUMABLE_UUID,
        },
    ]
    with jsonl.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return _RESUMABLE_UUID


class TestDetectCurrent:
    def test_detected_session_carries_resume_command(
        self,
        _clean_detect_env: None,
        monkeypatch: pytest.MonkeyPatch,
        claude_resumable_session: str,
    ) -> None:
        from ai_r.events import detect_current

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", claude_resumable_session)
        r = detect_current()
        assert r["session_id"] == claude_resumable_session
        # Expected literal from the spec (docs/methods.md, Resume command):
        # cd <project_dir> && claude --resume <uuid>.
        assert r["resume_command"] == (
            f"cd /tmp/work-x && claude --resume {claude_resumable_session}"
        )

    def test_none_when_no_session_detected(
        self, _clean_detect_env: None
    ) -> None:
        from ai_r.events import detect_current

        r = detect_current()
        assert "resume_command" in r
        assert r["resume_command"] is None

    def test_none_when_session_unresolvable(
        self, _clean_detect_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ai_r.events import detect_current

        # A detected id with no transcript in the store → honest None,
        # no crash, nothing fabricated.
        monkeypatch.setenv(
            "CLAUDE_CODE_SESSION_ID",
            "dead0001-2222-4333-8444-555555555555",
        )
        assert detect_current()["resume_command"] is None


class TestCliList:
    def _run(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = cli_module.main(argv)
        return rc, stdout.getvalue()

    def test_list_json_carries_resume_command(
        self, claude_resumable_session: str
    ) -> None:
        rc, out = self._run(["list", "--agent", "claude", "--json"])
        assert rc == 0
        payload = json.loads(out)
        rows = {row["uuid"]: row for row in payload}
        assert rows[claude_resumable_session]["resume_command"] == (
            f"cd /tmp/work-x && claude --resume {claude_resumable_session}"
        )

    def test_list_json_null_is_projected_not_omitted(
        self, tmp_sessions_dir: Path
    ) -> None:
        # A subagent (sidechain) session is never resumable → null in JSON.
        jsonl = (
            tmp_sessions_dir / ".claude" / "projects" / "proj-r"
            / "sidechain-1.jsonl"
        )
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "user",
            "message": {"role": "user", "content": "sub task"},
            "timestamp": "2026-07-01T10:00:00Z",
            "sessionId": "sidechain-1",
            "isSidechain": True,
        }
        jsonl.write_text(json.dumps(record) + "\n", encoding="utf-8")
        rc, out = self._run(["list", "--agent", "claude", "--json"])
        assert rc == 0
        payload = json.loads(out)
        rows = {row["uuid"]: row for row in payload}
        assert "resume_command" in rows["sidechain-1"]
        assert rows["sidechain-1"]["resume_command"] is None
