"""Hermetic tests for the Claude Desktop metadata index cache.

Spec: ``_load_desktop_index`` must not re-read + re-``json.loads`` every
``local_*.json`` metadata file when nothing under the Desktop root changed
(same file set, same per-file mtime + size).  A rewritten metadata file
(Desktop updates them IN PLACE — the file's mtime/size change, the
directory's mtime does not) or a new file must invalidate the entry, so a
cache HIT is byte-identical to a MISS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.parsers import claude


@pytest.fixture(autouse=True)
def _clear_desktop_cache():
    """Isolate every case from warm entries left by earlier tests."""
    cache = getattr(claude, "_desktop_index_cache", None)
    if cache is not None:
        cache.clear()
    yield
    cache = getattr(claude, "_desktop_index_cache", None)
    if cache is not None:
        cache.clear()


@pytest.fixture
def desktop_root(tmp_path: Path) -> Path:
    """A Desktop metadata root with two indexable files (observed layout)."""
    ws = tmp_path / "desktop" / "dev-1" / "ws-1"
    ws.mkdir(parents=True)
    (ws / "local_1.json").write_text(
        json.dumps(
            {"cliSessionId": "desk-cli-1", "sessionId": "local_1",
             "title": "Desktop T1"}
        ),
        encoding="utf-8",
    )
    (ws / "local_2.json").write_text(
        json.dumps({"sessionId": "desk-2", "title": "Desktop T2"}),
        encoding="utf-8",
    )
    return tmp_path / "desktop"


def test_cold_scan_builds_expected_index(desktop_root: Path) -> None:
    """The cache wrapper preserves the documented index shape exactly."""
    index = claude._load_desktop_index(desktop_root)
    assert set(index) == {"desk-cli-1", "desk-2"}
    record, json_path = index["desk-cli-1"]
    assert record["title"] == "Desktop T1"
    assert json_path == desktop_root / "dev-1" / "ws-1" / "local_1.json"


def test_warm_repeat_does_not_reparse(
    desktop_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call with an unchanged root parses ZERO metadata files."""
    claude._load_desktop_index(desktop_root)

    calls = {"n": 0}
    real_loads = json.loads

    def counted(s, *args, **kwargs):
        calls["n"] += 1
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(claude.json, "loads", counted)
    index = claude._load_desktop_index(desktop_root)
    assert calls["n"] == 0
    assert set(index) == {"desk-cli-1", "desk-2"}
    assert index["desk-cli-1"][0]["title"] == "Desktop T1"


def test_rewritten_metadata_invalidates(desktop_root: Path) -> None:
    """An in-place rewrite (mtime/size change) yields the NEW content."""
    claude._load_desktop_index(desktop_root)
    target = desktop_root / "dev-1" / "ws-1" / "local_1.json"
    target.write_text(
        json.dumps({"cliSessionId": "desk-cli-1", "title": "Renamed"}),
        encoding="utf-8",
    )
    index = claude._load_desktop_index(desktop_root)
    assert index["desk-cli-1"][0]["title"] == "Renamed"


def test_new_metadata_file_invalidates(desktop_root: Path) -> None:
    """A file appearing under the root shows up on the next call."""
    claude._load_desktop_index(desktop_root)
    (desktop_root / "dev-1" / "ws-1" / "local_3.json").write_text(
        json.dumps({"sessionId": "desk-3", "title": "Third"}),
        encoding="utf-8",
    )
    index = claude._load_desktop_index(desktop_root)
    assert set(index) == {"desk-cli-1", "desk-2", "desk-3"}
