"""Secret redaction tests (F2.1) — hermetic.

Covers the three layers of the feature:

* the pattern table itself (``ai_r.redact``): every ``[REDACTED_<TYPE>]``
  type fires on a canonical sample and the documented false-positive
  guards hold (uuids / bare hashes / ``sk-learn`` prose never trip);
* the emission surfaces: MCP verbs mask secrets by default, carry a
  per-type ``redactions`` counter, and return raw content with
  ``redact=False`` — while matching/filtering always runs on the RAW
  stored text;
* the empty-result diagnostics link (F1.1×F2.1): a filter value that is a
  ``[REDACTED_*]`` placeholder or looks like a secret earns a hint that
  redaction is output-only (retry with ``redact=false``).

All corpus-touching tests run against the fake ``AI_R_HOME`` tree
(autouse ``_isolate_ai_r_home``) and scope to ``agent="claude"`` — the
OpenCode parser can leak the real host DB (documented host-leak).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r import mcp_server
from ai_r.diagnostics import empty_result_diagnostics
from ai_r.events import query as query_core
from ai_r.redact import (
    REDACTION_MARKER_PREFIX,
    REDACTION_TYPES,
    merge_redaction_counts,
    redact_text,
    redact_value,
    secret_like_types,
)

# Canonical secret samples — one per redaction TYPE.  Assembled from
# harmless fragments; none is a real credential.
OPENAI_KEY = "sk-abc123def456ghi789jkl012mno"
ANTHROPIC_KEY = "sk-ant-api03-abc123def456ghi789"
GITHUB_TOKEN = "ghp_" + "A1b2C3d4" * 5  # 40 chars after the prefix
GITLAB_TOKEN = "glpat-abc123def456ghi789jk"
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNO7"
AWS_SECRET_LINE = 'aws_secret_access_key = "' + "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY12" + '"'
SLACK_TOKEN = "xoxb-1234567890-abcdefghij"
# FAKE Stripe secret + restricted keys (structurally shaped, never real).
# Prefix split off the body (like GOOGLE_API_KEY below) so the literal is not a
# contiguous ``sk_live_<24+>`` span — GitHub push-protection flags that shape as
# a real Stripe key; the concatenated runtime value is what the redaction needs.
STRIPE_LIVE = "sk_live_" + "51ABCdefGHIjklMNOpqrSTUvwx0000000000"
STRIPE_TEST = "sk_test_" + "51ABCdefGHIjklMNOpqrSTUvwx0000000000"
STRIPE_RESTRICTED = "rk_live_" + "51ABCdefGHIjklMNOpqrSTUvwx0000000000"
# FAKE JWT: three base64url segments joined by dots (header ``{"alg":"HS256"}``).
JWT_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
# FAKE Google API key: ``AIza`` + 35 url-safe chars = 39 total.  Split so the
# literal is not a scannable ``API_KEY = "<long>"`` assignment (secret scanners
# flag that shape); the runtime value is the full key the redaction test needs.
GOOGLE_API_KEY = "AIza" + "SyD_FAKEfakeFAKEfake0123456789-abcd"
BEARER_LINE = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload123.sig456"
URL_CREDS = "postgres://alice:s3cretpass@db.example.com:5432/app"
GENERIC_LINE = "PASSWORD=hunter2x9extra"
PRIVATE_KEY_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA7bq+abcdef\n"
    "-----END RSA PRIVATE KEY-----"
)


# ---------------------------------------------------------------------------
# Pattern table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "expected_type"),
    [
        (OPENAI_KEY, "OPENAI_KEY"),
        (ANTHROPIC_KEY, "ANTHROPIC_KEY"),
        (GITHUB_TOKEN, "GITHUB_TOKEN"),
        (GITLAB_TOKEN, "GITLAB_TOKEN"),
        (AWS_KEY, "AWS_KEY"),
        (AWS_SECRET_LINE, "AWS_SECRET"),
        (SLACK_TOKEN, "SLACK_TOKEN"),
        (STRIPE_LIVE, "STRIPE_KEY"),
        (STRIPE_TEST, "STRIPE_KEY"),
        (STRIPE_RESTRICTED, "STRIPE_KEY"),
        (JWT_TOKEN, "JWT"),
        (GOOGLE_API_KEY, "GOOGLE_API_KEY"),
        (BEARER_LINE, "BEARER_TOKEN"),
        (URL_CREDS, "URL_CREDENTIALS"),
        (GENERIC_LINE, "GENERIC_SECRET"),
        (PRIVATE_KEY_BLOCK, "PRIVATE_KEY"),
    ],
)
def test_each_type_fires(sample: str, expected_type: str) -> None:
    redacted, counts = redact_text(f"before {sample} after")
    assert counts == {expected_type: 1}
    assert f"[REDACTED_{expected_type}]" in redacted
    assert expected_type in REDACTION_TYPES


def test_anthropic_key_wins_over_openai() -> None:
    """``sk-ant-`` sits above the generic ``sk-`` alternative."""
    _, counts = redact_text(ANTHROPIC_KEY)
    assert counts == {"ANTHROPIC_KEY": 1}


def test_value_only_replacement_keeps_context() -> None:
    """Bearer prefix / URL scheme+host / key name survive the masking."""
    redacted, _ = redact_text(BEARER_LINE)
    assert redacted == "Authorization: Bearer [REDACTED_BEARER_TOKEN]"
    redacted, _ = redact_text(URL_CREDS)
    assert redacted == "postgres://[REDACTED_URL_CREDENTIALS]@db.example.com:5432/app"
    redacted, _ = redact_text(GENERIC_LINE)
    assert redacted == "PASSWORD=[REDACTED_GENERIC_SECRET]"


@pytest.mark.parametrize(
    "benign",
    [
        # uuids and hashes are identifiers, not secrets.
        "session 6c18b957-8f6b-4d3a-9c8e-482d18179298 done",
        "commit b0310ae4f2c1d8e9b7a6c5d4e3f2a1b0c9d8e7f6",
        # ``sk-`` prose without a digit (the sklearn guard).
        "use the sk-learn-pipeline-tuning-guide approach",
        # Bearer as a word, not a token.
        "Bearer authentication is described below",
        # generic key assigned a digit-less word.
        "token = tokenize",
        # generic key with a too-short value.
        "password: abc1",
        "plain text with no secrets at all",
    ],
)
def test_false_positive_guards(benign: str) -> None:
    redacted, counts = redact_text(benign)
    assert counts == {}
    assert redacted == benign


def test_counter_counts_every_hit() -> None:
    text = f"{OPENAI_KEY} then {OPENAI_KEY} then {GITHUB_TOKEN}"
    _, counts = redact_text(text)
    assert counts == {"OPENAI_KEY": 2, "GITHUB_TOKEN": 1}


def test_new_vendor_patterns_mask_value_and_context() -> None:
    """JWT / Google / Stripe keys are masked whole, leaving surrounding prose."""
    redacted, counts = redact_text(
        f"jwt {JWT_TOKEN} google {GOOGLE_API_KEY} stripe {STRIPE_LIVE} done"
    )
    assert counts == {"JWT": 1, "GOOGLE_API_KEY": 1, "STRIPE_KEY": 1}
    assert JWT_TOKEN not in redacted
    assert GOOGLE_API_KEY not in redacted
    assert STRIPE_LIVE not in redacted
    # Surrounding words survive (whole-token replacement, not the line).
    assert redacted.startswith("jwt [REDACTED_JWT] google [REDACTED_GOOGLE_API_KEY]")
    assert redacted.endswith("stripe [REDACTED_STRIPE_KEY] done")


@pytest.mark.parametrize(
    "benign",
    [
        # ``sk_`` without the ``live``/``test`` infix is not a Stripe key.
        "sk_foo_1234567890 is not a stripe secret",
        # ``AIza`` prefix but wrong length (too short) — not a Google key.
        "AIzaShort123 should stay",
        # A two-segment dotted base64 is not a JWT (needs three segments).
        "eyJhbGciOiJIUzI1NiJ9.onlyonesegment stays",
        # Bare ``eyJ`` word without the dotted structure.
        "eyJustAWordHere with no dots",
    ],
)
def test_new_patterns_false_positive_guards(benign: str) -> None:
    redacted, counts = redact_text(benign)
    assert counts == {}
    assert redacted == benign


def test_new_patterns_no_catastrophic_backtracking() -> None:
    """Pathological near-miss inputs stay linear (no regex blow-up)."""
    import time

    # Long ``eyJ``-prefixed base64 run with a single trailing dot (never a
    # full 3-segment JWT) and a long ``AIza``-prefixed run — both would trip
    # a nested-quantifier pattern into exponential backtracking.
    payloads = [
        "eyJ" + "A" * 5000 + ".",
        "AIza" + "B" * 5000,
        "sk_live_" + "C" * 5000 + " ",
    ]
    start = time.monotonic()
    for p in payloads:
        redact_text(p * 3)
    assert time.monotonic() - start < 1.0


def test_non_string_passthrough() -> None:
    assert redact_text(None) == (None, {})
    assert redact_text(42) == (42, {})
    assert redact_text("") == ("", {})


def test_redact_value_recurses_containers() -> None:
    payload = {
        "a": OPENAI_KEY,
        "b": [GENERIC_LINE, {"c": BEARER_LINE}],
        "n": 7,
    }
    redacted, counts = redact_value(payload)
    assert counts == {
        "OPENAI_KEY": 1,
        "GENERIC_SECRET": 1,
        "BEARER_TOKEN": 1,
    }
    assert redacted["a"] == "[REDACTED_OPENAI_KEY]"
    assert redacted["b"][1]["c"].endswith("[REDACTED_BEARER_TOKEN]")
    assert redacted["n"] == 7
    # The original payload is not mutated.
    assert payload["a"] == OPENAI_KEY


def test_merge_redaction_counts() -> None:
    dst = {"OPENAI_KEY": 1}
    merge_redaction_counts(dst, {"OPENAI_KEY": 2, "AWS_KEY": 1})
    assert dst == {"OPENAI_KEY": 3, "AWS_KEY": 1}


def test_secret_like_types() -> None:
    assert secret_like_types(OPENAI_KEY) == ["OPENAI_KEY"]
    assert secret_like_types("hello world") == []


# ---------------------------------------------------------------------------
# Emission surfaces (fake Claude corpus)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_claude_secret_session(tmp_sessions_dir: Path) -> str:
    """A Claude session whose transcript contains pasted secrets."""
    session_id = "claude-secret-1"
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-s" / f"{session_id}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": f"set the api key to {OPENAI_KEY} please",
            },
            "timestamp": "2026-06-14T10:00:00Z",
            "sessionId": session_id,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"Writing {GENERIC_LINE} to .env"},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": f"echo {GENERIC_LINE} >> .env"},
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
                    {"type": "tool_result", "content": f"wrote {GENERIC_LINE}"}
                ],
            },
            "timestamp": "2026-06-14T10:00:10Z",
            "sessionId": session_id,
        },
    ]
    with jsonl.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return session_id


def test_query_redacts_by_default(fake_claude_secret_session: str) -> None:
    out = mcp_server.query(
        agent="claude", session=fake_claude_secret_session, type="user_turn"
    )
    texts = [e["text"] for e in out["events"]]
    assert any("[REDACTED_OPENAI_KEY]" in t for t in texts)
    assert all(OPENAI_KEY not in t for t in texts)
    assert out["redactions"]["OPENAI_KEY"] >= 1


def test_query_redact_false_returns_raw(fake_claude_secret_session: str) -> None:
    out = mcp_server.query(
        agent="claude",
        session=fake_claude_secret_session,
        type="user_turn",
        redact=False,
    )
    assert any(OPENAI_KEY in e["text"] for e in out["events"])
    assert "redactions" not in out


def test_query_text_filter_matches_raw(fake_claude_secret_session: str) -> None:
    """Redaction is emission-time only: the raw secret is still findable."""
    out = mcp_server.query(
        agent="claude", session=fake_claude_secret_session, text=OPENAI_KEY
    )
    assert out["count"] >= 1
    assert all(OPENAI_KEY not in e["text"] for e in out["events"])


def test_query_rejects_non_bool_redact() -> None:
    out = mcp_server.query(agent="claude", redact="yes")  # type: ignore[arg-type]
    assert out["error"] == "invalid_argument"
    with pytest.raises(ValueError):
        query_core(agent="claude", redact="yes")  # type: ignore[arg-type]


def test_read_session_redacts_and_counts(fake_claude_secret_session: str) -> None:
    out = mcp_server.read_session(fake_claude_secret_session, agent="claude")
    blob = json.dumps(out, ensure_ascii=False)
    assert OPENAI_KEY not in blob
    assert "hunter2x9extra" not in blob
    assert "[REDACTED_OPENAI_KEY]" in blob
    assert out["redactions"]["OPENAI_KEY"] >= 1
    assert out["redactions"]["GENERIC_SECRET"] >= 1


def test_read_session_redact_false(fake_claude_secret_session: str) -> None:
    out = mcp_server.read_session(
        fake_claude_secret_session, agent="claude", redact=False
    )
    blob = json.dumps(out, ensure_ascii=False)
    assert OPENAI_KEY in blob
    assert "redactions" not in out


def test_search_sessions_matches_raw_masks_snippet(
    fake_claude_secret_session: str,
) -> None:
    out = mcp_server.search_sessions(OPENAI_KEY, agent="claude")
    assert out["count"] == 1
    blob = json.dumps(out["results"], ensure_ascii=False)
    assert OPENAI_KEY not in blob
    assert REDACTION_MARKER_PREFIX in blob
    assert out["redactions"]["OPENAI_KEY"] >= 1
    raw = mcp_server.search_sessions(OPENAI_KEY, agent="claude", redact=False)
    assert OPENAI_KEY in json.dumps(raw["results"], ensure_ascii=False)


def test_list_sessions_redacts_title(fake_claude_secret_session: str) -> None:
    out = mcp_server.list_sessions(agent="claude")
    titles = [s.get("title") or "" for s in out["sessions"]]
    assert any("[REDACTED_OPENAI_KEY]" in t for t in titles)
    assert all(OPENAI_KEY not in t for t in titles)
    assert out["redactions"]["OPENAI_KEY"] >= 1


def test_find_tool_calls_redacts_input(fake_claude_secret_session: str) -> None:
    out = mcp_server.find_tool_calls(agent="claude", tool_name="Bash")
    blob = json.dumps(out["records"], ensure_ascii=False)
    assert "hunter2x9extra" not in blob
    assert "[REDACTED_GENERIC_SECRET]" in blob
    assert out["redactions"]["GENERIC_SECRET"] >= 1
    raw = mcp_server.find_tool_calls(
        agent="claude", tool_name="Bash", redact=False
    )
    assert "hunter2x9extra" in json.dumps(raw["records"], ensure_ascii=False)
    assert "redactions" not in raw


def test_get_body_redacts_turn_text(fake_claude_secret_session: str) -> None:
    events = mcp_server.query(
        agent="claude", session=fake_claude_secret_session, type="user_turn"
    )["events"]
    target = next(e for e in events if "[REDACTED_OPENAI_KEY]" in e["text"])
    body = mcp_server.get_body(target["id"])
    assert "[REDACTED_OPENAI_KEY]" in body["text"]
    assert OPENAI_KEY not in body["text"]
    assert body["redactions"]["OPENAI_KEY"] >= 1
    raw = mcp_server.get_body(target["id"], redact=False)
    assert OPENAI_KEY in raw["text"]
    assert "redactions" not in raw


# ---------------------------------------------------------------------------
# Empty-result diagnostics link (F1.1 × F2.1)
# ---------------------------------------------------------------------------


def test_hint_on_placeholder_filter() -> None:
    diag = empty_result_diagnostics(
        agent="claude",
        filters={"text": "[REDACTED_OPENAI_KEY]"},
        redact_active=True,
    )
    assert any("placeholder" in h for h in diag["hints"])
    assert any("redact=false" in h for h in diag["hints"])


def test_hint_on_secret_looking_filter() -> None:
    diag = empty_result_diagnostics(
        agent="claude",
        filters={"text": OPENAI_KEY},
        redact_active=True,
    )
    assert any("redaction is enabled" in h for h in diag["hints"])
    assert any("redact=false" in h for h in diag["hints"])


def test_no_hint_when_redact_inactive_or_benign() -> None:
    diag = empty_result_diagnostics(
        agent="claude", filters={"text": OPENAI_KEY}, redact_active=False
    )
    assert not any("redact=false" in h for h in diag["hints"])
    diag = empty_result_diagnostics(
        agent="claude", filters={"text": "plain words"}, redact_active=True
    )
    assert not any("redact=false" in h for h in diag["hints"])


def test_query_empty_result_carries_redaction_hint(
    fake_claude_secret_session: str,
) -> None:
    """E2E: searching for a placeholder yields the F2.1 diagnostics hint."""
    out = mcp_server.query(agent="claude", text="[REDACTED_OPENAI_KEY]")
    assert out["count"] == 0
    hints = out["diagnostics"]["hints"]
    assert any("placeholder" in h for h in hints)
