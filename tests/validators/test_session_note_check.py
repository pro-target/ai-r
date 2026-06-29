"""Tests for the session-note template validator.

The required-sections list is read from the bundled template at
runtime, so the template file is the single source of truth shared
with whatever tool produces session notes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_r.validators.session_note_check import (
    SessionNoteValidationResult,
    extract_session_id_from_note,
    parse_required_sections,
    validate_session_note,
    validate_session_note_with_identity,
)

_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "ai_r"
    / "templates"
    / "session_note.md"
)


_DETECT_VARS = (
    "AI_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "OPENCODE_SESSION_ID",
    "AGENT_NAME",
    "AI_AGENT",
    "CODING_AGENT",
    "CODEX_HOME",
    "CLAUDECODE",
    "OPENCODE",
    "AI_R_SESSION_IDENTITY_DIR",
    "AI_SESSION_OUTPUT",
)


@pytest.fixture(autouse=True)
def _clean_session_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for var in _DETECT_VARS:
        monkeypatch.delenv(var, raising=False)
    empty_identity = tmp_path / "empty_identity"
    empty_identity.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AI_R_SESSION_IDENTITY_DIR", str(empty_identity))


def _complete_note(session_block: str = "") -> str:
    block = f"\n## Session\n\n{session_block}\n" if session_block else ""
    return (
        "# Session Note: demo\n\n"
        "**UUID**: u-1\n"
        "**Agent**: codex\n"
        "**Date**: 2026-06-21\n\n"
        "## Goal\nShip PR7b.\n\n"
        "## Decisions\n- validator reads template at runtime.\n\n"
        "## Files touched\n- src/ai_r/validators/session_note_check.py\n\n"
        "## Open questions\n- none\n\n"
        "## Next actions\n- wire into session-summarizer.\n\n"
        "## Verification\n- run pytest tests/validators/.\n"
        f"{block}"
    )


def _write_per_session(base: Path, agent: str, session_id: str) -> Path:
    path = base / agent / session_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id, encoding="utf-8")
    return path


def test_parse_required_sections() -> None:
    sections = parse_required_sections(_TEMPLATE)
    assert sections == [
        "Goal",
        "Decisions",
        "Files touched",
        "Open questions",
        "Next actions",
        "Verification",
    ]


def test_validate_complete_note(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(_complete_note(), encoding="utf-8")
    assert validate_session_note(note, _TEMPLATE) == []


def test_validate_missing_section(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        (
            "# Session Note: demo\n\n"
            "## Goal\nShip PR7b.\n\n"
            "## Decisions\n- none.\n\n"
            "## Files touched\n- a.py\n\n"
            "## Open questions\n- none\n\n"
            "## Next actions\n- ship.\n"
        ),
        encoding="utf-8",
    )
    assert "Verification" in validate_session_note(note, _TEMPLATE)


def test_validate_empty_section_body(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        (
            "# Session Note: demo\n\n"
            "## Goal\nShip PR7b.\n\n"
            "## Decisions\n- none.\n\n"
            "## Files touched\n- a.py\n\n"
            "## Open questions\n- none\n\n"
            "## Next actions\n- ship.\n\n"
            "## Verification\n\n"
        ),
        encoding="utf-8",
    )
    assert "Verification" in validate_session_note(note, _TEMPLATE)


# ---------------------------------------------------------------------------
# extract_session_id_from_note
# ---------------------------------------------------------------------------


def test_extract_session_id_from_note_simple(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        _complete_note(session_block="Session ID: ses_xxxyyy"),
        encoding="utf-8",
    )
    assert extract_session_id_from_note(note) == "ses_xxxyyy"


def test_extract_session_id_no_session_section(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(_complete_note(), encoding="utf-8")
    assert extract_session_id_from_note(note) is None


# ---------------------------------------------------------------------------
# validate_session_note_with_identity — multi-candidate awareness
# ---------------------------------------------------------------------------


def test_validate_session_note_identity_match_any_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "session-identity"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AI_R_SESSION_IDENTITY_DIR", str(base))
    _write_per_session(base, "opencode", "ses_firstcandidate")
    _write_per_session(base, "opencode", "ses_secondcandidat")
    note = tmp_path / "note.md"
    note.write_text(
        _complete_note(session_block="Session ID: ses_secondcandidat"),
        encoding="utf-8",
    )
    result = validate_session_note_with_identity(note, _TEMPLATE)
    assert isinstance(result, SessionNoteValidationResult)
    assert result.declared_session_id == "ses_secondcandidat"
    assert result.identity_ok is True
    assert result.errors == []
    assert len(result.candidates) == 2
    assert result.ambiguous is True


def test_validate_session_note_identity_mismatch_with_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "session-identity"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AI_R_SESSION_IDENTITY_DIR", str(base))
    _write_per_session(base, "opencode", "ses_candidateaaa")
    _write_per_session(base, "opencode", "ses_candidatebbb")
    note = tmp_path / "note.md"
    note.write_text(
        _complete_note(session_block="Session ID: ses_neitherhere"),
        encoding="utf-8",
    )
    result = validate_session_note_with_identity(note, _TEMPLATE)
    assert result.identity_ok is False
    assert any("mismatch" in e or "ambiguous" in e for e in result.errors)


def test_validate_session_note_identity_unverifiable(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        _complete_note(session_block="Session ID: ses_nothinghere"),
        encoding="utf-8",
    )
    result = validate_session_note_with_identity(note, _TEMPLATE)
    assert result.declared_session_id == "ses_nothinghere"
    assert result.identity_ok is False
    assert any("unverifiable" in e for e in result.errors)
    assert result.candidates == []


def test_validate_session_note_identity_ambiguous_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "session-identity"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AI_R_SESSION_IDENTITY_DIR", str(base))
    _write_per_session(base, "opencode", "ses_candidateaaa")
    _write_per_session(base, "opencode", "ses_candidatebbb")
    note = tmp_path / "note.md"
    note.write_text(
        _complete_note(session_block="Session ID: ses_nomatchany"),
        encoding="utf-8",
    )
    result = validate_session_note_with_identity(note, _TEMPLATE)
    assert result.ambiguous is True
    assert result.identity_ok is False
    assert any("ambiguous" in e for e in result.errors)
