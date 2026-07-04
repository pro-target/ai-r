"""F1.4 session-origin tests: ``project_dir`` + ``launch_surface``.

Covers, hermetically (fixtures only, no host data):

* the per-parser signal extraction — Claude (transcript ``cwd``, Desktop
  metadata, verified slug decode), Codex (``session_meta`` ``cwd`` +
  ``originator``), OpenCode (``session.directory`` column incl. the
  legacy-schema fallback), Pi (header ``cwd``), Antigravity (brain-root
  launch surface, honest ``project_dir=None``);
* the MCP summary projection (top-level fields next to ``kind`` /
  ``parent_uuid``, plus the ``extra`` passthrough);
* the ``project_dir`` filter on ``list_sessions`` and the ``query`` verb
  (exact-or-descendant, path-boundary aware, fail-loud validation).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_r.mcp_server import _session_summary, list_sessions, query
from ai_r.parsers import AgentName, Session, antigravity, claude, codex, opencode, pi
from ai_r.parsers._common import project_dir_matches
from ai_r.parsers.claude import _project_dir_from_slug


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# project_dir_matches — the filter's semantics SSOT
# ---------------------------------------------------------------------------


class TestProjectDirMatches:
    def test_exact_match(self) -> None:
        assert project_dir_matches("/home/u/dev/x", "/home/u/dev/x")

    def test_descendant_matches(self) -> None:
        assert project_dir_matches("/home/u/dev/x/sub/dir", "/home/u/dev/x")

    def test_path_boundary_no_sibling_leak(self) -> None:
        # /home/u/dev/ai must NOT match an ai-r session (prefix != subpath).
        assert not project_dir_matches("/home/u/dev/ai-r", "/home/u/dev/ai")

    def test_parent_does_not_match(self) -> None:
        assert not project_dir_matches("/home/u/dev", "/home/u/dev/x")

    def test_trailing_slashes_ignored(self) -> None:
        assert project_dir_matches("/home/u/dev/x/", "/home/u/dev/x")
        assert project_dir_matches("/home/u/dev/x", "/home/u/dev/x/")

    def test_absent_signal_never_matches(self) -> None:
        assert not project_dir_matches(None, "/home/u/dev/x")
        assert not project_dir_matches("", "/home/u/dev/x")

    def test_root_filter_matches_everything_recorded(self) -> None:
        assert project_dir_matches("/home/u", "/")
        assert project_dir_matches("/", "/")


# ---------------------------------------------------------------------------
# Claude — transcript cwd, slug-decode fallback, Desktop overlay
# ---------------------------------------------------------------------------


def _claude_records(cwd: str | None) -> list[dict]:
    user: dict = {
        "type": "user",
        "timestamp": "2026-07-01T10:00:00Z",
        "message": {"content": "hello origin"},
    }
    assistant: dict = {
        "type": "assistant",
        "timestamp": "2026-07-01T10:00:05Z",
        "message": {"content": [{"type": "text", "text": "hi"}]},
    }
    if cwd is not None:
        user["cwd"] = cwd
        assistant["cwd"] = cwd
    return [user, assistant]


class TestClaudeOrigin:
    def test_record_cwd_becomes_project_dir(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _write_jsonl(
            projects / "-home-u-dev-x" / "s1.jsonl",
            _claude_records("/home/u/dev/x"),
        )
        [session] = claude.list_sessions(base_dir=str(projects))
        assert session.project_dir == "/home/u/dev/x"
        assert session.launch_surface == "claude-cli"

    def test_no_cwd_unresolvable_slug_gives_none(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _write_jsonl(
            projects / "-no-such-root-anywhere" / "s2.jsonl",
            _claude_records(None),
        )
        [session] = claude.list_sessions(base_dir=str(projects))
        assert session.project_dir is None  # honest absence, not a guess
        assert session.launch_surface == "claude-cli"

    def test_no_cwd_slug_decodes_against_filesystem(
        self, tmp_path: Path
    ) -> None:
        # Real directory containing a dash: naive '-'→'/' decode would
        # produce .../dev/ai/r; the verified decode must recover dev/ai-r.
        real = tmp_path / "dev" / "ai-r"
        real.mkdir(parents=True)
        slug = "-" + str(real).lstrip("/").replace("/", "-")
        projects = tmp_path / "projects"
        _write_jsonl(projects / slug / "s3.jsonl", _claude_records(None))
        [session] = claude.list_sessions(base_dir=str(projects))
        assert session.project_dir == str(real)

    def test_slug_decode_declines_ambiguity_it_cannot_verify(
        self, tmp_path: Path
    ) -> None:
        assert _project_dir_from_slug("not-a-slug") is None
        assert _project_dir_from_slug("-") is None
        assert _project_dir_from_slug("--double") is None

    def test_desktop_enrichment_flips_launch_surface(
        self, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        _write_jsonl(
            projects / "-home-u-proj" / "s4.jsonl",
            _claude_records("/home/u/proj"),
        )
        desktop = tmp_path / "desktop"
        meta_dir = desktop / "device" / "workspace"
        meta_dir.mkdir(parents=True)
        (meta_dir / "local_a.json").write_text(
            json.dumps(
                {
                    "sessionId": "local_a",
                    "cliSessionId": "s4",
                    "title": "Desktop title",
                    "cwd": "/home/u/other",  # transcript cwd must win
                    "lastActivityAt": 1_782_864_000_000,
                }
            ),
            encoding="utf-8",
        )
        [session] = claude.list_sessions(
            base_dir=str(projects), desktop_dir=str(desktop)
        )
        assert session.launch_surface == "claude-desktop"
        assert session.project_dir == "/home/u/proj"

    def test_desktop_only_session_uses_metadata_cwd(
        self, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()
        desktop = tmp_path / "desktop"
        meta_dir = desktop / "device" / "workspace"
        meta_dir.mkdir(parents=True)
        (meta_dir / "local_b.json").write_text(
            json.dumps(
                {
                    "sessionId": "local_b",
                    "cliSessionId": "ghost-1",
                    "title": "Ghost",
                    "cwd": "/home/u/ghost-proj",
                    "lastActivityAt": 1_782_864_000_000,
                }
            ),
            encoding="utf-8",
        )
        [session] = claude.list_sessions(
            base_dir=str(projects), desktop_dir=str(desktop)
        )
        assert session.project_dir == "/home/u/ghost-proj"
        assert session.launch_surface == "claude-desktop"

    def test_desktop_cwd_fills_absent_transcript_signal(
        self, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        _write_jsonl(
            projects / "-no-such-root-anywhere" / "s5.jsonl",
            _claude_records(None),
        )
        desktop = tmp_path / "desktop"
        meta_dir = desktop / "device" / "workspace"
        meta_dir.mkdir(parents=True)
        (meta_dir / "local_c.json").write_text(
            json.dumps(
                {
                    "sessionId": "local_c",
                    "cliSessionId": "s5",
                    "title": "Filled",
                    "cwd": "/home/u/from-desktop",
                    "lastActivityAt": 1_782_864_000_000,
                }
            ),
            encoding="utf-8",
        )
        [session] = claude.list_sessions(
            base_dir=str(projects), desktop_dir=str(desktop)
        )
        assert session.project_dir == "/home/u/from-desktop"


# ---------------------------------------------------------------------------
# Codex — session_meta cwd + originator
# ---------------------------------------------------------------------------


class TestCodexOrigin:
    def _write_rollout(
        self, root: Path, uuid: str, payload_extra: dict
    ) -> None:
        payload = {
            "id": uuid,
            "timestamp": "2026-07-01T10:00:00Z",
            **payload_extra,
        }
        _write_jsonl(
            root / "2026" / "07" / "01" / f"rollout-2026-07-01-{uuid}.jsonl",
            [
                {
                    "timestamp": "2026-07-01T10:00:00Z",
                    "type": "session_meta",
                    "payload": payload,
                },
                {
                    "timestamp": "2026-07-01T10:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "text", "text": "codex origin"}],
                    },
                },
            ],
        )

    def test_cwd_and_originator_surface(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        self._write_rollout(
            root,
            "cdx-1",
            {"cwd": "/home/u/dev/x", "originator": "codex_vscode"},
        )
        [session] = codex.list_sessions(base_dir=str(root))
        assert session.project_dir == "/home/u/dev/x"
        assert session.launch_surface == "codex_vscode"  # raw, verbatim

    def test_missing_signals_stay_none(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        self._write_rollout(root, "cdx-2", {})
        [session] = codex.list_sessions(base_dir=str(root))
        assert session.project_dir is None
        assert session.launch_surface is None


# ---------------------------------------------------------------------------
# OpenCode — session.directory column + legacy-schema fallback
# ---------------------------------------------------------------------------


_OC_SCHEMA_COMMON = """
    CREATE TABLE message (
        id           TEXT PRIMARY KEY,
        session_id   TEXT NOT NULL,
        time_created INTEGER NOT NULL,
        time_updated INTEGER NOT NULL,
        data         TEXT
    );
