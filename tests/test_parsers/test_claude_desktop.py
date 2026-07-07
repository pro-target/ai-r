"""Tests for the Claude Desktop metadata overlay (F1.3).

Claude Desktop keeps a per-session metadata store
(``~/.config/Claude/claude-code-sessions/<device>/<workspace>/local_*.json``)
that references the backing CLI transcript via ``cliSessionId``.  The
parser scans BOTH roots, deduplicates by uuid and marks the origin in
``extra["source_root"]`` (``"cli"`` | ``"desktop"``).

Hermetic: every test below builds both roots under ``tmp_path``; the
single host test takes ``real_claude_desktop_dir`` and is auto-tagged
``@pytest.mark.host`` (skips when the host has no Desktop store).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_r.parsers import AgentName, claude


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_CLI_UUID = "11111111-aaaa-bbbb-cccc-000000000001"
_GHOST_UUID = "22222222-aaaa-bbbb-cccc-000000000002"
_CLI_ONLY_UUID = "33333333-aaaa-bbbb-cccc-000000000003"

# 2026-07-01T00:00:00Z in epoch milliseconds (Desktop timestamp unit).
_MS_2026_07_01 = 1_782_864_000_000


def _write_cli_session(projects: Path, uuid: str, text: str) -> Path:
    """One minimal CLI JSONL transcript with a single user turn."""
    slug_dir = projects / "-home-user-proj"
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{uuid}.jsonl"
    record = {
        "type": "user",
        "timestamp": "2026-07-01T10:00:00Z",
        "message": {"content": text},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _write_desktop_meta(
    store: Path,
    cli_session_id: str | None,
    title: str,
    name: str = "local_meta.json",
    **overrides: object,
) -> Path:
    """One Desktop metadata JSON in the observed two-level layout."""
    ws_dir = store / "device-uuid" / "workspace-uuid"
    ws_dir.mkdir(parents=True, exist_ok=True)
    record: dict = {
        "sessionId": f"local_{name.removesuffix('.json')}",
        "title": title,
        "cwd": "/home/user/proj",
        "originCwd": "/home/user/proj",
        "createdAt": _MS_2026_07_01,
        "lastActivityAt": _MS_2026_07_01 + 3_600_000,
        "isArchived": False,
        "model": "claude-fable-5",
    }
    if cli_session_id is not None:
        record["cliSessionId"] = cli_session_id
    record.update(overrides)
    path = ws_dir / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


@pytest.fixture
def dual_roots(tmp_path: Path) -> tuple[str, str]:
    """CLI + Desktop roots covering all three origin cases.

    * ``_CLI_UUID``      — transcript in CLI root **and** Desktop metadata.
    * ``_GHOST_UUID``    — Desktop metadata only (transcript gone).
    * ``_CLI_ONLY_UUID`` — transcript only.
    """
    projects = tmp_path / ".claude" / "projects"
    store = tmp_path / ".config" / "Claude" / "claude-code-sessions"
    _write_cli_session(projects, _CLI_UUID, "hello from cli")
    _write_cli_session(projects, _CLI_ONLY_UUID, "cli only session")
    _write_desktop_meta(
        store, _CLI_UUID, "Desktop shiny title", name="local_one.json"
    )
    _write_desktop_meta(
        store, _GHOST_UUID, "Ghost desktop session", name="local_two.json"
    )
    return str(projects), str(store)


# ---------------------------------------------------------------------------
# Dedup + origin marking
# ---------------------------------------------------------------------------


def test_dedup_one_session_per_uuid(dual_roots: tuple[str, str]) -> None:
    base, desktop = dual_roots
    sessions = claude.list_sessions(base_dir=base, desktop_dir=desktop)
    uuids = [s.uuid for s in sessions]
    assert len(uuids) == len(set(uuids)) == 3
    assert set(uuids) == {_CLI_UUID, _GHOST_UUID, _CLI_ONLY_UUID}


def test_desktop_title_wins_and_cli_title_kept(
    dual_roots: tuple[str, str],
) -> None:
    base, desktop = dual_roots
    by_uuid = {
        s.uuid: s
        for s in claude.list_sessions(base_dir=base, desktop_dir=desktop)
    }
    merged = by_uuid[_CLI_UUID]
    assert merged.title == "Desktop shiny title"
    assert merged.extra["cli_title"] == "hello from cli"
    assert merged.extra["source_root"] == "desktop"
    # CLI-derived fields survive the overlay.
    assert merged.extra["project_slug"] == "-home-user-proj"
    assert merged.path.endswith(f"{_CLI_UUID}.jsonl")
    assert merged.message_count == 1


def test_cli_only_session_marked_cli(dual_roots: tuple[str, str]) -> None:
    base, desktop = dual_roots
    by_uuid = {
        s.uuid: s
        for s in claude.list_sessions(base_dir=base, desktop_dir=desktop)
    }
    assert by_uuid[_CLI_ONLY_UUID].extra["source_root"] == "cli"
    assert "cli_title" not in by_uuid[_CLI_ONLY_UUID].extra


def test_list_sessions_sorts_mixed_cli_and_ghost_dates(
    dual_roots: tuple[str, str],
) -> None:
    # Regression: transcript-derived dates used to come out naive (the
    # ``raw[:23]`` truncation cut the tz suffix BEFORE the ``Z`` replace),
    # while desktop-only ghosts carry aware epoch dates — the final
    # ``sessions.sort(key=s.date)`` then raised ``TypeError``.  Every date
    # must now be tz-aware and the list sorted newest-first.
    base, desktop = dual_roots
    sessions = claude.list_sessions(base_dir=base, desktop_dir=desktop)
    assert len(sessions) == 3
    dates = [s.date for s in sessions]
    assert all(d.tzinfo is not None for d in dates)
    assert dates == sorted(dates, reverse=True)


def test_session_stats_since_until_over_mixed_corpus(
    dual_roots: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression companion: the since/until inventory filter must keep
    # working over a corpus that mixes transcript-derived dates and
    # desktop-ghost epoch dates (all tz-aware after the fix).
    from ai_r.session_stats import session_stats

    monkeypatch.setenv("AI_R_HOME", str(tmp_path))
    stats = session_stats(
        agent="claude", since="2026-07-01", until="2026-07-01",
        group_by="date",
    )
    assert stats["totals"]["sessions"] == 3
    assert [g["group"] for g in stats["groups"]] == ["2026-07-01"]

    excluded = session_stats(agent="claude", since="2026-07-02")
    assert excluded["totals"]["sessions"] == 0


def test_desktop_only_session_is_reference_only(
    dual_roots: tuple[str, str],
) -> None:
    base, desktop = dual_roots
    by_uuid = {
        s.uuid: s
        for s in claude.list_sessions(base_dir=base, desktop_dir=desktop)
    }
    ghost = by_uuid[_GHOST_UUID]
    assert ghost.agent is AgentName.CLAUDE
    assert ghost.title == "Ghost desktop session"
    assert ghost.extra["source_root"] == "desktop"
    assert ghost.message_count == 0
    assert ghost.path.endswith(".json")
    # Epoch-ms lastActivityAt → aware UTC datetime.
    assert ghost.date == datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)
    assert ghost.extra["cwd"] == "/home/user/proj"
    assert ghost.extra["project_slug"] == "-home-user-proj"


# ---------------------------------------------------------------------------
# Roots, participation rule, resilience
# ---------------------------------------------------------------------------


def test_source_roots_participation_rule(tmp_path: Path) -> None:
    # No explicit roots → both env-resolved (autouse AI_R_HOME isolation).
    assert len(claude.source_roots()) == 2
    # Explicit base_dir alone pins the scan to the CLI root (hermetic).
    assert claude.source_roots(base_dir=str(tmp_path)) == [str(tmp_path)]
    # Explicit desktop_dir re-enables the overlay.
    both = claude.source_roots(
        base_dir=str(tmp_path), desktop_dir=str(tmp_path / "d")
    )
    assert both == [str(tmp_path), str(tmp_path / "d")]


def test_env_resolution_via_ai_r_home(
    dual_roots: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Desktop root must honour AI_R_HOME exactly like the CLI root does
    # (hermetic runs swap HOME): no-arg calls see BOTH fixture roots.
    monkeypatch.setenv("AI_R_HOME", str(tmp_path))
    roots = claude.source_roots()
    assert roots[0] == str(tmp_path / ".claude" / "projects")
    assert roots[1] == str(
        tmp_path / ".config" / "Claude" / "claude-code-sessions"
    )
    sessions = claude.list_sessions()
    assert {s.uuid for s in sessions} == {
        _CLI_UUID,
        _GHOST_UUID,
        _CLI_ONLY_UUID,
    }


def test_missing_desktop_root_is_skipped(tmp_path: Path) -> None:
    projects = tmp_path / ".claude" / "projects"
    _write_cli_session(projects, _CLI_ONLY_UUID, "cli only session")
    sessions = claude.list_sessions(
        base_dir=str(projects),
        desktop_dir=str(tmp_path / "does-not-exist"),
    )
    assert [s.uuid for s in sessions] == [_CLI_ONLY_UUID]


def test_explicit_base_dir_alone_ignores_desktop(
    dual_roots: tuple[str, str],
) -> None:
    base, _desktop = dual_roots
    # Legacy call shape: only base_dir → CLI root only, no overlay.
    sessions = claude.list_sessions(base_dir=base)
    assert {s.uuid for s in sessions} == {_CLI_UUID, _CLI_ONLY_UUID}
    assert all(s.extra["source_root"] == "cli" for s in sessions)


def test_malformed_metadata_skipped(tmp_path: Path) -> None:
    store = tmp_path / "desktop"
    ws = store / "d" / "w"
    ws.mkdir(parents=True)
    (ws / "broken.json").write_text("{not json", encoding="utf-8")
    (ws / "list.json").write_text("[1, 2]", encoding="utf-8")
    (ws / "no_id.json").write_text(
        json.dumps({"title": "no ids here"}), encoding="utf-8"
    )
    projects = tmp_path / ".claude" / "projects"
    _write_cli_session(projects, _CLI_ONLY_UUID, "cli only session")
    sessions = claude.list_sessions(
        base_dir=str(projects), desktop_dir=str(store)
    )
    assert [s.uuid for s in sessions] == [_CLI_ONLY_UUID]


# ---------------------------------------------------------------------------
# read_session / session_exists / search through the overlay
# ---------------------------------------------------------------------------


def test_read_session_enriched_and_desktop_only(
    dual_roots: tuple[str, str],
) -> None:
    base, desktop = dual_roots
    merged = claude.read_session(_CLI_UUID, base_dir=base, desktop_dir=desktop)
    assert merged.title == "Desktop shiny title"
    assert merged.extra["source_root"] == "desktop"
    ghost = claude.read_session(
        _GHOST_UUID, base_dir=base, desktop_dir=desktop
    )
    assert ghost.extra["source_root"] == "desktop"
    assert ghost.message_count == 0
    with pytest.raises(FileNotFoundError):
        claude.read_session("absent", base_dir=base, desktop_dir=desktop)


def test_read_messages_desktop_only_is_empty(
    dual_roots: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A metadata-only session carries no transcript: reading its messages
    # yields an empty list (honest reference-only answer), not a crash.
    monkeypatch.setenv("AI_R_HOME", str(tmp_path))
    assert claude.read_messages(_GHOST_UUID) == []


def test_ghost_listed_is_readable_with_explicit_roots(
    dual_roots: tuple[str, str],
) -> None:
    """Defect #7-C: a Desktop-ghost surfaced by ``list_sessions(base, desktop)``
    must be openable by ``read_session`` / ``read_messages`` under the SAME
    explicit roots — list and read must agree, no 404.

    Before the fix ``read_messages`` had no ``desktop_dir`` parameter, so a
    ghost that ``list_sessions`` showed 404'd on read (passing only
    ``base_dir`` disabled the Desktop overlay).
    """
    base, desktop = dual_roots
    listed = {s.uuid for s in claude.list_sessions(base_dir=base, desktop_dir=desktop)}
    assert _GHOST_UUID in listed  # the ghost is visible in list

    # read_session must resolve it under the same explicit roots.
    ghost = claude.read_session(_GHOST_UUID, base_dir=base, desktop_dir=desktop)
    assert ghost.extra["source_root"] == "desktop"
    assert ghost.message_count == 0

    # read_messages (now threading desktop_dir) must NOT 404 — empty is fine.
    msgs = claude.read_messages(_GHOST_UUID, base_dir=base, desktop_dir=desktop)
    assert msgs == []

    # Every listed session must be readable through read_messages — the
    # list/read agreement invariant, checked over the whole corpus.
    for uuid in listed:
        claude.read_messages(uuid, base_dir=base, desktop_dir=desktop)


def test_session_exists_covers_desktop(dual_roots: tuple[str, str]) -> None:
    base, desktop = dual_roots
    assert claude.session_exists(_GHOST_UUID, base_dir=base, desktop_dir=desktop)
    assert claude.session_exists(_CLI_UUID, base_dir=base, desktop_dir=desktop)
    assert not claude.session_exists(
        "absent", base_dir=base, desktop_dir=desktop
    )
    assert not claude.session_exists(
        "../escape", base_dir=base, desktop_dir=desktop
    )


def test_search_finds_desktop_title(
    dual_roots: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The motivating bug: a Desktop-launched session was invisible to
    # title search because the CLI-side title was the raw first user
    # message.  The Desktop title must be searchable.
    monkeypatch.setenv("AI_R_HOME", str(tmp_path))
    hits = claude.search("shiny title")
    assert [s.uuid for s in hits] == [_CLI_UUID]


# ---------------------------------------------------------------------------
# Host smoke (auto-tagged @pytest.mark.host, skips when store is absent)
# ---------------------------------------------------------------------------


def test_real_desktop_store_visible(
    real_claude_dir: Path, real_claude_desktop_dir: Path
) -> None:
    sessions = claude.list_sessions(
        base_dir=str(real_claude_dir),
        desktop_dir=str(real_claude_desktop_dir),
    )
    assert sessions
    uuids = [s.uuid for s in sessions]
    assert len(uuids) == len(set(uuids)), "desktop overlay produced duplicates"
    desktop_marked = [
        s for s in sessions if s.extra.get("source_root") == "desktop"
    ]
    assert desktop_marked, "expected at least one desktop-marked session"
