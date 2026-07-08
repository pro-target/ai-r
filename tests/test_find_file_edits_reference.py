"""Reference-by-default behaviour of ``find_file_edits``.

The MCP ``find_file_edits`` tool historically inlined the full edit body
(``input``) into every record, so an audit listing over a busy path could
balloon past 200 KB.  The core now takes an ``include_input`` flag:

* core default ``include_input=True`` — the record still carries the full
  ``input`` (backward-compat for the in-repo consumers ``session_stats`` /
  ``file_frequency`` / the CLI, which pass through the core default);
* MCP wrapper default ``include_input=False`` — *reference-by-default*: the
  record drops ``input`` and instead carries a light-weight
  ``input_sha256`` + ``input_chars`` so the auditor sees a body exists and
  can fetch it on demand (``get_body`` / ``read_session``).

This module covers the three contract points:
    (a) the default MCP call carries no ``input`` but does carry
        ``input_sha256`` + ``input_chars``;
    (b) ``include_input=True`` restores the full body;
    (c) the core default (used by internal consumers) is unchanged — it
        still inlines ``input`` and never emits the reference fields.
"""

from __future__ import annotations

import json

from ai_r.find_file_edits import find_file_edits as _core
from ai_r.mcp_server import find_file_edits as _mcp


# The ``fake_claude_edit_session`` fixture (a Claude session with one real
# ``Edit`` tool_use) lives in ``conftest.py`` — shared with the ``get_body``
# tool-call body tests so both fingerprint the SAME edit input.  ``_EDIT_INPUT``
# mirrors ``conftest.CLAUDE_EDIT_INPUT`` (kept in step with that fixture).
_EDIT_INPUT: dict[str, str] = {
    "file_path": "/repo/src/widget.py",
    "old_string": "def old():\n    return 1\n",
    "new_string": "def new():\n    return 2\n",
}


def _widget_records(result: dict) -> list[dict]:
    """The records touching ``widget.py`` (guards against host-data leakage)."""
    return [r for r in result["records"] if r.get("file", "").endswith("widget.py")]


# ---------------------------------------------------------------------------
# (a) Default MCP call: reference, not body.
# ---------------------------------------------------------------------------


def test_mcp_default_is_reference_not_body(fake_claude_edit_session: str) -> None:
    """The default MCP call drops ``input`` and emits the reference fields."""
    result = _mcp(path="widget.py", agent="claude")
    records = _widget_records(result)
    assert len(records) == 1, result
    rec = records[0]

    # No full body inlined.
    assert "input" not in rec
    # A light-weight reference is present instead.
    assert "input_sha256" in rec
    assert "input_chars" in rec

    # The reference is meaningful: a 64-hex sha256 and a positive length that
    # matches the JSON-canonical form of the real edit input.
    canonical = json.dumps(_EDIT_INPUT, sort_keys=True, ensure_ascii=False)
    assert len(rec["input_sha256"]) == 64
    assert all(c in "0123456789abcdef" for c in rec["input_sha256"])
    assert rec["input_chars"] == len(canonical)


# ---------------------------------------------------------------------------
# (b) Opt-in via include_input=True: the full body returns.
# ---------------------------------------------------------------------------


def test_mcp_include_input_returns_body(fake_claude_edit_session: str) -> None:
    """``include_input=True`` restores the full ``input`` body on the record."""
    result = _mcp(path="widget.py", agent="claude", include_input=True)
    records = _widget_records(result)
    assert len(records) == 1, result
    rec = records[0]

    assert rec["input"] == _EDIT_INPUT
    # Opt-in body mode does not also emit the reference fields.
    assert "input_sha256" not in rec
    assert "input_chars" not in rec


# ---------------------------------------------------------------------------
# (c) Core default is unchanged for the in-repo consumers.
# ---------------------------------------------------------------------------


def test_core_default_still_inlines_input(fake_claude_edit_session: str) -> None:
    """The core default (``include_input=True``) is byte-for-byte unchanged.

    This is what ``session_stats`` / ``file_frequency`` / the CLI rely on —
    they call the core without passing ``include_input`` and must keep seeing
    the inlined ``input`` (and never the reference fields).
    """
    result = _core(path="widget.py", agent="claude")
    records = _widget_records(result)
    assert len(records) == 1, result
    rec = records[0]

    assert rec["input"] == _EDIT_INPUT
    assert "input_sha256" not in rec
    assert "input_chars" not in rec


