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


def _write_claude_write_session(
    uuid: str, writes: list[tuple[str, str]], *, intent: str = "Write the files"
) -> None:
    """A Claude JSONL: one user intent, then one ``Write`` per (path, content)."""
    home = Path(os.environ["AI_R_HOME"])
    jsonl = home / ".claude" / "projects" / "proj-sd" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = [
        {
            "type": "user",
            "message": {"role": "user", "content": intent},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        }
    ]
    for i, (fpath, content) in enumerate(writes):
        records.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": fpath, "content": content},
                        },
                    ],
                },
                "timestamp": f"2026-06-14T10:0{i + 1}:00Z",
                "sessionId": uuid,
            }
        )
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_mcp_session_diff_caps_big_write_and_stitched_diff(tmp_path: Path) -> None:
    """The 145K-char regression: one big ``Write`` must not blow the response.

    The body used to be emitted TWICE, uncapped — in the write hunk and in
    the stitched per-file ``diff``.  The MCP wrapper now cuts over-long
    fields with a ``…[truncated]`` marker, names every cut in the per-file
    ``truncated_fields`` (indexed paths), and carries ``output_truncated``.
    The CORE result stays raw — the cap is a transport bound, not data loss.
    """
    uuid = "claude-sd-big-1"
    content = "<html>\n" + ("x" * 88 + "\n") * 1000  # ~89 KB body
    intent = "please write the full report page again " * 30  # > 1000 chars
    _write_claude_write_session(
        uuid, [("/repo/site/report.html", content)], intent=intent
    )

    # The core keeps the full body (session_diff CORE contract is unchanged).
    raw = session_diff(uuid, "claude")
    assert raw["files"][0]["edits"][0]["hunks"][0]["content"] == content

    result = mcp_session_diff(session_uuid=uuid, agent="claude")
    f = result["files"][0]
    hunk = f["edits"][0]["hunks"][0]
    marker = "…[truncated]"
    assert hunk["content"].endswith(marker)
    assert len(hunk["content"]) == 4_000 + len(marker)
    assert f["edits"][0]["intent"].endswith(marker)
    assert len(f["edits"][0]["intent"]) == 1_000 + len(marker)
    assert f["diff"].endswith(marker)
    assert len(f["diff"]) == 20_000 + len(marker)
    assert f["truncated_fields"] == [
        "edits[0].intent",
        "edits[0].hunks[0].content",
        "diff",
    ]
    assert result["output_truncated"] is False
    assert result["count"] == 1
    # The whole response stays bounded (the regression was 145,811 chars).
    assert len(json.dumps(result, ensure_ascii=False)) < 40_000


def test_mcp_session_diff_small_session_untouched(tmp_path: Path) -> None:
    """Under-cap fields pass through byte-identical; the additive markers
    read "nothing was cut" (``truncated_fields == []``)."""
    uuid = "claude-sd-1"
    _write_claude_multi_edit_session(tmp_path, uuid, "/repo/src/mod.py")
    result = mcp_session_diff(session_uuid=uuid, agent="claude")
    f = result["files"][0]
    assert f["truncated_fields"] == []
    assert result["output_truncated"] is False
    assert f["edits"][2]["hunks"][0]["content"] == "def bar():\n    return 42\n"
    assert f["edits"][0]["intent"] == "Rename foo to bar"


def test_mcp_session_diff_byte_budget_stops_file_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the total byte budget whole file entries stop emitting and
    ``output_truncated`` flips; ``count`` keeps the TRUE total so the cut
    is visible (mirrors ``find_file_edits``)."""
    uuid = "claude-sd-budget-1"
    _write_claude_write_session(
        uuid,
        [("/repo/a.py", "print('a')\n" * 20), ("/repo/b.py", "print('b')\n" * 20)],
    )
    monkeypatch.setattr("ai_r.mcp_server._DIFF_OUTPUT_BYTES_BUDGET", 300)
    result = mcp_session_diff(session_uuid=uuid, agent="claude")
    assert result["output_truncated"] is True
    assert result["count"] == 2
    assert len(result["files"]) == 1


def test_mcp_diff_verb_shares_the_size_cap(tmp_path: Path) -> None:
    """The ``diff`` verb emits the same shape and shares the same bound —
    including its flat per-file ``hunks`` view, which aliases the very hunk
    dicts under ``edits[*].hunks`` (one walk bounds both)."""
    from ai_r.events import query
    from ai_r.mcp_server import diff as mcp_diff

    uuid = "claude-sd-big-2"
    content = ("y" * 100 + "\n") * 900  # ~91 KB body
    _write_claude_write_session(uuid, [("/repo/site/page.html", content)])

    rows = [
        ev
        for ev in query(type="tool_call(write)", session=uuid, agent="claude")
        if any("file" in r for r in ev.get("refs", ()))
    ]
    result = mcp_diff(rows=rows)
    f = result["files"][0]
    marker = "…[truncated]"
    assert f["edits"][0]["hunks"][0]["content"].endswith(marker)
    assert f["diff"].endswith(marker)
    assert f["hunks"][0]["content"].endswith(marker)  # the aliased flat view
    assert result["output_truncated"] is False
