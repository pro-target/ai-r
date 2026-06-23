"""Tests for the Antigravity brain parser.

The real ``~/.gemini/antigravity/brain`` directory on the test host is
empty (no Antigravity installation), so all real-data assertions are
guarded by ``skipif``.  The synthetic fixture in ``conftest.py`` covers
the parser logic in isolation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_reader.parsers import AgentName, antigravity
from ai_reader.parsers.antigravity import (
    _extract_title_from_markdown,
    _extract_title_from_overview,
    _is_valid_uuid,
    _normalise_title,
    _parse_iso_timestamp,
    _resolve_brain_roots,
    _scan_brain,
)


# ---------------------------------------------------------------------------
# Real data smoke (skipped when brain dir is empty / missing)
# ---------------------------------------------------------------------------


def test_list_sessions_real(real_antigravity_root: Path | None) -> None:
    if real_antigravity_root is None:
        pytest.skip("no real Antigravity brain dir on this host")

    sessions = antigravity.list_sessions(base_dir=str(real_antigravity_root))
    assert isinstance(sessions, list)
    for s in sessions[:3]:
        assert s.agent is AgentName.ANTIGRAVITY
        assert s.title


def test_scan_brain_real_smoke(real_antigravity_root: Path | None) -> None:
    if real_antigravity_root is None:
        pytest.skip("no real Antigravity brain dir on this host")

    for brain in sorted(real_antigravity_root.iterdir(), key=lambda p: p.name):
        session = _scan_brain(brain)
        if session is None:
            continue
        assert session.agent is AgentName.ANTIGRAVITY
        assert session.uuid == brain.name
        assert session.title
        assert session.path == str(brain)
        assert session.message_count >= 0
        return

    pytest.skip("real Antigravity brain dir has no parseable sessions")


# ---------------------------------------------------------------------------
# Synthetic brain
# ---------------------------------------------------------------------------


def test_parse_message(fake_antigravity_brain: Path) -> None:
    sessions = antigravity.list_sessions(
        base_dir=str(fake_antigravity_brain.parent)
    )
    assert len(sessions) == 1
    s = sessions[0]
    assert s.uuid == "test-ag-1"
    assert s.agent is AgentName.ANTIGRAVITY
    assert s.title == "Set up the lab"
    assert s.message_count == 2  # two events in overview.txt
    assert s.path == str(fake_antigravity_brain)


def test_read_session(fake_antigravity_brain: Path) -> None:
    s = antigravity.read_session(
        "test-ag-1", base_dir=str(fake_antigravity_brain.parent)
    )
    assert s.uuid == "test-ag-1"
    assert s.title == "Set up the lab"


def test_read_session_invalid_uuid(tmp_sessions_dir: Path) -> None:
    with pytest.raises(ValueError):
        antigravity.read_session("../escape", base_dir="anything")


def test_session_exists(fake_antigravity_brain: Path) -> None:
    base = str(fake_antigravity_brain.parent)
    assert antigravity.session_exists("test-ag-1", base_dir=base) is True
    assert antigravity.session_exists("nope", base_dir=base) is False
    assert antigravity.session_exists("../escape", base_dir=base) is False


def test_search(fake_antigravity_brain: Path) -> None:
    base = str(fake_antigravity_brain.parent)
    out = antigravity.search("lab", base_dir=base)
    assert len(out) == 1
    assert antigravity.search("zzz", base_dir=base) == []
    assert antigravity.search("", base_dir=base) == []


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def test_normalise_title() -> None:
    assert _normalise_title("hi\nthere") == "hi there"
    assert _normalise_title("") == "Untitled"
    assert _normalise_title("x" * 200) == "x" * 100


def test_is_valid_uuid() -> None:
    assert _is_valid_uuid("abc")
    assert not _is_valid_uuid("")
    assert not _is_valid_uuid("a/b")
    assert not _is_valid_uuid("a\\b")
    assert not _is_valid_uuid(" has space")


def test_parse_iso_timestamp() -> None:
    assert _parse_iso_timestamp("2026-06-14T10:00:00Z") is not None
    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("nope") is None
    assert _parse_iso_timestamp(None) is None  # type: ignore[arg-type]


def test_extract_title_from_overview_returns_untitled_on_no_match(
    tmp_path: Path,
) -> None:
    p = tmp_path / "overview.txt"
    p.write_text(
        '{"timestamp":"2026-06-14T10:00:00Z","source":"MODEL","type":"OUTPUT","content":"x"}\n',
        encoding="utf-8",
    )
    title, count, latest = _extract_title_from_overview(p)
    assert title == ""
    assert count == 1
    assert latest == "2026-06-14T10:00:00Z"


def test_extract_title_from_overview_handles_missing_file(tmp_path: Path) -> None:
    title, count, latest = _extract_title_from_overview(tmp_path / "nope.txt")
    assert title == "" and count == 0 and latest is None


def test_extract_title_from_markdown(tmp_path: Path) -> None:
    (tmp_path / "walkthrough.md").write_text(
        "# Hello world\n\nbody\n", encoding="utf-8"
    )
    assert _extract_title_from_markdown(tmp_path) == "Hello world"


def test_extract_title_from_markdown_no_file(tmp_path: Path) -> None:
    assert _extract_title_from_markdown(tmp_path) == ""


def test_resolve_brain_roots_explicit(tmp_path: Path) -> None:
    roots = _resolve_brain_roots(base_dir=str(tmp_path))
    assert roots == [tmp_path]


def test_resolve_brain_roots_uses_ai_reader_home(tmp_path: Path) -> None:
    # _isolate_ai_reader_home fixture points AI_READER_HOME at tmp_path.
    roots = _resolve_brain_roots()
    # We only assert the function returns a list (real shape depends on
    # the fixture tree, which always creates antigravity dirs).
    assert isinstance(roots, list)


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


def test_read_messages_from_overview(fake_antigravity_brain: Path) -> None:
    """read_messages falls back to overview.txt when no transcript exists."""
    base = str(fake_antigravity_brain.parent)
    msgs = antigravity.read_messages("test-ag-1", base_dir=base)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].text == "<USER_REQUEST>Set up the lab</USER_REQUEST>"
    assert msgs[1].role == "assistant"
    assert msgs[1].text == "ok"


def test_read_messages_from_transcript(
    fake_antigravity_brain_with_transcript: Path,
) -> None:
    base = str(fake_antigravity_brain_with_transcript.parent)
    msgs = antigravity.read_messages("ag-tools-1", base_dir=base)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].text == "Set up the lab"
    assert msgs[1].role == "assistant"
    assert msgs[1].text == "Lab is ready"


def test_read_messages_missing_raises(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        antigravity.read_messages("nope", base_dir=str(tmp_sessions_dir))


def test_read_messages_invalid_uuid(tmp_sessions_dir: Path) -> None:
    with pytest.raises(ValueError):
        antigravity.read_messages("../escape", base_dir="anything")
