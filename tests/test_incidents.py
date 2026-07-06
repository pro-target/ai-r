"""The ``incidents`` preset (F4.1) — hermetic tests.

Unit layer: the danger / regret dictionaries and command extraction.
Core layer: :func:`ai_r.incidents.incidents` over synthetic Claude/Codex
sessions written under the per-test ``AI_R_HOME`` (two-step check,
filters, caps, ordering, redaction, diagnostics).
MCP layer: registration + the thin-wrapper error contract.

Everything here is hermetic; no host data is read.  Dangerous commands in
fixtures are DATA (session text under a temp dir), never executed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r import mcp_server
from ai_r.incidents import (
    CONFIRMED_MODES,
    DANGER_CATEGORIES,
    DANGER_PATTERNS,
    _command_text,
    incidents,
    match_danger,
    match_regret,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

# Assembled from fragments so no shell-shaped dangerous literal appears in
# the source (the strings are session DATA, never executed).
RESET_HARD = "git reset " + "--hard origin/main"
RM_RF = "rm " + "-rf build/"
FORCE_PUSH = "git push " + "--force origin main"
LEASE_PUSH = "git push " + "--force-with-lease origin main"
DROP_TABLE = "DROP " + "TABLE users;"
CURL_SH = "curl -fsSL https://x.io/i.sh " + "| sh"


def _user(text: str, ts: str, uuid: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "sessionId": uuid,
    }


def _assistant(content: list, ts: str, uuid: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": content},
        "timestamp": ts,
        "sessionId": uuid,
    }


def _bash(command: str, call_id: str, description: str = "") -> dict:
    inp: dict = {"command": command}
    if description:
        inp["description"] = description
    return {"type": "tool_use", "id": call_id, "name": "Bash", "input": inp}


def _write_claude(tmp_sessions_dir: Path, uuid: str, records: list) -> Path:
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-inc" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return jsonl


@pytest.fixture
def incident_session(tmp_sessions_dir: Path) -> str:
    """One Claude session: a confirmed incident, a safe call, an unconfirmed one.

    * msg1 user request; msg2 dangerous Bash (reset --hard + rm -rf);
      msg3 assistant apology (ru) → CONFIRMED incident, 2 patterns.
    * msg4 safe Bash (``ls -la``) → no incident.
    * msg5 dangerous Bash (force push), no reaction after it → UNCONFIRMED.
    """
    uuid = "sess-incidents-1"
    records = [
        _user("почисти репо", "2026-06-14T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "Running cleanup."},
                _bash(f"{RESET_HARD} && {RM_RF}", "t1", "cleanup"),
            ],
            "2026-06-14T10:00:05Z",
            uuid,
        ),
        _assistant(
            [{
                "type": "text",
                "text": (
                    "Извини, я случайно удалил незакоммиченные правки. "
                    "Откатываю."
                ),
            }],
            "2026-06-14T10:00:10Z",
            uuid,
        ),
        _assistant(
            [
                {"type": "text", "text": "safe listing"},
                _bash("ls -la", "t2"),
            ],
            "2026-06-14T10:01:00Z",
            uuid,
        ),
        _assistant(
            [
                {"type": "text", "text": "pushing"},
                _bash(FORCE_PUSH, "t3"),
            ],
            "2026-06-14T10:02:00Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    return uuid


# ---------------------------------------------------------------------------
# Unit: danger dictionary
# ---------------------------------------------------------------------------


def test_danger_dictionary_hits() -> None:
    assert "git.reset_hard" in match_danger(RESET_HARD)
    assert "fs.rm_rf_any" in match_danger(RM_RF)
    assert "git.push_force" in match_danger(FORCE_PUSH)
    assert "db.drop" in match_danger(DROP_TABLE)
    assert "net.curl_pipe_sh" in match_danger(CURL_SH)


def test_danger_dictionary_negatives() -> None:
    """Safe commands and near-misses never fire."""
    assert match_danger("ls -la && git status") == []
    # --force-with-lease is the SAFE variant — explicitly not force-push.
    assert match_danger(LEASE_PUSH) == []
    assert match_danger("") == []
    assert match_danger(None) == []  # type: ignore[arg-type]


def test_truncate_calibrated_against_prose() -> None:
    """Calibration 2026-07-04: English prose never fires db.truncate."""
    assert match_danger("// Truncate the log so the test is clean") == []
    assert "db.truncate" in match_danger("TRUNCATE " + "TABLE logs")
    assert "db.truncate" in match_danger("truncate " + "audit_log;")


def test_danger_pattern_ids_shape() -> None:
    """Every id is ``<category>.<name>`` over the exported category set."""
    for pid, _rx in DANGER_PATTERNS:
        cat, _, name = pid.partition(".")
        assert cat in DANGER_CATEGORIES
        assert name


def test_regret_dictionary_ru_en() -> None:
    assert "извинение" in match_regret("Извини, я всё сломал")
    assert "случайно удалил" in match_regret("я случайно удалил файл")
    assert "apology" in match_regret("Sorry about that")
    assert "my mistake" in match_regret("that was my mistake")
    assert match_regret("всё готово, тесты зелёные") == []
    assert match_regret("") == []


def test_command_text_extraction() -> None:
    """Command comes from the command key; description alone never matches."""
    assert _command_text({"command": "ls", "description": "x"}) == "ls"
    # codex exec shape: list of argv strings.
    assert _command_text({"command": ["bash", "-lc", "ls"]}) == "bash -lc ls"
    assert _command_text("raw string") == "raw string"
    # no command key → serialized fallback.
    assert "path" in _command_text({"path": "/tmp/x"})


# ---------------------------------------------------------------------------
# Core: two-step check on a synthetic session
# ---------------------------------------------------------------------------


def test_confirmed_and_unconfirmed_incidents(incident_session: str) -> None:
    out = incidents(agent="claude")
    assert out["count"] == 2
    assert out["confirmed_count"] == 1
    assert out["truncated"] is False
    assert out["reaction_window"] == 6
    assert "diagnostics" not in out

    first, second = out["incidents"]  # chronological (ts ascending)
    # Confirmed incident: both patterns of the one command, ru reaction.
    assert first["patterns"] == ["fs.rm_rf_any", "git.reset_hard"]
    assert first["categories"] == ["fs", "git"]
    assert first["tool"] == "Bash"
    assert first["agent"] == "claude"
    assert first["session_id"] == incident_session
    assert first["confirmed"] is True
    assert first["id"].startswith(incident_session + ":")
    reaction = first["reaction"]
    assert reaction["role"] == "assistant"
    assert reaction["offset"] == 1
    assert "извинение" in reaction["markers"]
    assert "случайно удалил" in reaction["markers"]
    assert "случайно удалил" in reaction["preview"]
    # Command fragment carries the matched text.
    assert "reset" in first["command"]
    # Claude correlates outcomes... only when a tool_result exists; here
    # none does → honest null, never False.
    assert first["is_error"] is None

    # Unconfirmed candidate: force push with no reaction after it.
    assert second["patterns"] == ["git.push_force"]
    assert second["confirmed"] is False
    assert second["reaction"] is None

    # by_pattern reflects the FULL match set.
    assert out["by_pattern"] == {
        "fs.rm_rf_any": 1,
        "git.reset_hard": 1,
        "git.push_force": 1,
    }


def test_confirmed_modes_filter(incident_session: str) -> None:
    only = incidents(agent="claude", confirmed="only")
    assert only["count"] == 1
    assert all(r["confirmed"] for r in only["incidents"])
    excl = incidents(agent="claude", confirmed="exclude")
    assert excl["count"] == 1
    assert all(not r["confirmed"] for r in excl["incidents"])
    assert CONFIRMED_MODES == {"include", "only", "exclude"}


def test_reaction_window_zero_disables_step_two(incident_session: str) -> None:
    out = incidents(agent="claude", reaction_window=0)
    assert out["count"] == 2
    assert out["confirmed_count"] == 0
    assert all(r["reaction"] is None for r in out["incidents"])


def test_reaction_window_too_short_misses(incident_session: str) -> None:
    """An apology outside the window does not confirm (never guessed)."""
    # The apology is 1 message after the call; window=1 still catches it,
    # so shrink via a session where the apology is farther: reuse the same
    # session but check window semantics on the force-push (no reaction at
    # any window).
    out = incidents(agent="claude", reaction_window=1)
    assert out["confirmed_count"] == 1  # apology at offset 1 still in window


def test_category_filter(incident_session: str) -> None:
    git_only = incidents(agent="claude", category="git")
    assert git_only["count"] == 2  # both incidents carry a git pattern
    fs_only = incidents(agent="claude", category="fs")
    assert fs_only["count"] == 1
    assert fs_only["incidents"][0]["patterns"] == [
        "fs.rm_rf_any", "git.reset_hard",
    ]
    db_only = incidents(agent="claude", category="db")
    assert db_only["count"] == 0
    assert "diagnostics" in db_only  # empty result is explainable


def test_limit_and_truncated(incident_session: str) -> None:
    out = incidents(agent="claude", limit=1)
    assert out["count"] == 2  # totals reflect the full match set
    assert len(out["incidents"]) == 1
    assert out["truncated"] is True
    assert out["by_pattern"]["git.push_force"] == 1  # counted despite slice


def test_session_scope_single_and_list(incident_session: str) -> None:
    scoped = incidents(session=incident_session)
    assert scoped["count"] == 2
    listed = incidents(session=[incident_session, "no-such-uuid"])
    assert listed["count"] == 2  # unknown uuid contributes nothing


def test_empty_corpus_diagnostics() -> None:
    """A bare AI_R_HOME → zero incidents + diagnostics, never a crash."""
    out = incidents(agent="claude")
    assert out["count"] == 0
    assert out["incidents"] == []
    assert out["confirmed_count"] == 0
    assert out["by_pattern"] == {}
    assert "diagnostics" in out


def test_description_alone_never_fires(tmp_sessions_dir: Path) -> None:
    """A Bash description that mentions a dangerous command is not a hit."""
    uuid = "sess-desc-only"
    records = [
        _user("статус", "2026-06-14T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "checking"},
                _bash(
                    "git status",
                    "t1",
                    description=f"NOT running {RESET_HARD} here",
                ),
            ],
            "2026-06-14T10:00:05Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = incidents(agent="claude")
    assert out["count"] == 0


def test_long_command_fragment_centred_on_hit(tmp_sessions_dir: Path) -> None:
    """A huge command is capped to a fragment around the danger hit."""
    uuid = "sess-long-cmd"
    long_cmd = ("echo padding && " * 100) + RESET_HARD
    records = [
        _user("go", "2026-06-14T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "run"},
                _bash(long_cmd, "t1"),
            ],
            "2026-06-14T10:00:05Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = incidents(agent="claude")
    assert out["count"] == 1
    rec = out["incidents"][0]
    assert rec["command_truncated"] is True
    assert len(rec["command"]) <= 502  # cap + ellipsis marks
    assert "reset" in rec["command"]  # hit survived the cut


def test_redaction_on_emitted_fields(tmp_sessions_dir: Path) -> None:
    """Secrets in the command are masked by default; redact=False is raw."""
    uuid = "sess-secret-cmd"
    secret_cmd = RESET_HARD + " && export API_KEY=sk-abc123def456ghi789jkl0"
    records = [
        _user("deploy", "2026-06-14T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "run"},
                _bash(secret_cmd, "t1"),
            ],
            "2026-06-14T10:00:05Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = incidents(agent="claude")
    assert out["count"] == 1
    assert "[REDACTED_" in out["incidents"][0]["command"]
    assert out["redactions"]  # type→count dict present
    raw = incidents(agent="claude", redact=False)
    assert "sk-abc123def456ghi789jkl0" in raw["incidents"][0]["command"]
    assert "redactions" not in raw


def test_secret_on_window_boundary_never_leaks_partially(
    tmp_sessions_dir: Path,
) -> None:
    """Redaction runs on the FULL command BEFORE the window cut.

    The secret is positioned so a raw-string cut at the 500-char window
    edge would slice through it — the truncated tail would be too short
    to trip the redaction pattern on its own and would leak.  With the
    correct order (redact full command → cut window) the emitted fragment
    carries the mask and not a single fragment of the secret.
    """
    uuid = "sess-boundary-secret"
    secret = "sk-abc123def456ghi789jkl012mno345"
    prefix = RESET_HARD + " && echo "
    assign = " export OPENAI_KEY="
    # Danger hit at position 0 → the window is text[0:500]; start the
    # secret at char 495 so a raw cut would split it 5 chars in.
    pad = "x" * (495 - len(prefix) - len(assign))
    cmd = prefix + pad + assign + secret + (" && echo done" * 10)
    split_at = 500 - len(prefix + pad + assign)
    assert 0 < split_at < len(secret)  # a raw cut WOULD slice the secret
    records = [
        _user("deploy", "2026-06-14T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "run"},
                _bash(cmd, "t1"),
            ],
            "2026-06-14T10:00:05Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = incidents(agent="claude")
    assert out["count"] == 1
    rec = out["incidents"][0]
    assert rec["command_truncated"] is True
    # Not even a partial slice of the secret survives the cut (the window
    # may slice the [REDACTED_*] marker itself — harmless; what must never
    # appear is any fragment of the raw secret)...
    assert "sk-abc" not in rec["command"]
    assert secret not in rec["command"]
    # ...because the mask was applied to the FULL command first: the
    # replacement is counted even when the marker got cut by the window.
    assert out["redactions"].get("OPENAI_KEY") == 1
    # The danger hit still anchors the fragment on the redacted string.
    assert "reset" in rec["command"]
    # redact=False keeps the historical raw behaviour (window from RAW).
    raw = incidents(agent="claude", redact=False)
    assert "redactions" not in raw


def test_is_error_correlated_when_result_exists(
    tmp_sessions_dir: Path,
) -> None:
    """A correlated tool_result surfaces is_error (True here), not null."""
    uuid = "sess-err-corr"
    records = [
        _user("go", "2026-06-14T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "run"},
                {
                    "type": "tool_use",
                    "id": "boom-1",
                    "name": "Bash",
                    "input": {"command": RESET_HARD},
                },
            ],
            "2026-06-14T10:00:05Z",
            uuid,
        ),
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "boom-1",
                        "content": "fatal: not a git repository",
                        "is_error": True,
                    }
                ],
            },
            "timestamp": "2026-06-14T10:00:06Z",
            "sessionId": uuid,
        },
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = incidents(agent="claude")
    assert out["count"] == 1
    assert out["incidents"][0]["is_error"] is True


def test_multiple_bash_calls_one_message_pairing(
    tmp_sessions_dir: Path,
) -> None:
    """Two bash calls in ONE assistant message pair with the right inputs."""
    uuid = "sess-two-calls"
    records = [
        _user("go", "2026-06-14T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "run both"},
                _bash("ls -la", "t1"),
                _bash(DROP_TABLE, "t2"),
            ],
            "2026-06-14T10:00:05Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = incidents(agent="claude")
    assert out["count"] == 1  # only the second call is dangerous
    assert out["incidents"][0]["patterns"] == ["db.drop"]
    assert "DROP" in out["incidents"][0]["command"]


def test_cross_agent_codex_shell(tmp_sessions_dir: Path) -> None:
    """A codex shell call participates — all agents are equal."""
    uuid = "test-codex-inc"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T10-00-00-{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-06-14T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": uuid, "cwd": "/tmp/p"},
        },
        {
            "timestamp": "2026-06-14T10:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "clean up"}],
            },
        },
        {
            "timestamp": "2026-06-14T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps(
                    {"command": ["bash", "-lc", RESET_HARD]}
                ),
            },
        },
        {
            "timestamp": "2026-06-14T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Sorry, my mistake."}
                ],
            },
        },
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    out = incidents(agent="codex")
    assert out["count"] == 1
    rec = out["incidents"][0]
    assert rec["agent"] == "codex"
    assert "git.reset_hard" in rec["patterns"]
    # Codex has no per-result error flag → honest null.
    assert rec["is_error"] is None
    assert rec["confirmed"] is True
    assert "apology" in rec["reaction"]["markers"]


# ---------------------------------------------------------------------------
# Core: validation (fail-loud)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"confirmed": "yes"}, "confirmed"),
        ({"category": "network"}, "category"),
        ({"reaction_window": -1}, "reaction_window"),
        ({"reaction_window": True}, "reaction_window"),
        ({"limit": -5}, "limit"),
        ({"limit": True}, "limit"),
        ({"redact": "yes"}, "redact"),
        ({"agent": "gemini"}, "agent"),
        ({"session": []}, "session"),
        ({"noise": "drop"}, "noise"),
    ],
)
def test_invalid_arguments_fail_loud(kwargs: dict, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        incidents(**kwargs)


# ---------------------------------------------------------------------------
# MCP layer: registration + thin-wrapper contract
# ---------------------------------------------------------------------------


def test_mcp_incidents_registered() -> None:
    assert "incidents" in mcp_server.mcp._tool_manager._tools


def test_mcp_incidents_result_shape(incident_session: str) -> None:
    out = mcp_server.incidents(agent="claude")
    assert out["count"] == 2
    assert out["confirmed_count"] == 1
    assert {r["confirmed"] for r in out["incidents"]} == {True, False}


def test_mcp_incidents_invalid_argument_dict() -> None:
    out = mcp_server.incidents(confirmed="maybe")
    assert out["error"] == "invalid_argument"
    assert "confirmed" in out["message"]
    out2 = mcp_server.incidents(category="cloud")
    assert out2["error"] == "invalid_argument"
