"""Tests for ``ai-r struggle``: struggle-signature detection over ZCode sqlite.

The fixtures mirror the real ``~/.zcode/cli/db/db.sqlite`` layout
(``session`` / ``turn_usage`` / ``message`` / ``part``) and encode the
spec cases (``tmp/struggle-detect/reports/report.md``): the silent-death
triple (SD), the quota-death marker with its false-positive sources
(QD) — a skill quote in an output and the marker echoed in a tool
*input* rather than an output.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from ai_r.struggle import struggle

# Real quota-death marker (spec §"Дополнение"): strict provider contract.
QUOTA_MARKER = (
    "[1308][Usage limit reached for 5 hour. Your limit will reset at "
    "2026-10-01 18:08:06][202610011625280a27760bf6914908]"
)

_SCHEMA = """
CREATE TABLE session (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT,
    title        TEXT,
    directory    TEXT,
    task_type    TEXT NOT NULL DEFAULT 'interactive',
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    last_outcome TEXT
);
CREATE TABLE turn_usage (
    session_id          TEXT NOT NULL,
    turn_id             TEXT NOT NULL,
    status              TEXT NOT NULL,
    started_at          INTEGER NOT NULL,
    duration_ms         INTEGER,
    model_request_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count     INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cancelled_by_user   INTEGER NOT NULL DEFAULT 0,
    error_type          TEXT,
    error_code          TEXT,
    PRIMARY KEY(session_id, turn_id)
);
CREATE TABLE message (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data         TEXT NOT NULL
);
CREATE TABLE part (
    id           TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    data         TEXT NOT NULL
);
"""

# (session_id, last_outcome)
_SESSIONS = [
    ("sess_sd1", "error"),        # SD case A: silent death, session died
    ("sess_sd2", "error"),        # no model request at all -> not SD
    ("sess_sd3", "completed"),    # completed turn -> not SD
    ("sess_qd1", "error"),        # QD: strict marker in a tool output
    ("sess_qd2", "error"),        # QD false positive: skill quote
    ("sess_qd3", "error"),        # QD false positive: marker in input
    ("sess_ok", "completed"),     # healthy session
]

# (session_id, turn_id, status, started_at, req, output_tokens, last flag)
_TURNS = [
    ("sess_sd1", "t1", "error", 1_795_354_000_000, 1, 0),
    ("sess_sd2", "t1", "error", 1_795_354_000_000, 0, 0),
    ("sess_sd3", "t1", "completed", 1_795_354_000_000, 2, 0),
    ("sess_qd1", "t1", "error", 1_795_354_100_000, 21, 7450),
    ("sess_qd2", "t1", "error", 1_795_354_100_000, 3, 500),
    ("sess_qd3", "t1", "error", 1_795_354_100_000, 3, 500),
    ("sess_ok", "t1", "completed", 1_795_354_200_000, 4, 900),
]


def _tool_part(
    pid: str,
    sid: str,
    tool: str,
    *,
    output: Optional[str] = None,
    command: Optional[str] = None,
    status: str = "completed",
) -> tuple[str, str, str, int, str]:
    state: dict = {"status": status, "input": {}}
    if command is not None:
        state["input"] = {"command": command}
    if output is not None:
        state["output"] = output
    data = {"type": "tool", "callID": f"call_{pid}", "tool": tool, "state": state}
    return (pid, f"m-{sid}", sid, 1_795_354_100_000, json.dumps(data))


@pytest.fixture
def struggle_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    for sid, outcome in _SESSIONS:
        conn.execute(
            "INSERT INTO session VALUES (?, NULL, ?, '/home/user/proj', "
            "'interactive', 1_795_354_000_000, 1_795_354_500_000, ?)",
            (sid, f"title {sid}", outcome),
        )
    for sid, tid, status, started, req, out in _TURNS:
        conn.execute(
            "INSERT INTO turn_usage VALUES "
            "(?, ?, ?, ?, NULL, ?, 0, ?, 0, NULL, NULL)",
            (sid, tid, status, started, req, out),
        )
    for sid in {s for s, _ in _SESSIONS}:
        conn.execute(
            "INSERT INTO message VALUES (?, ?, 1_795_354_000_000, '{}')",
            (f"m-{sid}", sid),
        )
    parts = [
        # sess_qd1: the strict marker inside a tool output.
        _tool_part("p-qd1", "sess_qd1", "Bash", output=f"{QUOTA_MARKER}\ndone"),
        # sess_qd2: marker *quoted* from a skill doc (no [1308] prefix).
        _tool_part(
            "p-qd2", "sess_qd2", "Read",
            output="skill says: Usage limit reached for 5 hour...",
        ),
        # sess_qd3: the marker echoed in the command INPUT, not output.
        _tool_part(
            "p-qd3", "sess_qd3", "Bash",
            command="sqlite3 db 'select * from part where output like "
            "%Usage limit reached%'",
        ),
    ]
    conn.executemany("INSERT INTO part VALUES (?, ?, ?, ?, ?)", parts)
    conn.commit()
    conn.close()
    return db_path


def _kinds(records: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for rec in records:
        by.setdefault(rec["kind"], []).append(rec)
    return by


def test_sd_finds_silent_death_only(struggle_db: Path) -> None:
    records = struggle(db=str(struggle_db), kinds=("sd",))
    assert [r["session_id"] for r in records] == ["sess_sd1"]
    rec = records[0]
    assert rec["kind"] == "sd"
    assert rec["agent"] == "zcode"
    assert rec["severity"] == "high"  # session.last_outcome == "error"
    assert "output_tokens=0" in rec["evidence"]
    assert rec["ts"]  # ISO timestamp present


def test_sd_severity_medium_when_session_survived(struggle_db: Path) -> None:
    conn = sqlite3.connect(str(struggle_db))
    conn.execute("UPDATE session SET last_outcome='completed' "
                 "WHERE id='sess_sd1'")
    conn.commit()
    conn.close()
    records = struggle(db=str(struggle_db), kinds=("sd",))
    assert records[0]["severity"] == "medium"


def test_qd_strict_marker_with_reset_at(struggle_db: Path) -> None:
    records = struggle(db=str(struggle_db), kinds=("qd",))
    assert [r["session_id"] for r in records] == ["sess_qd1"]
    rec = records[0]
    assert rec["severity"] == "high"
    assert "1308" in rec["evidence"]
    assert "2026-10-01 18:08:06" in rec["evidence"]
    assert rec["reset_at"] == "2026-10-01 18:08:06"


def test_qd_loose_mode_catches_quote_false_positive(struggle_db: Path) -> None:
    """Strict mode rejects the skill quote; --loose-markers admits it."""
    strict = struggle(db=str(struggle_db), kinds=("qd",))
    assert {r["session_id"] for r in strict} == {"sess_qd1"}
    loose = struggle(db=str(struggle_db), kinds=("qd",), loose_markers=True)
    assert {r["session_id"] for r in loose} == {"sess_qd1", "sess_qd2"}
    qd2 = next(r for r in loose if r["session_id"] == "sess_qd2")
    assert qd2["severity"] == "low"


def test_marker_in_tool_input_is_never_qd(struggle_db: Path) -> None:
    """The live false positive: agents grepping the marker themselves."""
    for loose in (False, True):
        records = struggle(
            db=str(struggle_db), kinds=("qd",), loose_markers=loose
        )
        assert "sess_qd3" not in {r["session_id"] for r in records}


def test_kinds_and_session_filters(struggle_db: Path) -> None:
    all_kinds = struggle(db=str(struggle_db))
    assert set(all_kinds[0]) >= {
        "session_id", "agent", "kind", "severity", "evidence", "ts"
    }
    only_qd = struggle(db=str(struggle_db), kinds=("qd",))
    assert {r["session_id"] for r in only_qd} == {"sess_qd1"}
    pinned = struggle(db=str(struggle_db), session="sess_qd1")
    assert {r["session_id"] for r in pinned} == {"sess_qd1"}
    assert pinned and pinned[0]["kind"] == "qd"


def test_since_until_window(struggle_db: Path) -> None:
    # sess_qd1 turn started at 1_795_354_100_000; sd1 at ...000_000.
    from datetime import datetime, timezone

    since = datetime.fromtimestamp(1_795_354_050_000 / 1000, tz=timezone.utc)
    records = struggle(db=str(struggle_db), kinds=("sd", "qd"), since=since)
    assert {r["session_id"] for r in records} == {"sess_qd1"}
    until = datetime.fromtimestamp(1_795_354_000_000 / 1000, tz=timezone.utc)
    records = struggle(db=str(struggle_db), kinds=("sd",), until=until)
    assert records == []  # sd1 started exactly at `until` (exclusive)


def test_unknown_kind_rejected(struggle_db: Path) -> None:
    with pytest.raises(ValueError):
        struggle(db=str(struggle_db), kinds=("nope",))


# ---------------------------------------------------------------- CLI


def _run_cli(*argv: str) -> object:
    from ai_r.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(("struggle",) + argv)
    return args.func(args)


def test_cli_json_output(struggle_db, capsys: pytest.CaptureFixture) -> None:
    code = _run_cli("--db", str(struggle_db), "--json")
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert {r["kind"] for r in payload} == {"sd", "qd"}
    qd = next(r for r in payload if r["kind"] == "qd")
    assert qd["reset_at"] == "2026-10-01 18:08:06"


def test_cli_kinds_and_session(struggle_db, capsys: pytest.CaptureFixture) -> None:
    code = _run_cli(
        "--db", str(struggle_db), "--kinds", "sd", "--session", "sess_sd1"
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "sess_sd1" in out and "sd" in out
    assert "sess_qd1" not in out


def test_cli_unsupported_agent(struggle_db, capsys: pytest.CaptureFixture) -> None:
    code = _run_cli("--db", str(struggle_db), "--agent", "claude")
    assert code == 2
    assert "zcode" in capsys.readouterr().err
