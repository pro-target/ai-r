"""Tests for the bad-JSONL quarantine sink."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.parsers._quarantine import (
    QUARANTINE_REASON_INVALID_JSON,
    QUARANTINE_REASON_SCHEMA_MISMATCH,
    QUARANTINE_REASON_UNSUPPORTED_TYPE,
    QuarantineSink,
)


def test_quarantine_appends_records(tmp_path: Path) -> None:
    sink = QuarantineSink("claude", "sess-1", base_dir=str(tmp_path))
    sink.quarantine(1, "{not json", QUARANTINE_REASON_INVALID_JSON)
    sink.quarantine(7, "{also bad", QUARANTINE_REASON_SCHEMA_MISMATCH)
    sink.quarantine(42, "{still bad", QUARANTINE_REASON_UNSUPPORTED_TYPE)
    written = sink.flush()
    assert written == 3

    target = tmp_path / "quarantine" / "claude" / "sess-1.badlines.jsonl"
    assert target.is_file()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert [r["line_no"] for r in records] == [1, 7, 42]
    assert [r["reason"] for r in records] == [
        "invalid_json",
        "schema_mismatch",
        "unsupported_record_type",
    ]
    assert [r["raw"] for r in records] == ["{not json", "{also bad", "{still bad"]
    for record in records:
        assert "detected_at" in record
        assert record["detected_at"]


def test_quarantine_creates_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nest" / "level"
    assert not nested.exists()
    sink = QuarantineSink("codex", "deep-sess", base_dir=str(nested))
    sink.quarantine(3, "{x", QUARANTINE_REASON_INVALID_JSON)
    sink.flush()
    target = nested / "quarantine" / "codex" / "deep-sess.badlines.jsonl"
    assert target.is_file()
    assert target.parent.is_dir()


def test_quarantine_swallows_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = QuarantineSink("pi", "boom", base_dir=str(tmp_path))

    def _raise(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(Path, "read_bytes", _raise)
    monkeypatch.setattr(Path, "open", _raise)

    sink.quarantine(9, "{oops", QUARANTINE_REASON_INVALID_JSON)
    assert sink.flush() == 0