def test_core_include_input_false_matches_mcp_default(
    fake_claude_edit_session: str,
) -> None:
    """``_core(include_input=False)`` == the MCP default: reference, no body."""
    result = _core(path="widget.py", agent="claude", include_input=False)
    records = _widget_records(result)
    assert len(records) == 1, result
    rec = records[0]

    assert "input" not in rec
    assert len(rec["input_sha256"]) == 64
    assert rec["input_chars"] > 0


# ---------------------------------------------------------------------------
# (d) Size caps: per-record field caps + total byte budget (mirrors
#     ``find_tool_calls``) — the 3.2M-char-response regression guard.
# ---------------------------------------------------------------------------


def _write_edit_session(
    tmp_sessions_dir, uuid: str, *, user_text: str, assistant_text: str,
    edit_path: str,
) -> None:
    """One user turn + one assistant Edit call (minimal Claude JSONL)."""
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": assistant_text},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": edit_path,
                            "old_string": "a",
                            "new_string": "b",
                        },
                    },
                ],
            },
            "timestamp": "2026-06-14T10:00:05Z",
            "sessionId": uuid,
        },
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-caps"
        / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_size_caps_cut_long_intent_and_assistant(
    tmp_sessions_dir, monkeypatch,
) -> None:
    """Over-long ``intent``/``assistant`` are cut with a marker and named
    in ``truncated_fields`` (the uncapped fields were the 3.2M source)."""
    from pathlib import Path

    _write_edit_session(
        tmp_sessions_dir, "caps-long",
        user_text="i" * 5_000, assistant_text="a" * 10_000,
        edit_path="/repo/caps/long.py",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    result = _core(path="caps/long", agent="claude", include_input=False)
    rec = result["records"][0]
    assert rec["intent"].endswith("…[truncated]")
    assert len(rec["intent"]) <= 1_000 + len("…[truncated]")
    assert rec["assistant"].endswith("…[truncated]")
    assert len(rec["assistant"]) <= 4_000 + len("…[truncated]")
    assert rec["truncated_fields"] == ["intent", "assistant"]
    assert result["output_truncated"] is False


def test_size_caps_never_touch_the_full_input_body(
    fake_claude_edit_session: str,
) -> None:
    """``include_input=True`` promises the FULL body — caps must not cut it
    (``get_body`` round-trips the ``input_sha256`` fingerprint)."""
    result = _core(path="widget.py", agent="claude", include_input=True)
    rec = _widget_records(result)[0]
    assert rec["input"] == _EDIT_INPUT
    assert rec["truncated_fields"] == []


def test_byte_budget_stops_emission_and_flags(
    tmp_sessions_dir, monkeypatch,
) -> None:
    """Past the total byte budget records stop and ``output_truncated``
    is set — distinct from the count-based ``truncated``."""
    from pathlib import Path

    for i in range(3):
        _write_edit_session(
            tmp_sessions_dir, f"caps-budget-{i}",
            user_text=f"edit {i}", assistant_text="Editing.",
            edit_path=f"/repo/budget/f{i}.py",
        )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    monkeypatch.setattr("ai_r.find_file_edits._OUTPUT_BYTES_BUDGET", 300)
    result = _core(path="budget/f", agent="claude")
    assert result["output_truncated"] is True
    assert 1 <= len(result["records"]) < 3
    assert result["count"] == 3
    assert result["truncated"] is False  # no count-based cut happened


def test_size_caps_false_returns_raw_complete_records(
    tmp_sessions_dir, monkeypatch,
) -> None:
    """Internal rollups (``size_caps=False``) get raw fields, every record,
    and no cap bookkeeping — distinct-intent counts must not drift."""
    from pathlib import Path

    _write_edit_session(
        tmp_sessions_dir, "caps-raw",
        user_text="i" * 5_000, assistant_text="Editing.",
        edit_path="/repo/rawcaps/x.py",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )
    monkeypatch.setattr("ai_r.find_file_edits._OUTPUT_BYTES_BUDGET", 300)
    result = _core(
        path="rawcaps", agent="claude", size_caps=False, redact=False
    )
    rec = result["records"][0]
    assert rec["intent"] == "i" * 5_000
    assert "truncated_fields" not in rec
    assert result["output_truncated"] is False
