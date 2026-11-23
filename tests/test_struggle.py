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
    data         TEXT NOT NULL,
    sequence     INTEGER
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
    ("sess_ll1", "error"),        # LL: overspending builder turn
    ("sess_br1", "error"),        # BR: blind retry after an errored call
    ("sess_ok", "completed"),     # healthy session
]

# (session_id, turn_id, status, started_at, req, output_tokens, tools)
_TURNS = [
    ("sess_sd1", "t1", "error", 1_795_354_000_000, 1, 0, 0),
    ("sess_sd2", "t1", "error", 1_795_354_000_000, 0, 0, 0),
    ("sess_sd3", "t1", "completed", 1_795_354_000_000, 2, 0, 0),
    ("sess_qd1", "t1", "error", 1_795_354_100_000, 21, 7450, 21),
    ("sess_qd2", "t1", "error", 1_795_354_100_000, 3, 500, 3),
    ("sess_qd3", "t1", "error", 1_795_354_100_000, 3, 500, 3),
    # Spec case C: 280 tool calls, died error (but did produce output).
    ("sess_ll1", "t1", "error", 1_795_354_150_000, 30, 40_000, 280),
    ("sess_br1", "t1", "error", 1_795_354_160_000, 3, 800, 3),
    ("sess_ok", "t1", "completed", 1_795_354_200_000, 4, 900, 4),
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
    for sid, tid, status, started, req, out, tools in _TURNS:
        conn.execute(
            "INSERT INTO turn_usage VALUES "
            "(?, ?, ?, ?, NULL, ?, ?, ?, 0, NULL, NULL)",
            (sid, tid, status, started, req, tools, out),
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
        # sess_br1 (spec case D): a Bash call errors, the very next call
        # repeats it verbatim except for the digit run — normalized dup.
        _tool_part(
            "p-br-1", "sess_br1", "Bash",
            command="cat /tmp/report-1234.log", status="error",
        ),
        _tool_part(
            "p-br-2", "sess_br1", "Bash",
            command="cat /tmp/report-9999.log",
        ),
        # Same tool, but the inputs differ beyond digits: NOT a retry.
        _tool_part(
            "p-br-3", "sess_br1", "Read", command="cat /tmp/a/one.txt"
        ),
        _tool_part(
            "p-br-4", "sess_br1", "Read", command="cat /tmp/a/two.txt"
        ),
    ]
    # All parts share one timestamp, so enforce the intended call order
    # (br-1, br-2, then br-3, br-4) via an earlier message row.
    conn.executemany(
        "INSERT INTO part (id, message_id, session_id, time_created, data) "
        "VALUES (?, ?, ?, ?, ?)",
        parts,
    )
    conn.execute(
        "INSERT INTO message VALUES "
        "('m-br0', 'sess_br1', 1_795_353_950_000, '{}')"
    )
    conn.execute(
        "UPDATE part SET message_id='m-br0' WHERE id IN ('p-br-1','p-br-2')"
    )
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


# ------------------------------------------------------- stage 2: ll / br


def test_ll_overspending_builder(struggle_db: Path) -> None:
    records = struggle(db=str(struggle_db), kinds=("ll",))
    assert [r["session_id"] for r in records] == ["sess_ll1"]
    rec = records[0]
    assert rec["severity"] == "high"  # errored overspending turn
    assert "tool_calls=280" in rec["evidence"]


def test_ll_below_threshold_not_reported(struggle_db: Path) -> None:
    conn = sqlite3.connect(str(struggle_db))
    conn.execute(
        "UPDATE turn_usage SET tool_call_count=240 WHERE session_id='sess_ll1'"
    )
    conn.commit()
    conn.close()
    assert struggle(db=str(struggle_db), kinds=("ll",)) == []


def test_ll_completed_turn_is_medium(struggle_db: Path) -> None:
    conn = sqlite3.connect(str(struggle_db))
    conn.execute(
        "UPDATE turn_usage SET status='completed' "
        "WHERE session_id='sess_ll1'"
    )
    conn.commit()
    conn.close()
    records = struggle(db=str(struggle_db), kinds=("ll",))
    assert records[0]["severity"] == "medium"


def test_br_normalized_duplicate_after_error(struggle_db: Path) -> None:
    records = struggle(db=str(struggle_db), kinds=("br",))
    assert [r["session_id"] for r in records] == ["sess_br1"]
    rec = records[0]
    assert rec["kind"] == "br"
    assert "Bash" in rec["evidence"]
    # The pair differs only in digits/hashes: matched via normalization.
    assert "p-br-1" in rec["evidence"] and "p-br-2" in rec["evidence"]


def test_br_scope_limited_to_struggle_candidates(struggle_db: Path) -> None:
    """BR scans only errored/signalled sessions (v1 cost bound)."""
    conn = sqlite3.connect(str(struggle_db))
    # An errored Bash call + verbatim dup inside the healthy session.
    conn.execute(
        "INSERT INTO message VALUES "
        "('m-ok0', 'sess_ok', 1_795_353_900_000, '{}')"
    )
    for pid in ("p-ok-1", "p-ok-2"):
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, "
            "data) VALUES (?, 'm-ok0', 'sess_ok', 1_795_353_900_000, ?)",
            (pid, json.dumps({
                "type": "tool", "callID": f"call_{pid}", "tool": "Bash",
                "state": {"status": "error", "input": {"command": "ls /x"}},
            })),
        )
    conn.commit()
    conn.close()
    records = struggle(db=str(struggle_db), kinds=("br",))
    assert {r["session_id"] for r in records} == {"sess_br1"}


def test_br_session_pin_scans_any_session(struggle_db: Path) -> None:
    """--session opts in explicitly, even without other signals."""
    conn = sqlite3.connect(str(struggle_db))
    conn.execute(
        "INSERT INTO message VALUES "
        "('m-ok0', 'sess_ok', 1_795_353_900_000, '{}')"
    )
    for pid in ("p-ok-1", "p-ok-2"):
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, "
            "data) VALUES (?, 'm-ok0', 'sess_ok', 1_795_353_900_000, ?)",
            (pid, json.dumps({
                "type": "tool", "callID": f"call_{pid}", "tool": "Bash",
                "state": {"status": "error", "input": {"command": "ls /x"}},
            })),
        )
    conn.commit()
    conn.close()
    records = struggle(db=str(struggle_db), kinds=("br",), session="sess_ok")
    assert {r["session_id"] for r in records} == {"sess_ok"}


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
    assert {r["kind"] for r in payload} == {"sd", "qd", "ll", "br"}
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
