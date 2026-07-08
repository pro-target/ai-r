"""The ``quotes`` preset (F5.1) — hermetic tests.

Unit layer: the verbatim matcher (:func:`find_verbatim_quote`).
Core layer: :func:`ai_r.quotes.quotes` over synthetic Claude/Codex sessions
written under the per-test ``AI_R_HOME`` (quote detection, comment extraction,
source correlation, cross-agent, filters, caps, redaction, diagnostics).
MCP layer: registration + the thin-wrapper error contract.

Everything here is hermetic; no host data is read.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r import mcp_server
from ai_r.quotes import (
    MIN_QUOTE_CHARS,
    SOURCE_KINDS,
    find_verbatim_quote,
    quotes,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

# A quotable assistant line (≥ MIN_QUOTE_CHARS), and the verbatim span a user
# selects out of it and comments on.
ASSISTANT_LINE = (
    "The quotes preset is a deterministic scan over the normalized event "
    "stream with zero LLM guessing."
)
USER_QUOTE_PART = (
    "The quotes preset is a deterministic scan over the normalized event stream"
)


def _user(text: str, ts: str, uuid: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "sessionId": uuid,
    }


def _assistant(text: str, ts: str, uuid: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": ts,
        "sessionId": uuid,
    }


def _write_claude(tmp_sessions_dir: Path, uuid: str, records: list) -> Path:
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-quo" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return jsonl


@pytest.fixture
def quote_session(tmp_sessions_dir: Path) -> str:
    """A user quotes a prior assistant line (blockquote) and comments."""
    uuid = "sess-quotes-1"
    records = [
        _user("build the preset", "2026-07-09T10:00:00Z", uuid),
        _assistant(ASSISTANT_LINE, "2026-07-09T10:00:05Z", uuid),
        _user(
            "> " + USER_QUOTE_PART + "\n\nrename source_kind to origin please",
            "2026-07-09T10:01:00Z", uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    return uuid


# ---------------------------------------------------------------------------
# Unit: verbatim matcher
# ---------------------------------------------------------------------------


def test_find_verbatim_quote_hit() -> None:
    found = find_verbatim_quote("> " + USER_QUOTE_PART + "\n\nok but rename", ASSISTANT_LINE)
    assert found is not None
    size, quote, comment = found
    assert size >= MIN_QUOTE_CHARS
    assert "deterministic scan over the normalized event stream" in quote
    assert "rename" in comment and "…" in comment


def test_find_verbatim_quote_below_min_is_none() -> None:
    # Only a short shared run → not a quote.
    assert find_verbatim_quote("The quotes preset is", ASSISTANT_LINE) is None
    # Nothing shared → None.
    assert find_verbatim_quote("add a field called foo to output", ASSISTANT_LINE) is None
    assert find_verbatim_quote("", ASSISTANT_LINE) is None


def test_find_verbatim_quote_normalizes_markdown() -> None:
    # Markdown emphasis / blockquote on the user side must not defeat the match.
    marked = "> **" + USER_QUOTE_PART + "**\n\ncomment"
    found = find_verbatim_quote(marked, ASSISTANT_LINE)
    assert found is not None and found[0] >= MIN_QUOTE_CHARS


def test_vocab_shape() -> None:
    assert SOURCE_KINDS == {"assistant"}


# ---------------------------------------------------------------------------
# Core: detection on a synthetic session
# ---------------------------------------------------------------------------


def test_quote_detected_with_source_and_comment(quote_session: str) -> None:
    out = quotes(agent="claude")
    assert out["count"] == 1
    assert out["by_source_kind"] == {"assistant": 1}
    assert out["truncated"] is False
    assert "diagnostics" not in out

    rec = out["quotes"][0]
    assert rec["agent"] == "claude"
    assert rec["session_id"] == quote_session
    assert rec["id"].startswith(quote_session + ":")          # the user turn
    assert rec["source_id"].startswith(quote_session + ":")   # the assistant turn
    assert rec["source_id"] != rec["id"]
    assert rec["source_kind"] == "assistant"
    assert rec["quote_chars"] >= MIN_QUOTE_CHARS
    assert "deterministic scan over the normalized event stream" in rec["quote"]
    # Comment = the user's turn with the quote elided.
    assert "rename source_kind to origin please" in rec["comment"]
    assert "deterministic scan over the normalized" not in rec["comment"]


def test_external_paste_not_a_quote(tmp_sessions_dir: Path) -> None:
    """A user turn quoting text NOT present earlier yields no quote."""
    uuid = "sess-external"
    records = [
        _assistant(ASSISTANT_LINE, "2026-07-09T10:00:05Z", uuid),
        _user(
            "here is a long pasted paragraph from an external doc that shares "
            "nothing with the agent output above at all whatsoever",
            "2026-07-09T10:01:00Z", uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = quotes(agent="claude")
    assert out["count"] == 0
    assert "diagnostics" in out


def test_short_overlap_below_min_skipped(tmp_sessions_dir: Path) -> None:
    uuid = "sess-short"
    records = [
        _assistant(ASSISTANT_LINE, "2026-07-09T10:00:05Z", uuid),
        _user("The quotes preset is great, ship it", "2026-07-09T10:01:00Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    assert quotes(agent="claude")["count"] == 0


def test_quote_only_matches_preceding_source(tmp_sessions_dir: Path) -> None:
    """A quote is matched to a PRECEDING assistant turn, never a later one."""
    uuid = "sess-order"
    records = [
        _user("> " + USER_QUOTE_PART + "\n\ncommenting before it was said",
              "2026-07-09T10:00:00Z", uuid),
        _assistant(ASSISTANT_LINE, "2026-07-09T10:00:05Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    # The only user turn precedes the assistant line → no preceding source.
    assert quotes(agent="claude")["count"] == 0


def test_limit_and_truncated(tmp_sessions_dir: Path) -> None:
    uuid = "sess-many"
    records = [_user("start", "2026-07-09T10:00:00Z", uuid)]
    ts = 1
    for i in range(3):
        line = f"{ASSISTANT_LINE} variant {i} padding padding padding padding."
        records.append(_assistant(line, f"2026-07-09T10:0{ts}:00Z", uuid)); ts += 1
        records.append(_user("> " + line[:80] + f"\n\ncomment {i}",
                             f"2026-07-09T10:0{ts}:00Z", uuid)); ts += 1
    _write_claude(tmp_sessions_dir, uuid, records)
    full = quotes(agent="claude")
    assert full["count"] == 3
    lim = quotes(agent="claude", limit=1)
    assert lim["count"] == 3 and len(lim["quotes"]) == 1 and lim["truncated"] is True


def test_session_scope_single_and_list(quote_session: str) -> None:
    assert quotes(session=quote_session)["count"] == 1
    listed = quotes(session=[quote_session, "no-such-uuid"])
    assert listed["count"] == 1


def test_redaction_on_quote_and_comment(tmp_sessions_dir: Path) -> None:
    uuid = "sess-secret-quote"
    secret = "sk-abc123def456ghi789jkl0mno"
    line = (
        "The deployment uses the api key " + secret
        + " to authenticate against the internal service endpoint now."
    )
    records = [
        _assistant(line, "2026-07-09T10:00:05Z", uuid),
        _user("> " + line[:90] + "\n\nrotate that key", "2026-07-09T10:01:00Z", uuid),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = quotes(agent="claude")
    assert out["count"] == 1
    assert secret not in out["quotes"][0]["quote"]
    assert "[REDACTED_" in out["quotes"][0]["quote"]
    assert out["redactions"]
    raw = quotes(agent="claude", redact=False)
    assert secret in raw["quotes"][0]["quote"]
    assert "redactions" not in raw


def test_cross_agent_codex(tmp_sessions_dir: Path) -> None:
    """A codex session participates — all agents are equal (cross-client)."""
    uuid = "test-codex-quo"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "07" / "09"
        / f"rollout-2026-07-09T10-00-00-{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": "2026-07-09T10:00:00Z", "type": "session_meta",
         "payload": {"id": uuid, "cwd": "/tmp/p"}},
        {"timestamp": "2026-07-09T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": ASSISTANT_LINE}]}},
        {"timestamp": "2026-07-09T10:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "> " + USER_QUOTE_PART + "\n\nlgtm ship"}]}},
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    out = quotes(agent="codex")
    assert out["count"] == 1
    assert out["quotes"][0]["agent"] == "codex"
    assert out["quotes"][0]["quote_chars"] >= MIN_QUOTE_CHARS


def test_empty_corpus_diagnostics() -> None:
    out = quotes(agent="claude")
    assert out["count"] == 0
    assert out["quotes"] == []
    assert out["by_source_kind"] == {}
    assert "diagnostics" in out


# ---------------------------------------------------------------------------
# Core: validation (fail-loud)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"source_kind": "tool"}, "source_kind"),
        ({"limit": -1}, "limit"),
        ({"limit": True}, "limit"),
        ({"redact": "yes"}, "redact"),
        ({"agent": "gemini"}, "agent"),
        ({"session": []}, "session"),
        ({"noise": "drop"}, "noise"),
    ],
)
def test_invalid_arguments_fail_loud(kwargs: dict, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        quotes(**kwargs)


# ---------------------------------------------------------------------------
# MCP layer
# ---------------------------------------------------------------------------


def test_mcp_quotes_registered() -> None:
    assert "quotes" in mcp_server.mcp._tool_manager._tools


def test_mcp_quotes_result_shape(quote_session: str) -> None:
    out = mcp_server.quotes(agent="claude")
    assert out["count"] == 1
    assert out["quotes"][0]["source_kind"] == "assistant"


def test_mcp_quotes_invalid_argument_dict() -> None:
    out = mcp_server.quotes(source_kind="tool")
    assert out["error"] == "invalid_argument"
    assert "source_kind" in out["message"]
