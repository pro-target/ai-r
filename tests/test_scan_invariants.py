"""Scan-level invariants for ``find_tool_calls`` (cache visibility, since
superset, per-agent count sums, zcode timestamps).

Background — the live repro this module pins (observed on main @ 623e28d):

* a WIDE scan (``--since 1970-01-01 --limit 50000``) returned 1322 records
  with ``output_truncated=True`` and a max timestamp of 2026-09-12, while a
  NARROW scan (``--since 2026-09-13``) still saw the 09-13/14 records —
  breaking "wide ⊇ narrow" at the EMISSION level.  Root cause: records are
  sorted by timestamp ascending and the emission loop stopped at the flat
  4 MB byte budget, cutting the TAIL — exactly the newest records.  The
  scan cache itself was healthy (per-file stat signature); the fix scales
  the byte budget with the caller's count contract
  (:func:`ai_r.find_file_edits.scaled_output_budget`).

* a zcode record with ``timestamp: None`` in ``find-tool-calls --json`` was
  NOT reproducible on main (the ``tool ts → msg ts → session ts`` fallback
  chain predates the zcode reader and ``Session.date`` is never ``None``);
  the tests here pin that invariant so a regression cannot land silently.

All cases are hermetic: stores live under the autouse ``AI_R_HOME`` tmp tree
(``tmp_sessions_dir``) and the inventory cache is cleared per test.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_r.find_file_edits import scaled_output_budget
from ai_r.find_tool_calls import find_tool_calls
from ai_r.parsers import PARSERS


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _clear_inventory_cache() -> None:
    """Reset the per-process inventory cache (shared dict in ``_common``)."""
    from ai_r.parsers import _common as _pc

    cache = getattr(_pc, "_agent_sessions_cache", None)
    if cache is not None:
        cache.clear()


@pytest.fixture(autouse=True)
def _fresh_scan_cache():
    """Isolate every case from warm entries left by earlier tests."""
    _clear_inventory_cache()
    yield
    _clear_inventory_cache()


@pytest.fixture
def claude_store(tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the Claude parser at the hermetic tmp projects tree."""
    base = tmp_sessions_dir / ".claude" / "projects"
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: base
    )
    return base


def _write_claude_mcp_session(
    claude_store: Path,
    uuid: str,
    *,
    call_ts: str,
    pad: int = 0,
    project: str = "proj-inv",
) -> Path:
    """One Claude JSONL: user msg + assistant ``mcp__ai-r__query`` call.

    ``pad`` inflates the tool input and the assistant text so a batch of
    these records can exceed a flat byte budget (each padded record
    serializes to ~9 KB — the per-field caps bound it there).
    """
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": f"run query {uuid}"},
            "timestamp": "2026-06-14T09:00:00Z",
            "sessionId": uuid,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Calling the scanner." + "a" * pad},
                    {
                        "type": "tool_use",
                        "name": "mcp__ai-r__query",
                        "input": {"query": "q" * pad} if pad else {"query": "x"},
                    },
                ],
            },
            "timestamp": call_ts,
            "sessionId": uuid,
        },
    ]
    jsonl = claude_store / project / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return jsonl


def _record_key(rec: dict) -> tuple:
    """Stable identity of an emitted record (for superset comparisons)."""
    return (
        rec["agent"],
        rec["session_uuid"],
        rec["message_index"],
        rec["tool"],
        rec["timestamp"],
    )


# ---------------------------------------------------------------------------
# (а) Warm cache → new/appended session → rescan must SEE it
# ---------------------------------------------------------------------------


def test_warmed_scan_sees_new_and_appended_sessions(
    claude_store: Path,
) -> None:
    """The inventory cache must not hide store growth from a rescan.

    Warm both a wide and a narrow scan, then (1) append a second tool call
    to the SAME session file and (2) drop a brand-new session file in —
    both must appear in the NEXT wide AND narrow scan (the stat signature
    keys on per-file mtime+size, so an append and a new file both
    invalidate).
    """
    _write_claude_mcp_session(
        claude_store, "inv-warm-a", call_ts="2026-06-14T10:00:05Z"
    )
    wide = find_tool_calls(tool_name_pattern="mcp__", since="1970-01-01")
    assert wide["count"] == 1  # warm the inventory cache

    # Append to the SAME file: a second user+assistant turn one day later.
    jsonl = claude_store / "proj-inv" / "inv-warm-a.jsonl"
    extra = [
        {
            "type": "user",
            "message": {"role": "user", "content": "run it again"},
            "timestamp": "2026-06-15T09:00:00Z",
            "sessionId": "inv-warm-a",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Again."},
                    {
                        "type": "tool_use",
                        "name": "mcp__ai-r__query",
                        "input": {"query": "y"},
                    },
                ],
            },
            "timestamp": "2026-06-15T10:00:05Z",
            "sessionId": "inv-warm-a",
        },
    ]
    with jsonl.open("a", encoding="utf-8") as fh:
        for rec in extra:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Brand-new session file, newest of all.
    _write_claude_mcp_session(
        claude_store, "inv-warm-b", call_ts="2026-06-16T10:00:05Z"
    )

    wide2 = find_tool_calls(tool_name_pattern="mcp__", since="1970-01-01")
    assert wide2["count"] == 3, "rescan must see the appended AND new calls"
    uuids = {r["session_uuid"] for r in wide2["records"]}
    assert uuids == {"inv-warm-a", "inv-warm-b"}

    # The NARROW window (past the original warm data) must see them too —
    # the live repro was exactly a narrow window outseeing a wide one.
    narrow = find_tool_calls(
        tool_name_pattern="mcp__", since="2026-06-15T00:00:00Z"
    )
    assert narrow["count"] == 2
    assert {r["session_uuid"] for r in narrow["records"]} == {
        "inv-warm-a",
        "inv-warm-b",
    }


