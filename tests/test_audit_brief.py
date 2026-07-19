"""Hermetic tests for the ``audit_brief`` preset (stage 4).

Everything runs on synthetic sessions under the autouse ``AI_R_HOME``
isolation — no host data (hermeticity rule: host data absent → these tests
never touch it in the first place).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.audit_brief import audit_brief


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


LONG_USER_TEXT = "Please refactor the auth module carefully. " * 20


@pytest.fixture
def rich_claude_session(tmp_sessions_dir: Path) -> str:
    """A Claude session with user turns, an edit, an error and prose."""
    session_id = "audit-brief-rich-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-a"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": LONG_USER_TEXT},
                "timestamp": "2026-06-14T10:00:00Z",
                "sessionId": session_id,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Editing the auth module."},
                        {
                            "type": "tool_use",
                            "id": "tu-edit-1",
                            "name": "Edit",
                            "input": {
                                "file_path": "/repo/src/auth.py",
                                "old_string": "a",
                                "new_string": "b",
                            },
                        },
                    ],
                },
                "timestamp": "2026-06-14T10:00:05Z",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-edit-1",
                            "is_error": True,
                            "content": "old_string not found",
                        }
                    ],
                },
                "timestamp": "2026-06-14T10:00:06Z",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Now run the tests and report back, verbatim.",
                },
                "timestamp": "2026-06-14T10:01:00Z",
                "sessionId": session_id,
            },
        ],
    )
    return session_id


def test_shape_and_footprints(rich_claude_session: str) -> None:
    brief = audit_brief(rich_claude_session)

    # Deterministic section skeleton.
    for key in (
        "session", "user_turns", "plans", "tools", "files",
        "tokens", "component_tokens", "budget",
    ):
        assert key in brief, f"missing section {key!r}"
    assert brief["session"]["uuid"] == rich_claude_session
    assert brief["session"]["agent"] == "claude"

    # (a) user turns — verbatim, chronological, no cut marker.
    texts = [t["text"] for t in brief["user_turns"]]
    assert texts == [
        LONG_USER_TEXT.strip(),
        "Now run the tests and report back, verbatim.",
    ] or texts == [
        LONG_USER_TEXT,
        "Now run the tests and report back, verbatim.",
    ]
    assert brief["user_turns_count"] == 2

    # (c) tool footprint — counts by kind + the notable error, not a dump.
    assert brief["tools"]["total"] == 1
    assert brief["tools"]["by_kind"] == {"edit": 1}
    assert brief["tools"]["errors_count"] == 1
    (err,) = brief["tools"]["errors"]
    assert err["tool"] == "Edit"
    assert err["tool_kind"] == "edit"
    assert "input" not in err  # summary, never a dump

    # (d) file footprint from the existing file refs.
    assert brief["files"]["count"] == 1
    assert brief["files"]["edited"] == [
        {"file": "/repo/src/auth.py", "edits": 1}
    ]

    # (e) token breakdown — honest source label.
    assert brief["tokens"]["source"] in ("exact", "estimate", None)
    assert brief["component_tokens"] is None or (
        brief["component_tokens"]["source"] == "estimate"
    )

    # Budget: default fits, nothing dropped.
    assert brief["budget"]["dropped"] == []
    assert brief["budget"]["over_budget"] is False
    assert brief["budget"]["used_chars"] <= brief["budget"]["budget_chars"]


def test_budget_ladder_order_and_user_turns_never_truncated(
    rich_claude_session: str,
) -> None:
    full = audit_brief(rich_claude_session, budget_chars=0)
    full_turn_texts = [t["text"] for t in full["user_turns"]]
    assert full["budget"]["budget_chars"] == 0  # unlimited: ladder never ran
    assert full["budget"]["dropped"] == []

    tiny = audit_brief(rich_claude_session, budget_chars=300)
    # Fixed ladder ORDER: tool details first, then the file list.  (This
    # session has no plan bodies, so `plan_bodies` honestly never appears.)
    assert tiny["budget"]["dropped"] == ["tool_error_details", "file_details"]
    # Counts/references survive the drop.
    assert tiny["tools"]["errors"] is None
    assert tiny["tools"]["errors_dropped"] is True
    assert tiny["tools"]["errors_count"] == 1
    assert tiny["files"]["edited"] is None
    assert tiny["files"]["count"] == 1
    # User turns: byte-identical to the unbudgeted run — NEVER truncated.
    assert [t["text"] for t in tiny["user_turns"]] == full_turn_texts
    # Honest over-budget marker + the full-projection reference.
    assert tiny["budget"]["over_budget"] is True
    assert "NEVER truncated" in tiny["budget"]["note"]
    assert rich_claude_session in tiny["budget"]["note"]


def test_budget_honest_on_final_serialized_json(
    rich_claude_session: str,
) -> None:
    """The pinned property: final serialized length <= budget OR over_budget.

    Regression for the pre-insertion measurement bug: the ladder used to
    measure the response BEFORE the ``budget``/``redactions`` blocks were
    attached, so a near-limit digest could exceed ``budget_chars`` while
    claiming ``over_budget: false``.
    """
    full = audit_brief(rich_claude_session, budget_chars=0)
    base = len(json.dumps(full, ensure_ascii=False, default=str))
    # Probe every regime: comfortably under, the exact edge, just over (the
    # old bug zone), mid-ladder, hopeless-tiny.
    for budget in (base + 50, base, base - 1, base - 30, 900, 300):
        brief = audit_brief(rich_claude_session, budget_chars=budget)
        payload = json.dumps(brief, ensure_ascii=False, default=str)
        assert (
            len(payload) <= budget or brief["budget"]["over_budget"] is True
        ), f"budget={budget}: {len(payload)} chars with over_budget=false"
        # used_chars IS the final payload length — never a pre-insertion guess.
        assert brief["budget"]["used_chars"] == len(payload)


def test_mid_budget_drops_only_what_it_must(rich_claude_session: str) -> None:
    full = audit_brief(rich_claude_session, budget_chars=0)
    # A budget just below the full size forces the FIRST rung only.
    just_under = full["budget"]["used_chars"] - 1
    mid = audit_brief(rich_claude_session, budget_chars=just_under)
    assert mid["budget"]["dropped"][0] == "tool_error_details"
    # The ladder stops as soon as the digest fits — never drops more.
    if not mid["budget"]["over_budget"]:
        assert mid["budget"]["used_chars"] <= just_under


def test_invalid_arguments_fail_loud(rich_claude_session: str) -> None:
    with pytest.raises(ValueError):
        audit_brief("")
    with pytest.raises(ValueError):
        audit_brief(rich_claude_session, budget_chars=-1)
    with pytest.raises(ValueError):
        audit_brief(rich_claude_session, budget_chars=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        audit_brief(rich_claude_session, redact="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        audit_brief(rich_claude_session, agent="not-an-agent")


def test_unknown_session_not_found(tmp_sessions_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        audit_brief("no-such-session-uuid")


@pytest.fixture
def hex_sessions(tmp_sessions_dir: Path) -> tuple[str, str]:
    """Two Claude sessions sharing a 6-hex id head (prefix resolution)."""
    ids = (
        "c0ffee01-aaaa-4aaa-8aaa-000000000001",
        "c0ffee02-bbbb-4bbb-8bbb-000000000002",
    )
    for i, session_id in enumerate(ids):
        _write_jsonl(
            tmp_sessions_dir / ".claude" / "projects" / "proj-hex"
            / f"{session_id}.jsonl",
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": f"hex turn {i}"},
                    "timestamp": f"2026-06-15T10:0{i}:00Z",
                    "sessionId": session_id,
                }
            ],
        )
    return ids


def test_prefix_unique_resolves_and_echoes_full_uuid(
    hex_sessions: tuple[str, str],
) -> None:
    brief = audit_brief("c0ffee01")  # the 8-hex head — locate's id matching
    assert brief["session"]["uuid"] == hex_sessions[0]  # full id echoed
    assert [t["text"] for t in brief["user_turns"]] == ["hex turn 0"]


def test_prefix_ambiguous_fails_loud_with_candidates(
    hex_sessions: tuple[str, str],
) -> None:
    with pytest.raises(ValueError) as excinfo:
        audit_brief("c0ffee")
    message = str(excinfo.value)
    assert "ambiguous" in message
    for session_id in hex_sessions:
        assert session_id in message


def test_prefix_none_not_found(hex_sessions: tuple[str, str]) -> None:
    with pytest.raises(FileNotFoundError):
        audit_brief("deadbeef")


def test_mcp_prefix_error_contract(hex_sessions: tuple[str, str]) -> None:
    from ai_r.mcp_server import audit_brief as mcp_audit_brief

    ambiguous = mcp_audit_brief("c0ffee")
    assert ambiguous["error"] == "invalid_argument"
    assert "ambiguous" in ambiguous["message"]
    missing = mcp_audit_brief("deadbeef")
    assert missing["error"] == "not_found"


def test_mcp_wrapper_error_contract(tmp_sessions_dir: Path) -> None:
    from ai_r.mcp_server import audit_brief as mcp_audit_brief

    missing = mcp_audit_brief("no-such-session-uuid")
    assert missing["error"] == "not_found"
    bad = mcp_audit_brief("x", budget_chars=-5)
    assert bad["error"] == "invalid_argument"


def test_cli_markdown_and_json(
    rich_claude_session: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_r.cli.main import main

    assert main(["audit-brief", rich_claude_session]) == 0
    out = capsys.readouterr().out
    assert f"# Audit brief — {rich_claude_session} (claude)" in out
    assert "## User turns (2, verbatim)" in out
    assert "Now run the tests and report back, verbatim." in out

    assert main(["audit-brief", rich_claude_session, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["uuid"] == rich_claude_session

    assert main(["audit-brief", "no-such-uuid"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("ai-r: ")
