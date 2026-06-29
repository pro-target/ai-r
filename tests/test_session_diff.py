"""Hermetic tests for :func:`ai_r.session_diff.session_diff`.

The reconstruction is exercised against a fake Claude session written into
the per-test ``AI_R_HOME`` (the autouse ``_isolate_ai_r_home`` fixture in
``conftest.py``). No real host data, no git, no ``@pytest.mark.host``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_r.mcp_server import session_diff as mcp_session_diff
from ai_r.session_diff import session_diff


def _write_claude_multi_edit_session(tmp_path: Path, uuid: str, edit_path: str) -> None:
    """A Claude JSONL: two Edit calls + one Write on the SAME file, in order."""
    home = Path(os.environ["AI_R_HOME"])
    jsonl = home / ".claude" / "projects" / "proj-sd" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Rename foo to bar"},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Editing."},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": edit_path,
                            "old_string": "foo",
                            "new_string": "bar",
                        },
                    },
                ],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "Now add a docstring"},
            "timestamp": "2026-06-14T10:01:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": edit_path,
                            "old_string": "def bar():",
                            "new_string": 'def bar():\n    """doc"""',
                        },
                    },
                ],
            },
            "timestamp": "2026-06-14T10:01:05Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {
                            "file_path": edit_path,
                            "content": "def bar():\n    return 42\n",
                        },
                    },
                ],
            },
            "timestamp": "2026-06-14T10:02:00Z",
            "sessionId": uuid,
        },
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_session_diff_collects_edit_and_write_hunks_in_order(tmp_path: Path) -> None:
    uuid = "claude-sd-1"
    edit_path = "/repo/src/mod.py"
    _write_claude_multi_edit_session(tmp_path, uuid, edit_path)

    result = session_diff(uuid, "claude")

    assert result["count"] == 1
    assert [c for c in result["caveats"]]  # both caveats present
    assert len(result["caveats"]) == 2

    f = result["files"][0]
    assert f["file"] == edit_path
    edits = f["edits"]
    assert len(edits) == 3  # two Edit + one Write

    # Chronological order preserved.
    assert [e["timestamp"] for e in edits] == [
        "2026-06-14T10:00:05+00:00",
        "2026-06-14T10:01:05+00:00",
        "2026-06-14T10:02:00+00:00",
    ]
    assert [e["tool"] for e in edits] == ["Edit", "Edit", "Write"]

    # Form 1: Edit hunks carry old→new.
    first = edits[0]["hunks"][0]
    assert first["kind"] == "replace"
    assert first["old"] == "foo"
    assert first["new"] == "bar"

    # Form 2: Write hunk carries full content.
    write_hunk = edits[2]["hunks"][0]
    assert write_hunk["kind"] == "write"
    assert write_hunk["content"] == "def bar():\n    return 42\n"

    # Intent threaded from the preceding user message.
    assert edits[0]["intent"] == "Rename foo to bar"
    assert edits[1]["intent"] == "Now add a docstring"

    # Readable stitched diff contains both the replace markers and the
    # full-content write markers.
    diff = f["diff"]
    assert "- foo" in diff
    assert "+ bar" in diff
    assert "+ def bar():" in diff
    assert "+     return 42" in diff


def test_session_diff_path_filter(tmp_path: Path) -> None:
    uuid = "claude-sd-1"
    edit_path = "/repo/src/mod.py"
    _write_claude_multi_edit_session(tmp_path, uuid, edit_path)

    # Matching substring keeps the file.
    assert session_diff(uuid, "claude", path="mod.py")["count"] == 1
    # Non-matching substring drops it.
    assert session_diff(uuid, "claude", path="other.py")["count"] == 0


def test_session_diff_caveats_mention_both_blind_spots(tmp_path: Path) -> None:
    uuid = "claude-sd-1"
    _write_claude_multi_edit_session(tmp_path, uuid, "/repo/src/mod.py")
    caveats = " ".join(session_diff(uuid, "claude")["caveats"]).lower()
    assert "git" in caveats  # blind spot 1
    assert "tee" in caveats and "sed -i" in caveats  # RISK-3 blind spot 2


def test_session_diff_unknown_agent_raises() -> None:
    with pytest.raises(ValueError):
        session_diff("any", "not-an-agent")


def test_session_diff_empty_uuid_raises() -> None:
    with pytest.raises(ValueError):
        session_diff("   ", "claude")


def test_session_diff_missing_session_empty(tmp_path: Path) -> None:
    result = session_diff("does-not-exist", "claude")
    assert result["count"] == 0
    assert result["files"] == []
    assert len(result["caveats"]) == 2


def test_mcp_session_diff_happy_path(tmp_path: Path) -> None:
    uuid = "claude-sd-1"
    _write_claude_multi_edit_session(tmp_path, uuid, "/repo/src/mod.py")
    result = mcp_session_diff(session_uuid=uuid, agent="claude")
    assert result["count"] == 1
    assert "error" not in result


def test_mcp_session_diff_invalid_agent_returns_error_dict() -> None:
    result = mcp_session_diff(session_uuid="any", agent="bogus")
    assert result["error"] == "invalid_argument"
    assert "bogus" in result["message"]
