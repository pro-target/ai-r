"""Tests for the Antigravity brain parser.

When the host has no Antigravity brain dir, the ``real_antigravity_root``
fixture auto-skips these real-data tests (see conftest).  The synthetic
fixture in ``conftest.py`` covers the parser logic in isolation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_r.parsers import AgentName, antigravity
from ai_r.parsers.antigravity import (
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


def test_list_sessions_real(real_antigravity_root: Path) -> None:
    # ``real_antigravity_root`` auto-skips when no brain dir exists (conftest).
    sessions = antigravity.list_sessions(base_dir=str(real_antigravity_root))
    assert isinstance(sessions, list)
    for s in sessions[:3]:
        assert s.agent is AgentName.ANTIGRAVITY
        assert s.title


def test_scan_brain_real_smoke(real_antigravity_root: Path) -> None:
    # ``real_antigravity_root`` auto-skips when no brain dir exists (conftest).
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


def test_resolve_brain_roots_uses_ai_r_home(tmp_path: Path) -> None:
    # _isolate_ai_r_home fixture points AI_R_HOME at tmp_path.
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


# ---------------------------------------------------------------------------
# Protobuf wire helpers + synthetic v1.5+ fixtures
# ---------------------------------------------------------------------------


def _pb_varint(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_uint(field_num: int, val: int) -> bytes:
    return _pb_varint((field_num << 3) | 0) + _pb_varint(val)


def _pb_bytes(field_num: int, raw: bytes) -> bytes:
    return _pb_varint((field_num << 3) | 2) + _pb_varint(len(raw)) + raw


def _pb_str(field_num: int, text: str) -> bytes:
    return _pb_bytes(field_num, text.encode("utf-8"))


def _build_trajectory_meta_blob(
    *,
    project_uri_f1: str | None = None,
    project_uri_f7: str | None = None,
    created_sec: int | None = None,
    created_nanos: int = 0,
    parent_uuid: str | None = None,
    root_uuid: str | None = None,
    subagent_type: str | None = None,
    subagent_role: str | None = None,
    subagent_prompt: str | None = None,
    spawn_depth: int | None = None,
) -> bytes:
    chunks: list[bytes] = []
    if project_uri_f1 is not None:
        chunks.append(_pb_bytes(1, _pb_str(1, project_uri_f1)))
    if created_sec is not None:
        ts_msg = _pb_uint(1, created_sec) + _pb_uint(2, created_nanos)
        chunks.append(_pb_bytes(2, ts_msg))
    if parent_uuid is not None:
        chunks.append(_pb_str(5, parent_uuid))
    if root_uuid is not None:
        chunks.append(_pb_str(6, root_uuid))
    if project_uri_f7 is not None:
        chunks.append(_pb_str(7, project_uri_f7))
    if any(x is not None for x in (subagent_type, subagent_role, subagent_prompt)):
        f8 = b""
        if subagent_type is not None:
            f8 += _pb_str(1, subagent_type)
        if subagent_role is not None:
            f8 += _pb_str(2, subagent_role)
        if subagent_prompt is not None:
            f8 += _pb_str(3, subagent_prompt)
        chunks.append(_pb_bytes(8, f8))
    if spawn_depth is not None:
        chunks.append(_pb_uint(17, spawn_depth))
    return b"".join(chunks)


def _write_conv_db(db_path: Path, blob: bytes, blob_id: str = "main") -> None:
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)"
    )
    conn.execute(
        "INSERT INTO trajectory_metadata_blob (id, data) VALUES (?, ?)",
        (blob_id, blob),
    )
    conn.commit()
    conn.close()


def _write_jsonl_lines(path: Path, records: list[dict]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 4 roots + deduplication + launch_surface
# ---------------------------------------------------------------------------


def test_four_roots_discovery_and_launch_surface(tmp_sessions_dir: Path) -> None:
    gemini = tmp_sessions_dir / ".gemini"
    specs = [
        ("antigravity", "u-main-ide", "antigravity-ide"),
        ("antigravity-cli", "u-cli", "antigravity-cli"),
        ("antigravity-ide", "u-ide-2", "antigravity-ide"),
        ("antigravity-backup", "u-backup", None),
    ]
    for root_name, uid, _ in specs:
        _write_jsonl_lines(
            gemini / root_name / "brain" / uid
            / ".system_generated" / "logs" / "transcript_full.jsonl",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-08-01T10:00:00Z",
                    "content": f"Task in {root_name}",
                }
            ],
        )

    roots = antigravity.source_roots()
    for root_name, _, _ in specs:
        assert str(gemini / root_name / "brain") in roots

    by_uuid = {s.uuid: s for s in antigravity.list_sessions()}
    assert set(by_uuid) == {"u-main-ide", "u-cli", "u-ide-2", "u-backup"}
    for _, uid, expected_surface in specs:
        assert by_uuid[uid].launch_surface == expected_surface


def test_dedup_across_roots_priority_and_richness(tmp_sessions_dir: Path) -> None:
    import os

    gemini = tmp_sessions_dir / ".gemini"
    dup_same_tier = "dup-root-prio-1"
    # Same richness (both have transcript_full.jsonl), backup has NEWER mtime,
    # yet ``antigravity`` must win over ``antigravity-backup``.
    p_main = (
        gemini / "antigravity" / "brain" / dup_same_tier
        / ".system_generated" / "logs" / "transcript_full.jsonl"
    )
    p_bak = (
        gemini / "antigravity-backup" / "brain" / dup_same_tier
        / ".system_generated" / "logs" / "transcript_full.jsonl"
    )
    _write_jsonl_lines(
        p_main,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-01T10:00:00Z",
                "content": "From main root",
            }
        ],
    )
    _write_jsonl_lines(
        p_bak,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-01T10:00:00Z",
                "content": "From backup root",
            }
        ],
    )
    os.utime(p_main, (1_700_000_000, 1_700_000_000))
    os.utime(p_bak, (1_800_000_000, 1_800_000_000))

    # Richness wins first: ``antigravity`` has only overview.txt, while
    # ``antigravity-backup`` has transcript_full.jsonl + .db.
    dup_rich = "dup-richness-wins-1"
    _write_jsonl_lines(
        gemini / "antigravity" / "brain" / dup_rich
        / ".system_generated" / "logs" / "overview.txt",
        [
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-08-01T10:00:00Z",
                "content": "Stale overview only",
            }
        ],
    )
    _write_jsonl_lines(
        gemini / "antigravity-backup" / "brain" / dup_rich
        / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-01T11:00:00Z",
                "content": "Full transcript in backup",
            }
        ],
    )
    _write_conv_db(
        gemini / "antigravity-backup" / "conversations" / f"{dup_rich}.db",
        _build_trajectory_meta_blob(project_uri_f1="file:///repo/from-bak"),
    )

    sessions = antigravity.list_sessions()
    assert len([s for s in sessions if s.uuid == dup_same_tier]) == 1
    assert len([s for s in sessions if s.uuid == dup_rich]) == 1
    by_uuid = {s.uuid: s for s in sessions}

    assert by_uuid[dup_same_tier].title == "From main root"
    assert by_uuid[dup_same_tier].launch_surface == "antigravity-ide"
    assert antigravity.read_session(dup_same_tier).title == "From main root"

    assert by_uuid[dup_rich].title == "Full transcript in backup"
    assert by_uuid[dup_rich].project_dir == "/repo/from-bak"
    assert antigravity.read_session(dup_rich).project_dir == "/repo/from-bak"


# ---------------------------------------------------------------------------
# Binary layer (.db) + 3 fallbacks (.pb, conversation_summaries.db, subagents/*.json)
# ---------------------------------------------------------------------------


def test_db_metadata_top_level_and_subagent(tmp_sessions_dir: Path) -> None:
    from datetime import datetime, timezone

    gemini = tmp_sessions_dir / ".gemini" / "antigravity"
    top_id = "11111111-1111-4111-8111-111111111111"
    sub_id = "22222222-2222-4222-8222-222222222222"
    sub_prompt_only_id = "33333333-3333-4333-8333-333333333333"

    # Top-level session: project_dir via f1.1 (percent-encoded), date fallback
    # via f2.1+f2.2 (no created_at/timestamp in transcript).
    _write_jsonl_lines(
        gemini / "brain" / top_id / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Inspect workspace",
            }
        ],
    )
    _write_conv_db(
        gemini / "conversations" / f"{top_id}.db",
        _build_trajectory_meta_blob(
            project_uri_f1="file:///home/user/my%20proj",
            created_sec=1_780_000_000,
            created_nanos=500_000_000,
            root_uuid=top_id,
        ),
    )

    # Subagent with f5, f8.1, f8.2, f17 and project_dir via f7 only (no USER_INPUT).
    _write_jsonl_lines(
        gemini / "brain" / sub_id / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-08-02T12:00:00Z",
                "content": "Subagent report ready.",
            }
        ],
    )
    _write_conv_db(
        gemini / "conversations" / f"{sub_id}.db",
        _build_trajectory_meta_blob(
            project_uri_f7="file:///home/user/subproj",
            created_sec=1_780_000_100,
            parent_uuid=top_id,
            root_uuid=top_id,
            subagent_type="research",
            subagent_role="Architecture Researcher",
            subagent_prompt="Read all modules and report",
            spawn_depth=1,
        ),
    )

    # Subagent without f5 (only root_uuid != uuid) and without f8.2 (falls back to f8.3).
    _write_jsonl_lines(
        gemini / "brain" / sub_prompt_only_id
        / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-08-02T12:05:00Z",
                "content": "Done.",
            }
        ],
    )
    _write_conv_db(
        gemini / "conversations" / f"{sub_prompt_only_id}.db",
        _build_trajectory_meta_blob(
            root_uuid=top_id,
            subagent_type="builder",
            subagent_prompt="Implement the parser\nin full",
            spawn_depth=2,
        ),
    )

    top = antigravity.read_session(top_id)
    assert top.project_dir == "/home/user/my proj"
    assert top.kind == "agent"
    assert top.parent_uuid is None
    assert top.date == datetime.fromtimestamp(1_780_000_000.5, tz=timezone.utc)

    sub = antigravity.read_session(sub_id)
    assert sub.project_dir == "/home/user/subproj"
    assert sub.kind == "subagent"
    assert sub.parent_uuid == top_id
    assert sub.title == "Architecture Researcher"
    assert sub.extra["subagent_type"] == "research"
    assert sub.extra["spawn_depth"] == 1

    sub2 = antigravity.read_session(sub_prompt_only_id)
    assert sub2.kind == "subagent"
    assert sub2.parent_uuid is None
    assert sub2.title == "Implement the parser in full"
    assert sub2.extra["subagent_type"] == "builder"
    assert sub2.extra["spawn_depth"] == 2


def test_fallback_1_pb_valid_and_corrupt(tmp_sessions_dir: Path) -> None:
    gemini = tmp_sessions_dir / ".gemini" / "antigravity"
    uid_ok = "pb-valid-1"
    uid_bad = "pb-corrupt-1"

    for uid in (uid_ok, uid_bad):
        _write_jsonl_lines(
            gemini / "brain" / uid / ".system_generated" / "logs" / "overview.txt",
            [
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-08-01T10:00:00Z",
                    "content": "ok",
                }
            ],
        )

    pb_dir = gemini / "conversations"
    pb_dir.mkdir(parents=True, exist_ok=True)
    (pb_dir / f"{uid_ok}.pb").write_bytes(
        _build_trajectory_meta_blob(
            project_uri_f1="file:///srv/pb-app",
            parent_uuid="pb-parent-0",
            subagent_type="explorer",
            subagent_role="PB Explorer",
            spawn_depth=1,
        )
    )
    # Encrypted/corrupt raw bytes must fail soft without raising.
    (pb_dir / f"{uid_bad}.pb").write_bytes(b"\xaf\x01\x0e\xc3\xff\xfe\x00\x80\x80")

    s_ok = antigravity.read_session(uid_ok)
    assert s_ok.project_dir == "/srv/pb-app"
    assert s_ok.parent_uuid == "pb-parent-0"
    assert s_ok.kind == "subagent"
    assert s_ok.title == "PB Explorer"
    assert s_ok.extra["subagent_type"] == "explorer"
    assert s_ok.extra["spawn_depth"] == 1

    s_bad = antigravity.read_session(uid_bad)
    assert s_bad.project_dir is None
    assert s_bad.kind == "agent"


def test_fallback_2_conversation_summaries_db(tmp_sessions_dir: Path) -> None:
    import json
    import sqlite3

    gemini = tmp_sessions_dir / ".gemini" / "antigravity-cli"
    uid = "sum-sub-1"
    _write_jsonl_lines(
        gemini / "brain" / uid / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Summary-backed session.",
            }
        ],
    )
    sdb = gemini / "conversation_summaries.db"
    conn = sqlite3.connect(str(sdb))
    conn.executescript(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT "",
            preview TEXT NOT NULL DEFAULT "",
            last_modified_time TEXT NOT NULL,
            workspace_uris TEXT NOT NULL,
            agent_name TEXT NOT NULL DEFAULT "",
            parent_conversation_id TEXT NOT NULL DEFAULT "",
            nesting_depth INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE conversation_parents (
            conversation_id TEXT PRIMARY KEY,
            parent_id TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            "Summary Subagent Title",
            "Preview text",
            "2026-06-21 09:15:09.124768443+00:00",
            json.dumps(["file:///home/dev/summary-repo"]),
            "auditor",
            "",  # leave empty here so conversation_parents fallback is also exercised
            2,
        ),
    )
    conn.execute(
        "INSERT INTO conversation_parents VALUES (?, ?)",
        (uid, "sum-parent-root"),
    )
    conn.commit()
    conn.close()

    s = antigravity.read_session(uid)
    assert s.project_dir == "/home/dev/summary-repo"
    assert s.parent_uuid == "sum-parent-root"
    assert s.kind == "subagent"
    assert s.title == "Summary Subagent Title"
    assert s.extra["subagent_type"] == "auditor"
    assert s.extra["spawn_depth"] == 2
    assert s.date.year == 2026 and s.date.month == 6 and s.date.day == 21


def test_fallback_3_subagents_json_in_parent_brain(tmp_sessions_dir: Path) -> None:
    import json

    brain_root = tmp_sessions_dir / ".gemini" / "antigravity" / "brain"
    parent_id = "parent-brain-1"
    child_id = "child-brain-1"

    _write_jsonl_lines(
        brain_root / parent_id / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-03T09:00:00Z",
                "content": "Spawn a helper",
            }
        ],
    )
    sub_meta = brain_root / parent_id / ".system_generated" / "subagents" / f"{child_id}.json"
    sub_meta.parent.mkdir(parents=True, exist_ok=True)
    sub_meta.write_text(
        json.dumps(
            {
                "conversationId": child_id,
                "spawnStepIndex": 1,
                "state": "SUBAGENT_STATE_ALIVE",
                "subagentDescriptor": {
                    "typeName": "transcript-reader",
                    "role": "Transcript Reader Subagent",
                },
                "workspaceUris": ["file:///home/dev/parent-ws"],
            }
        ),
        encoding="utf-8",
    )

    _write_jsonl_lines(
        brain_root / child_id / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-08-03T09:01:00Z",
                "content": "Child finished reading.",
            }
        ],
    )

    child = antigravity.read_session(child_id)
    assert child.parent_uuid == parent_id
    assert child.kind == "subagent"
    assert child.title == "Transcript Reader Subagent"
    assert child.project_dir == "/home/dev/parent-ws"
    assert child.extra["subagent_type"] == "transcript-reader"


# ---------------------------------------------------------------------------
# Non-monotonic step_index + PLANNER_RESPONSE <-> MODEL/GENERIC correlation
# ---------------------------------------------------------------------------


def test_non_monotonic_step_index_and_tool_correlation(tmp_sessions_dir: Path) -> None:
    uid = "ag-corr-1"
    transcript = (
        tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / uid
        / ".system_generated" / "logs" / "transcript_full.jsonl"
    )
    # Intentionally written in out-of-order step_index: 0, 2, 3, 1 + SYSTEM noise.
    _write_jsonl_lines(
        transcript,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-04T10:00:00Z",
                "content": "Read a.py and run tests",
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-08-04T10:00:03Z",
                "content": "def foo(): pass\n",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "ERROR",
                "created_at": "2026-08-04T10:00:05Z",
                "error": "timeout",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-08-04T10:00:01Z",
                "thinking": "Check file first, then run pytest.",
                "model": "gemini-4-neon-high",
                "tool_calls": [
                    {"name": "view_file", "args": {"AbsolutePath": "/repo/a.py"}},
                    {"name": "run_command", "args": {"CommandLine": "pytest"}},
                ],
            },
            {
                "step_index": 4,
                "source": "SYSTEM",
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "created_at": "2026-08-04T10:00:06Z",
                "content": "system note",
            },
        ],
    )

    msgs = antigravity.read_messages(uid)
    # Must NOT emit bogus assistant messages from MODEL/GENERIC or SYSTEM.
    assert [m.role for m in msgs] == ["user", "assistant"]
    assistant = msgs[1]
    assert assistant.text == ""
    assert assistant.thinking == "Check file first, then run pytest."
    assert assistant.model == "gemini-4-neon-high"

    assert len(assistant.tool_use) == 2
    assert assistant.tool_use[0]["name"] == "view_file"
    assert assistant.tool_use[0]["tool_use_id"] == "ag-step-1-0"
    assert assistant.tool_use[1]["name"] == "run_command"
    assert assistant.tool_use[1]["tool_use_id"] == "ag-step-1-1"

    assert assistant.tool_result == (
        {
            "content": "def foo(): pass\n",
            "is_error": False,
            "tool_use_id": "ag-step-1-0",
        },
        {
            "content": "timeout",
            "is_error": True,
            "tool_use_id": "ag-step-1-1",
        },
    )


def test_orphan_model_generic_becomes_tool_role_not_assistant(
    tmp_sessions_dir: Path,
) -> None:
    uid = "ag-orphan-generic-1"
    transcript = (
        tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / uid
        / ".system_generated" / "logs" / "transcript_full.jsonl"
    )
    _write_jsonl_lines(
        transcript,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-04T10:00:00Z",
                "content": "Start",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-08-04T10:00:01Z",
                "content": "unmatched output",
            },
        ],
    )
    msgs = antigravity.read_messages(uid)
    assert len(msgs) == 2
    assert msgs[1].role == "tool"
    assert msgs[1].text == ""
    assert msgs[1].tool_result == (
        {"content": "unmatched output", "is_error": False},
    )


# ---------------------------------------------------------------------------
# Transcript priority, created_at, thinking, model, tokens, media, truncated tail
# ---------------------------------------------------------------------------


def test_transcript_full_wins_over_truncated_siblings(
    tmp_sessions_dir: Path,
) -> None:
    uid = "ag-prio-files-1"
    logs = (
        tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / uid
        / ".system_generated" / "logs"
    )
    _write_jsonl_lines(
        logs / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-05T10:00:00Z",
                "content": "Canonical full user prompt",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-08-05T10:00:05Z",
                "content": "Full un-truncated answer",
            },
        ],
    )
    for other_name in ("transcript.json", "transcript.jsonl", "overview.txt"):
        _write_jsonl_lines(
            logs / other_name,
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-08-05T10:00:00Z",
                    "content": f"Truncated from {other_name}",
                }
            ],
        )

    sess = antigravity.read_session(uid)
    assert sess.title == "Canonical full user prompt"
    msgs = antigravity.read_messages(uid)
    assert len(msgs) == 2
    assert msgs[1].text == "Full un-truncated answer"


def test_rich_fields_media_tokens_bool_guard_and_truncated_tail(
    tmp_sessions_dir: Path,
) -> None:
    import json

    uid = "ag-rich-fields-1"
    brain = tmp_sessions_dir / ".gemini" / "antigravity" / "brain" / uid
    transcript = brain / ".system_generated" / "logs" / "transcript_full.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)

    valid_lines = [
        json.dumps(
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-08-06T12:00:00Z",
                "content": "Look at this screenshot",
                "media": [
                    {
                        "mime_type": "image/png",
                        "uri": "file:///tmp/shot%201.png",
                    }
                ],
            }
        ),
        json.dumps(
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-08-06T12:00:05Z",
                "thinking": "Analyzing screenshot pixels.",
                "content": "I see the button.",
                "model": "gemini-4-neon-high",
                "input_tokens": 120,
                "output_tokens": 30,
                "cache_read_tokens": 500,
            }
        ),
        # Bool values in token slots must NOT be counted as 1; all-zero -> None.
        json.dumps(
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-08-06T12:00:10Z",
                "content": "Follow-up without real usage.",
                "model": "gemini-4-flash",
                "input_tokens": True,
                "output_tokens": False,
                "cache_read_tokens": 0,
            }
        ),
    ]
    # Append a broken JSON line AND a truncated tail without trailing newline.
    raw_payload = (
        "\n".join(valid_lines)
        + "\n{not valid json line\n"
        + '{"step_index": 99, "source": "MODEL", "content": "cut-off'
    )
    transcript.write_text(raw_payload, encoding="utf-8")

    # Corrupt SQLite .db next to it must also fail soft without crashing.
    bad_db = tmp_sessions_dir / ".gemini" / "antigravity" / "conversations" / f"{uid}.db"
    bad_db.parent.mkdir(parents=True, exist_ok=True)
    bad_db.write_bytes(b"corrupt-not-a-sqlite-file")

    sess = antigravity.read_session(uid)
    assert sess.models == ("gemini-4-neon-high", "gemini-4-flash")
    assert sess.date.year == 2026 and sess.date.month == 8 and sess.date.day == 6

    msgs = antigravity.read_messages(uid)
    assert len(msgs) == 3

    assert msgs[0].role == "user"
    assert msgs[0].timestamp is not None
    assert msgs[0].user_refs == (
        {"kind": "image", "target": "/tmp/shot 1.png", "origin": "structured"},
    )

    assert msgs[1].role == "assistant"
    assert msgs[1].text == "I see the button."
    assert msgs[1].thinking == "Analyzing screenshot pixels."
    assert "Analyzing" not in msgs[1].text
    assert msgs[1].model == "gemini-4-neon-high"
    assert msgs[1].tokens == {
        "input": 120,
        "output": 30,
        "reasoning": 0,
        "cache_read": 500,
        "cache_write": 0,
        "total": 650,
    }

    assert msgs[2].tokens is None
    assert msgs[2].model == "gemini-4-flash"

    assert antigravity.read_token_usage(uid) == {
        "input": 120,
        "output": 30,
        "reasoning": 0,
        "cache_read": 500,
        "cache_write": 0,
        "total": 650,
    }