def test_warmed_scan_sees_sqlite_append(
    tmp_sessions_dir: Path, fake_zcode_db: Path
) -> None:
    """Same visibility contract for the ZCode SQLite store: a warmed scan
    must see a message/tool-part row inserted after the warm call."""
    warm = find_tool_calls(tool_name_pattern="Read", agent="zcode")
    assert warm["count"] == 1  # zc-p-3 (Read) — and the cache is warm now

    conn = sqlite3.connect(str(fake_zcode_db))
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
        ("zc-m-late", "sess_test-zc-1", 1_790_002_000_000,
         1_790_002_000_000,
         json.dumps({"role": "assistant",
                     "time": {"created": 1_790_002_000_000}}),
         3),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("zc-p-late", "zc-m-late", "sess_test-zc-1",
         1_790_002_000_000, 1_790_002_000_000,
         json.dumps({"type": "tool", "tool": "Read",
                     "callID": "call_zc_late",
                     "state": {"status": "completed",
                               "input": {"file_path": "/tmp/x/late.py"},
                               "output": "2 lines"}}), 0),
    )
    conn.commit()
    conn.close()

    rescan = find_tool_calls(tool_name_pattern="Read", agent="zcode")
    assert rescan["count"] == 2, "sqlite append must invalidate the warm scan"
    assert rescan["records"][-1]["timestamp"] is not None


# ---------------------------------------------------------------------------
# (б) Wide since ⊇ narrow since (emission level)
# ---------------------------------------------------------------------------


def test_wide_since_emission_is_superset_of_narrow(
    claude_store: Path,
) -> None:
    """A wide ``since`` must EMIT every record a narrower ``since`` emits.

    The corpus is sized so its serialized records (~9 KB each, field-capped)
    exceed the flat 4 MB base budget: pre-fix the emission loop tail-cut the
    newest records, so the narrow window out-saw the wide one.
    """
    n_sessions = 500
    for i in range(n_sessions):
        # Spread across one day: 2026-06-14T10:00 + i minutes.
        ts = f"2026-06-14T{10 + i // 60:02d}:{i % 60:02d}:05Z"
        _write_claude_mcp_session(
            claude_store, f"inv-wide-{i:04d}", call_ts=ts, pad=4_000
        )

    wide = find_tool_calls(
        tool_name_pattern="mcp__", since="1970-01-01", limit=100_000
    )
    # The contract: everything matched is emitted, nothing tail-cut.
    assert wide["count"] == n_sessions
    assert len(wide["records"]) == wide["count"]
    assert wide["output_truncated"] is False
    assert wide["truncated"] is False

    # Narrow window over the newest ~third of the corpus.
    narrow = find_tool_calls(
        tool_name_pattern="mcp__",
        since="2026-06-14T10:20:00Z",
        limit=100_000,
    )
    assert 0 < narrow["count"] < wide["count"]
    assert narrow["output_truncated"] is False

    wide_keys = {_record_key(r) for r in wide["records"]}
    narrow_keys = {_record_key(r) for r in narrow["records"]}
    missing = narrow_keys - wide_keys
    assert not missing, (
        f"wide since 1970 emission is missing {len(missing)} record(s) "
        f"the narrow since window emits: {sorted(missing)[:3]}"
    )
    assert wide["count"] >= narrow["count"]


