"""The ``network`` preset (F4.3) — hermetic tests.

Unit layer: target extraction (:func:`request_fields`) and the risk
dictionary (:func:`assess_request`).
Core layer: :func:`ai_r.network.network` over synthetic Claude/Codex
sessions written under the per-test ``AI_R_HOME`` (extraction, kinds,
filters, rollups, caps, ordering, redaction, diagnostics).
MCP layer: registration + the thin-wrapper error contract.

Everything here is hermetic; no host data is read.  URLs/secrets in
fixtures are DATA (session text under a temp dir), never dereferenced.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_r import mcp_server
from ai_r.events._common import resolve_tool
from ai_r.network import (
    KIND_VALUES,
    RISK_LABELS,
    RISK_MODES,
    assess_request,
    network,
    request_fields,
)

# Assembled so no live-looking secret literal appears in the source
# (the strings are session DATA, never sent anywhere).
GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2  # 40 chars after prefix
SECRET_URL = f"https://api.example.com/repos?token={GH_TOKEN}"
CRED_URL = "http://user:hunter2@intranet.example.com/status"
PLAIN_URL = "https://docs.python.org/3/library/ipaddress.html"
LOCAL_URL = "http://127.0.0.1:8000/admin"
PUNY_URL = "https://xn--e1afmkfd.example.com/login"


# ---------------------------------------------------------------------------
# Builders (Claude JSONL shape, mirrors test_incidents.py)
# ---------------------------------------------------------------------------


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


def _web(name: str, inp: dict, call_id: str) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": inp}


def _write_claude(tmp_sessions_dir: Path, uuid: str, records: list) -> Path:
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-net" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return jsonl


@pytest.fixture
def web_session(tmp_sessions_dir: Path) -> str:
    """One Claude session: a clean fetch, a search, and two risky fetches.

    * msg1 user request; msg2 WebFetch of a clean https URL + WebSearch.
    * msg3 WebFetch of a plain-http localhost URL (2 risk labels).
    * msg4 WebFetch of a URL carrying a GitHub token in the query string.
    * msg5 a Bash call — must NOT appear in the audit.
    """
    uuid = "sess-network-1"
    records = [
        _user("собери доки", "2026-06-20T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "fetching docs"},
                _web("WebFetch", {"url": PLAIN_URL, "prompt": "summarize"}, "w1"),
                _web("WebSearch", {"query": "python ipaddress module"}, "w2"),
            ],
            "2026-06-20T10:00:05Z",
            uuid,
        ),
        _assistant(
            [
                {"type": "text", "text": "checking local admin"},
                _web("WebFetch", {"url": LOCAL_URL, "prompt": "check"}, "w3"),
            ],
            "2026-06-20T10:01:00Z",
            uuid,
        ),
        _assistant(
            [
                {"type": "text", "text": "calling api"},
                _web("WebFetch", {"url": SECRET_URL, "prompt": "list"}, "w4"),
            ],
            "2026-06-20T10:02:00Z",
            uuid,
        ),
        _assistant(
            [
                {"type": "text", "text": "local listing"},
                {
                    "type": "tool_use",
                    "id": "b1",
                    "name": "Bash",
                    "input": {"command": "ls -la"},
                },
            ],
            "2026-06-20T10:03:00Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    return uuid


# ---------------------------------------------------------------------------
# Unit: target extraction
# ---------------------------------------------------------------------------


def test_request_fields_url_and_query_keys() -> None:
    assert request_fields({"url": PLAIN_URL, "prompt": "x"}) == (PLAIN_URL, None)
    assert request_fields({"query": "hello world"}) == (None, "hello world")
    # Both present (never happens today, but the contract is stable).
    assert request_fields({"url": PLAIN_URL, "query": "q"}) == (PLAIN_URL, "q")


def test_request_fields_url_embedded_in_prompt() -> None:
    """Gemini ``web_fetch`` shape: the URL lives inside the prompt text."""
    url, query = request_fields(
        {"prompt": f"Summarize {PLAIN_URL} please"}
    )
    assert url == PLAIN_URL
    assert query is None


def test_request_fields_honest_nulls() -> None:
    assert request_fields({"format": "markdown"}) == (None, None)
    assert request_fields("no url here") == (None, None)
    assert request_fields(None) == (None, None)


# ---------------------------------------------------------------------------
# Unit: risk dictionary
# ---------------------------------------------------------------------------


def test_risk_dictionary_hits() -> None:
    # user:pass@ is both a structural risk AND a redactable secret
    # (URL_CREDENTIALS) — both labels fire, honestly.
    assert assess_request(CRED_URL) == [
        "plain_http", "credentials_in_url", "secret_in_url",
    ]
    assert assess_request(SECRET_URL) == ["secret_in_url"]
    assert assess_request(LOCAL_URL) == [
        "plain_http", "ip_literal_host", "private_or_local_host",
    ]
    assert assess_request(PUNY_URL) == ["punycode_host"]
    assert assess_request("https://192.168.1.10/api") == [
        "ip_literal_host", "private_or_local_host",
    ]
    # Public raw IP: literal, but not private.
    assert assess_request("https://8.8.8.8/dns") == ["ip_literal_host"]
    assert assess_request("http://ci.internal/build") == [
        "plain_http", "private_or_local_host",
    ]
    assert assess_request(None, f"try key {GH_TOKEN}") == ["secret_in_query"]


def test_risk_dictionary_negatives() -> None:
    assert assess_request(PLAIN_URL) == []
    assert assess_request(None, "python ipaddress module") == []
    assert assess_request(None, None) == []
    # Unparsable URL: no URL-shape risks are guessed.
    assert "ip_literal_host" not in assess_request("https://[not-a-host/")


def test_risk_vocabulary_shape() -> None:
    assert set(RISK_LABELS) >= {
        "plain_http", "secret_in_url", "private_or_local_host",
    }
    assert RISK_MODES == {"include", "only", "exclude"}
    assert KIND_VALUES == {"fetch", "search"}


def test_antigravity_web_names_classified() -> None:
    """Gemini/Antigravity web tool names resolve to the web kind."""
    assert resolve_tool("google_web_search", None)[0] == "web"
    assert resolve_tool("web_fetch", None)[0] == "web"


# ---------------------------------------------------------------------------
# Core: extraction + rollups on a synthetic session
# ---------------------------------------------------------------------------


def test_requests_shape_and_rollups(web_session: str) -> None:
    out = network(agent="claude")
    assert out["count"] == 4
    assert out["risky_count"] == 2
    assert out["truncated"] is False
    assert "diagnostics" not in out

    reqs = out["requests"]  # chronological (ts ascending)
    fetch1, search1, local, secret = reqs
    assert fetch1["kind"] == "fetch"
    assert fetch1["url"] == PLAIN_URL
    assert fetch1["domain"] == "docs.python.org"
    assert fetch1["risks"] == []
    assert fetch1["tool"] == "WebFetch"
    assert fetch1["agent"] == "claude"
    assert fetch1["session_id"] == web_session
    assert fetch1["id"].startswith(web_session + ":")
    # No correlated tool_result in the fixture → honest null.
    assert fetch1["is_error"] is None

    assert search1["kind"] == "search"
    assert search1["url"] is None
    assert search1["domain"] is None
    assert search1["query"] == "python ipaddress module"

    assert local["risks"] == [
        "plain_http", "ip_literal_host", "private_or_local_host",
    ]
    assert local["domain"] == "127.0.0.1"
    assert secret["risks"] == ["secret_in_url"]

    # Rollups reflect the FULL match set; searches carry no domain.
    assert out["by_domain"] == {
        "docs.python.org": 1, "127.0.0.1": 1, "api.example.com": 1,
    }
    assert out["by_risk"] == {
        "plain_http": 1,
        "ip_literal_host": 1,
        "private_or_local_host": 1,
        "secret_in_url": 1,
    }


def test_bash_calls_never_appear(web_session: str) -> None:
    out = network(agent="claude")
    assert all(r["tool"] != "Bash" for r in out["requests"])


def test_kind_filter(web_session: str) -> None:
    fetches = network(agent="claude", kind="fetch")
    assert fetches["count"] == 3
    assert all(r["kind"] == "fetch" for r in fetches["requests"])
    searches = network(agent="claude", kind="search")
    assert searches["count"] == 1
    assert searches["requests"][0]["query"] == "python ipaddress module"


def test_risk_modes_filter(web_session: str) -> None:
    only = network(agent="claude", risk="only")
    assert only["count"] == 2
    assert all(r["risks"] for r in only["requests"])
    excl = network(agent="claude", risk="exclude")
    assert excl["count"] == 2
    assert all(not r["risks"] for r in excl["requests"])


def test_domain_filter_equals_or_subdomain(web_session: str) -> None:
    exact = network(agent="claude", domain="docs.python.org")
    assert exact["count"] == 1
    parent = network(agent="claude", domain="python.org")
    assert parent["count"] == 1  # docs.python.org is a subdomain
    other = network(agent="claude", domain="example.org")
    assert other["count"] == 0
    assert "diagnostics" in other  # empty result is explainable
    # A search (no URL) never matches a domain filter.
    assert all(
        r["kind"] == "fetch" for r in parent["requests"]
    )


def test_limit_and_truncated(web_session: str) -> None:
    out = network(agent="claude", limit=1)
    assert out["count"] == 4  # totals reflect the full match set
    assert len(out["requests"]) == 1
    assert out["truncated"] is True
    assert out["by_risk"]["secret_in_url"] == 1  # counted despite slice


def test_session_scope_single_and_list(web_session: str) -> None:
    scoped = network(session=web_session)
    assert scoped["count"] == 4
    listed = network(session=[web_session, "no-such-uuid"])
    assert listed["count"] == 4  # unknown uuid contributes nothing


def test_empty_corpus_diagnostics() -> None:
    """A bare AI_R_HOME → zero requests + diagnostics, never a crash."""
    out = network(agent="claude")
    assert out["count"] == 0
    assert out["requests"] == []
    assert out["risky_count"] == 0
    assert out["by_domain"] == {}
    assert out["by_risk"] == {}
    assert "diagnostics" in out


def test_redaction_on_emitted_url(web_session: str) -> None:
    """The token in the URL is masked by default; matching ran on RAW."""
    out = network(agent="claude", risk="only")
    secret_rec = [
        r for r in out["requests"] if "secret_in_url" in r["risks"]
    ][0]
    # ``token=<value>`` is matched by the GENERIC_SECRET pattern (the
    # ``token=`` key wins at an earlier offset than the bare ghp_ value) —
    # either way the raw token never leaves the record.
    assert "[REDACTED_GENERIC_SECRET]" in secret_rec["url"]
    assert GH_TOKEN not in secret_rec["url"]
    assert out["redactions"].get("GENERIC_SECRET") == 1
    # redact=False returns the raw URL — and the SAME record is found
    # either way (assessment always runs on RAW strings).
    raw = network(agent="claude", risk="only", redact=False)
    raw_rec = [
        r for r in raw["requests"] if "secret_in_url" in r["risks"]
    ][0]
    assert GH_TOKEN in raw_rec["url"]
    assert "redactions" not in raw


def test_secret_on_cap_boundary_never_leaks_partially(
    tmp_sessions_dir: Path,
) -> None:
    """Redaction runs on the FULL url BEFORE the cap cut.

    The token is positioned so a raw-string cut at the 500-char cap edge
    would slice through it — the truncated tail would be too short to trip
    the redaction pattern on its own and would leak.  With the correct
    order (redact full string → cap) the emitted field carries the mask
    and not a single fragment of the secret.
    """
    uuid = "sess-net-boundary"
    prefix = "https://api.example.com/v1/data?path="
    pad = "x" * (495 - len(prefix) - len("&token="))
    url = prefix + pad + "&token=" + GH_TOKEN + "&page=2"
    split_at = 500 - (len(prefix) + len(pad) + len("&token="))
    assert 0 < split_at < len(GH_TOKEN)  # a raw cut WOULD slice the token
    records = [
        _user("go", "2026-06-20T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "fetch"},
                _web("WebFetch", {"url": url, "prompt": "x"}, "w1"),
            ],
            "2026-06-20T10:00:05Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = network(agent="claude")
    assert out["count"] == 1
    rec = out["requests"][0]
    assert rec["url_truncated"] is True
    assert len(rec["url"]) <= 501  # cap + ellipsis mark
    assert "ghp_" not in rec["url"]  # not even a partial slice survives
    assert out["redactions"]  # the mask was applied to the FULL string
    assert "secret_in_url" in rec["risks"]


def test_is_error_correlated_when_result_exists(
    tmp_sessions_dir: Path,
) -> None:
    """A correlated tool_result surfaces is_error (True here), not null."""
    uuid = "sess-net-err"
    records = [
        _user("go", "2026-06-20T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "fetch"},
                _web("WebFetch", {"url": PLAIN_URL, "prompt": "x"}, "boom-1"),
            ],
            "2026-06-20T10:00:05Z",
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
                        "content": "fetch failed: 403",
                        "is_error": True,
                    }
                ],
            },
            "timestamp": "2026-06-20T10:00:06Z",
            "sessionId": uuid,
        },
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = network(agent="claude")
    assert out["count"] == 1
    assert out["requests"][0]["is_error"] is True


def test_multiple_web_calls_one_message_pairing(
    tmp_sessions_dir: Path,
) -> None:
    """Two web calls in ONE assistant message pair with the right inputs."""
    uuid = "sess-net-two"
    records = [
        _user("go", "2026-06-20T10:00:00Z", uuid),
        _assistant(
            [
                {"type": "text", "text": "both"},
                _web("WebSearch", {"query": "first search"}, "w1"),
                _web("WebFetch", {"url": PLAIN_URL, "prompt": "x"}, "w2"),
            ],
            "2026-06-20T10:00:05Z",
            uuid,
        ),
    ]
    _write_claude(tmp_sessions_dir, uuid, records)
    out = network(agent="claude")
    assert out["count"] == 2
    first, second = out["requests"]
    assert first["kind"] == "search"
    assert first["query"] == "first search"
    assert second["kind"] == "fetch"
    assert second["url"] == PLAIN_URL


def test_cross_agent_codex_web_search_call(tmp_sessions_dir: Path) -> None:
    """Codex ``web_search_call`` records participate — all agents equal."""
    uuid = "test-codex-net"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "20"
        / f"rollout-2026-06-20T10-00-00-{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-06-20T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": uuid, "cwd": "/tmp/p"},
        },
        {
            "timestamp": "2026-06-20T10:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "find docs"}],
            },
        },
        {
            "timestamp": "2026-06-20T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": "opencode cli install docs",
                    "queries": ["opencode cli install docs"],
                },
            },
        },
        {
            "timestamp": "2026-06-20T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "open_page", "url": "https://opencode.ai/"},
            },
        },
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    out = network(agent="codex")
    assert out["count"] == 2
    search, fetch = out["requests"]
    assert search["agent"] == "codex"
    assert search["tool"] == "web_search"
    assert search["kind"] == "search"
    assert search["query"] == "opencode cli install docs"
    assert fetch["kind"] == "fetch"
    assert fetch["url"] == "https://opencode.ai/"
    assert fetch["domain"] == "opencode.ai"
    # Codex has no per-result flag for web_search_call → honest null.
    assert search["is_error"] is None
    assert fetch["is_error"] is None


# ---------------------------------------------------------------------------
# Core: validation (fail-loud)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"kind": "download"}, "kind"),
        ({"risk": "high"}, "risk"),
        ({"domain": "   "}, "domain"),
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
        network(**kwargs)


# ---------------------------------------------------------------------------
# MCP layer: registration + thin-wrapper contract
# ---------------------------------------------------------------------------


def test_mcp_network_registered() -> None:
    assert "network" in mcp_server.mcp._tool_manager._tools


def test_mcp_network_result_shape(web_session: str) -> None:
    out = mcp_server.network(agent="claude")
    assert out["count"] == 4
    assert out["risky_count"] == 2
    assert {r["kind"] for r in out["requests"]} == {"fetch", "search"}


def test_mcp_network_invalid_argument_dict() -> None:
    out = mcp_server.network(risk="maybe")
    assert out["error"] == "invalid_argument"
    assert "risk" in out["message"]
    out2 = mcp_server.network(kind="upload")
    assert out2["error"] == "invalid_argument"
