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
  next to ``project_dir`` / ``launch_surface``.
"""
from __future__ import annotations

from datetime import datetime, timezone

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