def test_output_budget_still_guards_uncapped_content(
    claude_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scaling the budget with the count contract must NOT disable the
    guard: with both knobs shrunk the emission still stops and flags."""
    import ai_r.find_tool_calls as ftc

    for i in range(3):
        _write_claude_mcp_session(
            claude_store, f"inv-guard-{i}", call_ts=f"2026-06-14T10:0{i}:05Z"
        )
    monkeypatch.setattr(ftc, "_OUTPUT_BYTES_BUDGET", 300)
    monkeypatch.setattr(ftc, "_WORST_CAPPED_RECORD_BYTES", 10)
    result = ftc.find_tool_calls(tool_name_pattern="mcp__")
    assert result["count"] == 3
    assert result["output_truncated"] is True
    assert 1 <= len(result["records"]) < 3
    assert result["truncated"] is False


def test_scaled_output_budget_floor_and_ceiling() -> None:
    """The budget helper: base floor at small plans, linear scaling in
    between, hard ceiling for monster plans."""
    assert scaled_output_budget(0, 12_000) == 4_000_000
    assert scaled_output_budget(10, 12_000) == 4_000_000  # floor
    assert scaled_output_budget(1_000, 12_000) == 12_000_000  # scaled
    assert scaled_output_budget(10**9, 12_000) == 512_000_000  # ceiling


# ---------------------------------------------------------------------------
# (в) Unfiltered count == sum of per-agent counts
# ---------------------------------------------------------------------------


def test_unfiltered_count_equals_per_agent_sum(
    claude_store: Path, tmp_sessions_dir: Path
) -> None:
    """The no-agent scan count is exactly the sum of per-agent counts."""
    _write_claude_mcp_session(
        claude_store, "inv-sum-a", call_ts="2026-06-14T10:00:05Z"
    )
    _write_claude_mcp_session(
        claude_store, "inv-sum-b", call_ts="2026-06-14T11:00:05Z"
    )
    # A zcode mcp__ call in the SQLite store (second agent with data).
    db_path = tmp_sessions_dir / ".zcode" / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, directory TEXT,
            task_type TEXT NOT NULL DEFAULT 'interactive',
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES session(id),
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL, sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES message(id),
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL, sequence INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("sess-inv-sum", None, "Sum probe", "/home/user/proj",
         "interactive", 1_790_000_000_000, 1_790_000_500_000),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
        ("m-inv-sum", "sess-inv-sum", 1_790_000_200_000, 1_790_000_200_000,
         json.dumps({"role": "assistant",
                     "time": {"created": 1_790_000_200_000}}), 0),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p-inv-sum", "m-inv-sum", "sess-inv-sum",
         1_790_000_200_000, 1_790_000_200_000,
         json.dumps({"type": "tool", "tool": "mcp__ai-r__query",
                     "callID": "call_inv_sum",
                     "state": {"status": "completed",
                               "input": {"query": "z"},
                               "output": "ok"}}), 0),
    )
    conn.commit()
    conn.close()

    per_agent = {
        agent.value.lower(): find_tool_calls(
            tool_name_pattern="mcp__", agent=agent.value.lower()
        )["count"]
        for agent in PARSERS
    }
    assert per_agent["claude"] == 2
    assert per_agent["zcode"] == 1

    total = find_tool_calls(tool_name_pattern="mcp__")["count"]
    assert total == sum(per_agent.values()), (
        f"unfiltered count {total} != per-agent sum {sum(per_agent.values())}"
    )


# ---------------------------------------------------------------------------
# zcode timestamps: DB and rollout records always carry one (defect-2 pin)
# ---------------------------------------------------------------------------


def test_zcode_db_records_carry_timestamp(fake_zcode_db: Path) -> None:
    """DB-sourced zcode tool calls must never emit ``timestamp: None``."""
    result = find_tool_calls(tool_name_pattern="e", agent="zcode")
    assert result["count"] >= 2  # Read + Edit (+ AskUserQuestion)
    assert all(r["timestamp"] for r in result["records"])
    # The per-call instant is the tool part's own time, not the epoch floor.
    stamps = {r["timestamp"] for r in result["records"]}
    assert all(not ts.startswith("1970-01-01") for ts in stamps)


def test_zcode_rollout_records_carry_timestamp(
    tmp_sessions_dir: Path,
) -> None:
    """Rollout-sourced zcode tool calls (input-snapshot messages carry no
    per-message ts) fall back to the session date — never ``None``."""
    rollout = tmp_sessions_dir / ".zcode" / "cli" / "rollout"
    rollout.mkdir(parents=True, exist_ok=True)
    session_id = "sess_inv-rollout"
    record = {
        "sessionId": session_id,
        "completedAt": "2026-06-14T10:05:00.000Z",
        "request": {
            "messagesKind": "full",
            "messages": [
                {"role": "user", "content": "run it"},
                {
                    "role": "assistant",
                    "toolCalls": [
                        {
                            "id": "call_ro_1",
                            "name": "mcp__ai-r__query",
                            "input": {"query": "r"},
                        }
                    ],
                },
            ],
        },
        "response": {"text": "done", "toolCalls": [], "usage": {}},
    }
    # The rollout filename must be model-io-<sessionId>.jsonl — that is how
    # read_session locates the file for a listed uuid.
    (rollout / f"model-io-{session_id}.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    result = find_tool_calls(
        tool_name_pattern="mcp__", agent="zcode", limit=0
    )
    assert result["count"] == 1
    rec = result["records"][0]
    assert rec["timestamp"] is not None
    assert rec["timestamp"].startswith("2026-06-14")


def test_cli_json_zcode_timestamp_parity(fake_zcode_db: Path) -> None:
    """The CLI ``--json`` surface is the core result verbatim — zcode
    records keep their timestamps there too (the live complaint was
    ``timestamp: None`` on this exact surface)."""
    import contextlib
    import io

    from ai_r import cli as cli_module

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = cli_module.main(
            [
                "find-tool-calls",
                "--pattern",
                "e",
                "--agent",
                "zcode",
                "--json",
            ]
        )
    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert payload["count"] >= 2
    assert all(rec["timestamp"] for rec in payload["records"])