"""


class TestOpenCodeOrigin:
    def _db_with_directory(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE session (
                id           TEXT PRIMARY KEY,
                parent_id    TEXT,
                title        TEXT,
                directory    TEXT,
                time_created INTEGER,
                time_updated INTEGER
            );
            """
            + _OC_SCHEMA_COMMON
        )
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            ("oc-dir-1", None, "With dir", "/home/u/dev/x",
             1_716_000_000_000, 1_716_000_500_000),
        )
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            ("oc-dir-2", None, "Empty dir", "",
             1_716_000_000_000, 1_716_000_400_000),
        )
        conn.commit()
        conn.close()

    def _db_legacy(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE session (
                id           TEXT PRIMARY KEY,
                parent_id    TEXT,
                title        TEXT,
                time_created INTEGER,
                time_updated INTEGER
            );
            """
            + _OC_SCHEMA_COMMON
        )
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?)",
            ("oc-old-1", None, "Legacy", 1_716_000_000_000,
             1_716_000_500_000),
        )
        conn.commit()
        conn.close()

    def test_directory_column_becomes_project_dir(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "opencode.db"
        self._db_with_directory(db)
        sessions = {
            s.uuid: s for s in opencode.list_sessions(override=str(db))
        }
        assert sessions["oc-dir-1"].project_dir == "/home/u/dev/x"
        assert sessions["oc-dir-1"].launch_surface is None  # no signal
        assert sessions["oc-dir-2"].project_dir is None  # empty ≠ signal

    def test_legacy_schema_degrades_to_none_not_crash(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "opencode.db"
        self._db_legacy(db)
        # NB: the opencode parser scans every discoverable DB (a real
        # host DB may coexist on non-hermetic runs) — assert on OUR
        # session specifically, not on the total count.
        matches = [
            s
            for s in opencode.list_sessions(override=str(db))
            if s.uuid == "oc-old-1"
        ]
        assert len(matches) == 1  # enumeration survived the legacy schema
        assert matches[0].project_dir is None
        read_back = opencode.read_session("oc-old-1", override=str(db))
        assert read_back.project_dir is None
        assert opencode.session_exists("oc-old-1", override=str(db))


# ---------------------------------------------------------------------------
# Pi — header cwd; no launch-surface signal
# ---------------------------------------------------------------------------


class TestPiOrigin:
    def test_header_cwd_becomes_project_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        _write_jsonl(
            root / "--home-u-dev-x--" / "2026-07-01T10-00-00-000Z_pi-1.jsonl",
            [
                {
                    "type": "session",
                    "version": 3,
                    "id": "pi-1",
                    "timestamp": "2026-07-01T10:00:00.000Z",
                    "cwd": "/home/u/dev/x",
                },
                {
                    "type": "message",
                    "timestamp": "2026-07-01T10:00:02.000Z",
                    "message": {"role": "user", "content": "pi origin"},
                },
            ],
        )
        [session] = pi.list_sessions(base_dir=str(root))
        assert session.project_dir == "/home/u/dev/x"
        assert session.launch_surface is None  # format has no signal

    def test_headerless_session_stays_none(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        _write_jsonl(
            root / "misc" / "2026-07-01T11-00-00-000Z_pi-2.jsonl",
            [
                {
                    "type": "session",
                    "version": 3,
                    "id": "pi-2",
                    "timestamp": "2026-07-01T11:00:00.000Z",
                },
                {
                    "type": "message",
                    "timestamp": "2026-07-01T11:00:02.000Z",
                    "message": {"role": "user", "content": "no cwd here"},
                },
            ],
        )
        [session] = pi.list_sessions(base_dir=str(root))
        assert session.project_dir is None


# ---------------------------------------------------------------------------
# Antigravity — brain-root launch surface; NO project_dir signal
# ---------------------------------------------------------------------------


def _write_brain(brain: Path) -> None:
    logs = brain / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    _write_jsonl(
        logs / "overview.txt",
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "timestamp": "2026-07-01T10:00:00Z",
                "content": "<USER_REQUEST>origin check</USER_REQUEST>",
            }
        ],
    )


class TestAntigravityOrigin:
    def test_ide_and_cli_roots_distinguished(
        self, tmp_sessions_dir: Path
    ) -> None:
        gemini = tmp_sessions_dir / ".gemini"
        ide_brain = (
            gemini / "antigravity" / "brain"
            / "11111111-2222-3333-4444-555555555555"
        )
        cli_brain = (
            gemini / "antigravity-cli" / "brain"
            / "66666666-7777-8888-9999-000000000000"
        )
        _write_brain(ide_brain)
        _write_brain(cli_brain)
        by_uuid = {s.uuid: s for s in antigravity.list_sessions()}
        ide = by_uuid["11111111-2222-3333-4444-555555555555"]
        cli = by_uuid["66666666-7777-8888-9999-000000000000"]
        assert ide.launch_surface == "antigravity-ide"
        assert cli.launch_surface == "antigravity-cli"
        # No structured cwd/directory field exists in the format —
        # project_dir must stay honestly absent, never guessed from text.
        assert ide.project_dir is None
        assert cli.project_dir is None

    def test_arbitrary_base_dir_yields_no_surface(
        self, tmp_path: Path
    ) -> None:
        brain = tmp_path / "some-fixture-root" / "brain-x"
        _write_brain(brain)
        [session] = antigravity.list_sessions(
            base_dir=str(tmp_path / "some-fixture-root")
        )
        assert session.launch_surface is None  # layout not recognisable


# ---------------------------------------------------------------------------
# MCP summary projection
# ---------------------------------------------------------------------------


class TestSessionSummary:
    def _session(self, **overrides: object) -> Session:
        base: dict = dict(
            uuid="s-1",
            agent=AgentName.CLAUDE,
            title="t",
            date=datetime(2026, 7, 1, tzinfo=timezone.utc),
            path="/tmp/s-1.jsonl",
            message_count=2,
        )
        base.update(overrides)
        return Session(**base)  # type: ignore[arg-type]

    def test_origin_fields_are_top_level(self) -> None:
        summary = _session_summary(
            self._session(
                project_dir="/home/u/dev/x", launch_surface="claude-cli"
            )
        )
        assert summary["project_dir"] == "/home/u/dev/x"
        assert summary["launch_surface"] == "claude-cli"
        # Next to the existing first-class fields, same style.
        assert "kind" in summary and "parent_uuid" in summary

    def test_absent_signals_are_null_not_missing(self) -> None:
        summary = _session_summary(self._session())
        assert summary["project_dir"] is None
        assert summary["launch_surface"] is None

    def test_extra_bag_is_passed_through(self) -> None:
        summary = _session_summary(
            self._session(extra={"source_root": "desktop", "cwd": "/x"})
        )
        assert summary["extra"] == {"source_root": "desktop", "cwd": "/x"}

    def test_empty_extra_is_omitted(self) -> None:
        summary = _session_summary(self._session())
        assert "extra" not in summary


# ---------------------------------------------------------------------------
# MCP filters — list_sessions(project_dir=...) and query(project_dir=...)
# ---------------------------------------------------------------------------


@pytest.fixture
def two_claude_projects(tmp_sessions_dir: Path) -> None:
    """Two Claude sessions in different project dirs (env-resolved root)."""
    projects = tmp_sessions_dir / ".claude" / "projects"
    _write_jsonl(
        projects / "-home-u-dev-x" / "in-x.jsonl",
        _claude_records("/home/u/dev/x"),
    )
    _write_jsonl(
        projects / "-home-u-dev-x-sub" / "in-x-sub.jsonl",
        _claude_records("/home/u/dev/x/sub"),
    )
    _write_jsonl(
        projects / "-home-u-dev-xy" / "in-xy.jsonl",
        _claude_records("/home/u/dev/xy"),
    )


class TestListSessionsProjectDirFilter:
    def test_exact_and_descendant_match(
        self, two_claude_projects: None
    ) -> None:
        result = list_sessions(agent="claude", project_dir="/home/u/dev/x")
        uuids = {s["uuid"] for s in result["sessions"]}
        # in-x (exact) + in-x-sub (descendant); in-xy is a sibling prefix
        # that must NOT leak across the path boundary.
        assert uuids == {"in-x", "in-x-sub"}
        for s in result["sessions"]:
            assert s["project_dir"].startswith("/home/u/dev/x")

    def test_no_match_carries_diagnostics_with_filter_echo(
        self, two_claude_projects: None
    ) -> None:
        result = list_sessions(agent="claude", project_dir="/nowhere")
        assert result["total"] == 0
        assert result["diagnostics"]["filters"]["project_dir"] == "/nowhere"

    def test_empty_filter_fails_loud(self) -> None:
        result = list_sessions(agent="claude", project_dir="   ")
        assert result["error"] == "invalid_argument"

    def test_composes_with_other_filters(
        self, two_claude_projects: None
    ) -> None:
        result = list_sessions(
            agent="claude",
            project_dir="/home/u/dev/x",
            kind="subagent",
        )
        assert result["total"] == 0  # AND-composition: no subagents here


class TestQueryProjectDirFilter:
    def test_events_scoped_to_project(self, two_claude_projects: None) -> None:
        result = query(
            type="user_turn", agent="claude", project_dir="/home/u/dev/xy"
        )
        assert result["count"] == 1
        assert {e["session_id"] for e in result["events"]} == {"in-xy"}

    def test_descendants_included_boundary_respected(
        self, two_claude_projects: None
    ) -> None:
        result = query(
            type="user_turn", agent="claude", project_dir="/home/u/dev/x"
        )
        assert {e["session_id"] for e in result["events"]} == {
            "in-x",
            "in-x-sub",
        }

    def test_empty_filter_fails_loud(self) -> None:
        result = query(agent="claude", project_dir="")
        assert result["error"] == "invalid_argument"

    def test_no_match_echoes_filter_in_diagnostics(
        self, two_claude_projects: None
    ) -> None:
        result = query(agent="claude", project_dir="/nowhere")
        assert result["count"] == 0
        assert result["diagnostics"]["filters"]["project_dir"] == "/nowhere"
