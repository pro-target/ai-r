"""Claude ``<tool_use_error>`` / ``toolUseResult:"Error:"`` outcome derive.

Real Claude Code transcripts record some failed tool calls WITHOUT the
per-block ``is_error`` flag: the failure shows only as a ``tool_result``
whose content starts with ``<tool_use_error>``, and/or a record-level
``toolUseResult`` string starting with ``"Error:"``.  The parser must
DERIVE ``is_error=True`` from either signal (an explicit flag still wins),
so ``find_tool_calls(is_error=True)`` / ``query`` refs / ``read_session``
rendering / ``session_outcome`` never go blind on such sessions while
``is_error_reliable=True`` keeps claiming reliability.

Hermetic: the autouse ``_isolate_ai_r_home`` fixture points parsers at a
per-test temp home; each test additionally monkeypatches
``_resolve_base_dir`` at the fixture's projects tree.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r.events import iter_events, query
from ai_r.find_tool_calls import find_tool_calls
from ai_r.outcome import session_outcome
from ai_r.parsers import claude
from ai_r.parsers.models import AgentName


# ---------------------------------------------------------------------------
# Seed: a Claude session whose failed calls use the two error FORMATS with
# NO explicit ``is_error`` flag, plus two priority-guard calls.
# ---------------------------------------------------------------------------


def _write_session(tmp_sessions_dir: Path, uuid: str) -> None:
    """Write a Claude JSONL exercising every derive + priority branch.

    Calls (each an assistant ``tool_use`` correlated by ``tool_use_id`` to a
    following user ``tool_result``):

    * ``tu-tue-str`` — result content is a STRING starting with
      ``<tool_use_error>``; block carries NO ``is_error`` → derive True.
    * ``tu-tue-list`` — result content is a LIST whose first ``text`` block
      starts with ``<tool_use_error>``; NO ``is_error`` → derive True.
    * ``tu-tur`` — result content is an ordinary string, but the record's
      top-level ``toolUseResult`` is ``"Error: …"``; NO ``is_error`` →
      derive True.
    * ``tu-ok`` — a plain successful result, NO ``is_error`` → False.
    * ``tu-explicit-false`` — content starts with ``<tool_use_error>`` BUT
      the block sets ``is_error: False`` → explicit flag wins → False.
    * ``tu-explicit-true`` — a benign result string BUT the block sets
      ``is_error: True`` → explicit flag wins → True.
    """

    def _call(tuid: str, command: str, ts: str) -> dict:
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tuid, "name": "Bash",
                     "input": {"command": command}},
                ],
            },
            "timestamp": ts,
            "sessionId": uuid,
        }

    def _result(
        tuid: str, content, ts: str, *, is_error=None, tool_use_result=None
    ) -> dict:
        block: dict = {
            "type": "tool_result", "tool_use_id": tuid, "content": content,
        }
        if is_error is not None:
            block["is_error"] = is_error
        record: dict = {
            "type": "user",
            "message": {"role": "user", "content": [block]},
            "timestamp": ts,
            "sessionId": uuid,
        }
        if tool_use_result is not None:
            record["toolUseResult"] = tool_use_result
        return record

    tue = "<tool_use_error>Error: No such tool available: Glob.</tool_use_error>"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "run the commands"},
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": uuid,
        },
        # (a) string-content <tool_use_error>, no flag
        _call("tu-tue-str", "glob-a", "2026-06-14T10:00:01Z"),
        _result("tu-tue-str", tue, "2026-06-14T10:00:02Z"),
        # (a') list-content <tool_use_error>, no flag
        _call("tu-tue-list", "glob-b", "2026-06-14T10:00:03Z"),
        _result(
            "tu-tue-list",
            [{"type": "text", "text": tue}],
            "2026-06-14T10:00:04Z",
        ),
        # (b) toolUseResult: "Error: ...", no flag, plain content
        _call("tu-tur", "big-output", "2026-06-14T10:00:05Z"),
        _result(
            "tu-tur",
            "Error: result (56,376 characters) exceeds maximum allowed tokens.",
            "2026-06-14T10:00:06Z",
            tool_use_result=(
                "Error: result (56,376 characters) exceeds maximum "
                "allowed tokens. Output saved to /tmp/x."
            ),
        ),
        # success, no flag
        _call("tu-ok", "true", "2026-06-14T10:00:07Z"),
        _result("tu-ok", "ok-stdout", "2026-06-14T10:00:08Z"),
        # explicit False beats <tool_use_error> content
        _call("tu-explicit-false", "glob-c", "2026-06-14T10:00:09Z"),
        _result("tu-explicit-false", tue, "2026-06-14T10:00:10Z",
                is_error=False),
        # explicit True beats a benign string
        _call("tu-explicit-true", "false", "2026-06-14T10:00:11Z"),
        _result("tu-explicit-true", "looks fine", "2026-06-14T10:00:12Z",
                is_error=True),
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-tue" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def claude_tue_session(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    uuid = "tue-derive-1"
    _write_session(tmp_sessions_dir, uuid)
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir",
        lambda bd=None: tmp_sessions_dir / ".claude" / "projects",
    )
    return uuid


# ---------------------------------------------------------------------------
# 1. Parser layer: tool_result[].is_error carries the derived value.
# ---------------------------------------------------------------------------


def test_parser_derives_is_error_from_both_formats(
    claude_tue_session: str,
) -> None:
    msgs = claude.read_messages(claude_tue_session)
    by_id = {}
    for m in msgs:
        for tr in m.tool_result:
            if tr.get("tool_use_id"):
                by_id[tr["tool_use_id"]] = tr["is_error"]
    assert by_id["tu-tue-str"] is True       # <tool_use_error> string
    assert by_id["tu-tue-list"] is True      # <tool_use_error> in list block
    assert by_id["tu-tur"] is True           # toolUseResult "Error:"
    assert by_id["tu-ok"] is False           # plain success


def test_parser_explicit_flag_wins(claude_tue_session: str) -> None:
    """An explicit ``is_error`` is never overridden by a format signal."""
    msgs = claude.read_messages(claude_tue_session)
    by_id = {
        tr["tool_use_id"]: tr["is_error"]
        for m in msgs for tr in m.tool_result if tr.get("tool_use_id")
    }
    # <tool_use_error> content but explicit is_error=False → stays False.
    assert by_id["tu-explicit-false"] is False
    # benign content but explicit is_error=True → stays True.
    assert by_id["tu-explicit-true"] is True


# ---------------------------------------------------------------------------
# 2. find_tool_calls(is_error=True) sees the derived failures.
# ---------------------------------------------------------------------------


def test_find_tool_calls_is_error_true_includes_derived(
    claude_tue_session: str,
) -> None:
    failing = find_tool_calls(agent="claude", is_error=True)
    got = {json.dumps(r["input"], sort_keys=True) for r in failing["records"]}
    # The three no-flag failures + the explicit-True one; NOT the successes
    # nor the explicit-False (which content-looks like an error).
    assert json.dumps({"command": "glob-a"}, sort_keys=True) in got
    assert json.dumps({"command": "glob-b"}, sort_keys=True) in got
    assert json.dumps({"command": "big-output"}, sort_keys=True) in got
    assert json.dumps({"command": "false"}, sort_keys=True) in got
    assert json.dumps({"command": "true"}, sort_keys=True) not in got
    assert json.dumps({"command": "glob-c"}, sort_keys=True) not in got
    assert failing["count"] == 4
    assert all(r["is_error"] is True for r in failing["records"])
    # Reliability claim stays honest for Claude (per-record flag).
    assert all(r["is_error_reliable"] is True for r in failing["records"])


def test_find_tool_calls_is_error_false_excludes_derived(
    claude_tue_session: str,
) -> None:
    ok = find_tool_calls(agent="claude", is_error=False)
    got = {json.dumps(r["input"], sort_keys=True) for r in ok["records"]}
    assert json.dumps({"command": "true"}, sort_keys=True) in got
    assert json.dumps({"command": "glob-c"}, sort_keys=True) in got  # explicit False
    assert json.dumps({"command": "glob-a"}, sort_keys=True) not in got
    assert ok["count"] == 2


# ---------------------------------------------------------------------------
# 3. query: the tool_call event carries the derived is_error ref.
# ---------------------------------------------------------------------------


def test_query_refs_carry_derived_is_error(claude_tue_session: str) -> None:
    events = list(iter_events("claude", session=claude_tue_session))
    # Multiset of is_error refs across all tool_call events.
    flags = [
        r["is_error"]
        for e in events if e.type.startswith("tool_call(")
        for r in e.refs if "is_error" in r
    ]
    assert sorted(flags) == [False, False, True, True, True, True]

    # And via the public query verb (same event stream, refs preserved).
    q = query(type="tool_call", session=claude_tue_session, agent="claude")
    q_flags = [
        ref["is_error"]
        for ev in q
        for ref in (ev.get("refs") or ())
        if "is_error" in ref
    ]
    assert sorted(q_flags) == [False, False, True, True, True, True]


# ---------------------------------------------------------------------------
# 4. read_session (MCP projection) renders "[tool_result ERROR: ...]".
# ---------------------------------------------------------------------------


def test_read_session_renders_error_marker(claude_tue_session: str) -> None:
    from ai_r import mcp_server

    payload = mcp_server.read_session(
        claude_tue_session, agent="claude", redact=False
    )
    bodies = "\n".join(
        m.get("content", "") for m in payload["messages"]
    )
    # Every derived failure surfaces as an ERROR marker in the projection.
    assert "[tool_result ERROR:" in bodies
    # The <tool_use_error> failures and the toolUseResult "Error:" one all
    # render as errors — count the ERROR markers (3 no-flag + 1 explicit-True).
    assert bodies.count("[tool_result ERROR") == 4


# ---------------------------------------------------------------------------
# 5. session_outcome / incidents: error_rate counts the derived failures.
# ---------------------------------------------------------------------------


def test_session_outcome_error_rate_counts_derived(
    claude_tue_session: str,
) -> None:
    msgs = claude.read_messages(claude_tue_session)
    out = session_outcome(msgs, AgentName.CLAUDE)
    # 6 tool_results total; 4 errors (3 derived-no-flag + 1 explicit-True).
    assert out["tool_errors"] == 4
    assert out["error_rate"] == round(4 / 6, 4)
