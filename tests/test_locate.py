"""Hermetic tests for the ``locate`` preset (stage 4).

Synthetic sessions only, under the autouse ``AI_R_HOME`` isolation; the
``--web`` sources (``SW_HOME`` / ``.claude.json``) are faked inside the same
temp home — no host data is ever read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.locate import locate


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


def _claude_session(root: Path, session_id: str, title: str, ts: str) -> None:
    _write_jsonl(
        root / ".claude" / "projects" / "proj-a" / f"{session_id}.jsonl",
        [
            {
                "type": "user",
                "message": {"role": "user", "content": title},
                "timestamp": ts,
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                },
                "timestamp": ts,
                "sessionId": session_id,
            },
        ],
    )


OLD_ID = "aaaa1111-0000-4000-8000-000000000001"
NEW_ID = "bbbb2222-0000-4000-8000-000000000002"


@pytest.fixture
def two_sessions(tmp_sessions_dir: Path) -> Path:
    _claude_session(
        tmp_sessions_dir, OLD_ID, "Deploy pipeline alpha", "2026-06-10T09:00:00Z"
    )
    _claude_session(
        tmp_sessions_dir, NEW_ID, "Deploy pipeline beta", "2026-06-20T09:00:00Z"
    )
    return tmp_sessions_dir


def test_full_uuid_and_prefix_match(two_sessions: Path) -> None:
    exact = locate(OLD_ID)
    assert exact["count"] == 1
    (rec,) = exact["matches"]
    assert rec["uuid"] == OLD_ID
    assert rec["match"] == "id"
    assert rec["agent"] == "claude"
    assert rec["readable"] is True
    assert rec["read_command"] == f"ai-r read {OLD_ID} --agent claude"
    assert rec["path"].endswith(f"{OLD_ID}.jsonl")
    assert isinstance(rec["size_bytes"], int) and rec["size_bytes"] > 0

    prefix = locate("bbbb2222")  # the 8-hex head
    assert prefix["count"] == 1
    assert prefix["matches"][0]["uuid"] == NEW_ID
    assert prefix["matches"][0]["match"] == "id"


def test_title_substring_case_insensitive_ranked_mtime_desc(
    two_sessions: Path,
) -> None:
    # agent-scoped: the OpenCode parser reads the host DB regardless of
    # AI_R_HOME (known leak) — an unscoped count would be host-dependent.
    result = locate("DEPLOY PIPELINE", agent="claude")
    assert result["count"] == 2
    uuids = [rec["uuid"] for rec in result["matches"]]
    # Newest first (mtime/last-activity descending).
    assert uuids == [NEW_ID, OLD_ID]
    assert {rec["match"] for rec in result["matches"]} == {"title"}


def test_limit_and_truncated(two_sessions: Path) -> None:
    result = locate("deploy", agent="claude", limit=1)
    assert result["count"] == 2  # full total survives the cap
    assert len(result["matches"]) == 1
    assert result["truncated"] is True
    assert result["matches"][0]["uuid"] == NEW_ID


def test_zero_match_suggestions_and_diagnostics(two_sessions: Path) -> None:
    # close to the fixture titles; agent-scoped for hermeticity (see above)
    result = locate("Deploy pipeline gamme", agent="claude")
    assert result["count"] == 0
    assert result["matches"] == []
    assert "Deploy pipeline alpha" in result["suggestions"] or (
        "Deploy pipeline beta" in result["suggestions"]
    )
    assert "diagnostics" in result
    # A needle nothing resembles: honest empty suggestions, never invented.
    far = locate("zzzzzzzzzzzzzzzz", agent="claude")
    assert far["count"] == 0
    assert far["suggestions"] == []


def test_invalid_arguments_fail_loud(two_sessions: Path) -> None:
    with pytest.raises(ValueError):
        locate("")
    with pytest.raises(ValueError):
        locate("   ")
    with pytest.raises(ValueError):
        locate("x", limit=-1)
    with pytest.raises(ValueError):
        locate("x", web="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        locate("x", agent="not-an-agent")


def test_web_block_with_fake_sources(
    two_sessions: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (a) hook-export dir via explicit SW_HOME.
    sw_home = tmp_path / "sw"
    exports = sw_home / "web-sessions"
    exports.mkdir(parents=True)
    export_file = exports / "web-cafe1234-export.json"
    export_file.write_text('{"messages": []}', encoding="utf-8")
    monkeypatch.setenv("SW_HOME", str(sw_home))

    # (b) teleport stub in the (isolated) home's .claude.json.
    stub_id = "cafe1234-0000-4000-8000-00000000beef"
    (two_sessions / ".claude.json").write_text(
        json.dumps({"projects": {"/repo/x": {"lastSessionId": stub_id}}}),
        encoding="utf-8",
    )

    result = locate("cafe1234", web=True)
    web = result["web"]
    assert web["sources"]["exports_dir_found"] is True
    assert web["sources"]["claude_json_found"] is True
    (export,) = web["exports"]
    assert export["path"] == str(export_file)
    assert export["source"] == "hook_export"
    assert export["readable"] is True
    (stub,) = web["stubs"]
    assert stub["uuid"] == stub_id
    assert stub["project_dir"] == "/repo/x"
    assert stub["content_local"] is False
    assert "scope_note" in web

    # web=False → no web key at all (byte-identical to before).
    assert "web" not in locate("cafe1234", web=False)


def test_web_block_missing_sources_and_malformed_json(
    two_sessions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SW_HOME", raising=False)
    result = locate("anything", web=True)
    web = result["web"]
    # Default SW_HOME under the isolated home — absent, skipped honestly.
    assert web["sources"]["exports_dir_found"] is False
    assert web["exports"] == []
    assert web["stubs"] == []

    # A malformed .claude.json degrades to an honest error note.
    (two_sessions / ".claude.json").write_text("{not json", encoding="utf-8")
    broken = locate("anything", web=True)["web"]
    assert broken["sources"]["claude_json_found"] is True
    assert "claude_json_error" in broken["sources"]
    assert broken["stubs"] == []


def test_mcp_wrapper_error_contract(two_sessions: Path) -> None:
    from ai_r.mcp_server import locate as mcp_locate

    bad = mcp_locate("")
    assert bad["error"] == "invalid_argument"
    ok = mcp_locate("deploy", agent="claude")
    assert ok["count"] == 2


def test_cli_human_and_json(
    two_sessions: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_r.cli.main import main

    assert main(["locate", "deploy pipeline", "--agent", "claude"]) == 0
    out = capsys.readouterr().out
    assert NEW_ID in out and OLD_ID in out
    assert "read:" in out

    assert main(["locate", "deploy pipeline", "--agent", "claude", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2

    assert main(["locate", ""]) == 2
    assert capsys.readouterr().err.startswith("ai-r: ")
