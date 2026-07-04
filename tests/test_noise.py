"""Hermetic tests for the noise criterion + the ``noise=`` parameter (F1.2).

Covers three layers:

1. The criterion itself (``ai_r.parsers._noise``): noise == spawned
   subagent session (``kind == "subagent"`` or ``parent_uuid`` set).
2. Parser-level subagent detection for the agents fixed in F1.2:
   OpenCode (``session.parent_id``), Codex (``session_meta.payload
   .thread_source``/``parent_thread_id`` incl. the nested
   ``source.subagent.thread_spawn`` fallback), Pi (``parentSession``
   promoted from ``extra`` to first-class ``parent_uuid``/``kind``).
3. The public surface: ``noise=exclude|include|only`` on
   ``list_sessions`` / ``search_sessions`` / ``query`` (+ fail-loud on an
   unknown mode).

Everything runs against fake fixtures under ``AI_R_HOME`` — no host data.
NOTE: MCP-level assertions are scoped to a single agent because the
OpenCode parser resolves its DB independently of ``AI_R_HOME`` (host-leak
guard, see tests/conftest.py); OpenCode is exercised at parser level with
an explicit ``override=``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_r.mcp_server import list_sessions, query, search_sessions
from ai_r.parsers import AgentName, codex, opencode, pi
from ai_r.parsers._noise import (
    NOISE_MODES,
    is_noise,
    noise_allows,
    validate_noise,
)
from ai_r.parsers.models import Session


def _mk_session(kind: str = "agent", parent_uuid: str | None = None) -> Session:
    return Session(
        uuid="s-1",
        agent=AgentName.CLAUDE,
        title="t",
        date=datetime(2026, 6, 14, tzinfo=timezone.utc),
        path="/nope",
        message_count=0,
        parent_uuid=parent_uuid,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# 1. Criterion (SSOT)
# ---------------------------------------------------------------------------


def test_is_noise_criterion() -> None:
    assert not is_noise(_mk_session())
    assert is_noise(_mk_session(kind="subagent"))
    # Either signal alone is sufficient (defensive vs. half-filled parsers).
    assert is_noise(_mk_session(parent_uuid="p-1"))
    assert is_noise(_mk_session(kind="subagent", parent_uuid="p-1"))


def test_noise_allows_matrix() -> None:
    top, sub = _mk_session(), _mk_session(kind="subagent", parent_uuid="p-1")
    assert noise_allows(top, "include") and noise_allows(sub, "include")
    assert noise_allows(top, "exclude") and not noise_allows(sub, "exclude")
    assert not noise_allows(top, "only") and noise_allows(sub, "only")


def test_validate_noise_fail_loud() -> None:
    for mode in NOISE_MODES:
        assert validate_noise(mode) == mode
    with pytest.raises(ValueError, match="noise"):
        validate_noise("bogus")


# ---------------------------------------------------------------------------
# 2. Parser-level subagent detection
# ---------------------------------------------------------------------------


def test_opencode_child_session_is_subagent(fake_opencode_db: Path) -> None:
    sessions = {
        s.uuid: s
        for s in opencode.list_sessions(override=str(fake_opencode_db))
    }
    assert sessions["test-oc-1"].kind == "agent"
    assert sessions["test-oc-1"].parent_uuid is None
    # The bug fixed in F1.2: parent_id was set but kind stayed "agent".
    assert sessions["test-oc-2"].kind == "subagent"
    assert sessions["test-oc-2"].parent_uuid == "test-oc-1"


def test_codex_subagent_flat_parent(
    fake_codex_session: Path, fake_codex_subagent: Path
) -> None:
    sessions = {s.uuid: s for s in codex.list_sessions()}
    assert sessions["test-codex-1"].kind == "agent"
    assert sessions["test-codex-1"].parent_uuid is None
    sub = sessions["test-codex-sub-1"]
    assert sub.kind == "subagent"
    assert sub.parent_uuid == "test-codex-1"


def test_codex_subagent_nested_only_parent(
    fake_codex_subagent_nested_only: Path,
) -> None:
    (sub,) = codex.list_sessions()
    assert sub.uuid == "test-codex-sub-2"
    assert sub.kind == "subagent"
    # Flat parent_thread_id absent → recovered from the nested
    # source.subagent.thread_spawn blob.
    assert sub.parent_uuid == "test-codex-1"


def test_pi_parent_session_promoted(
    fake_pi_session: Path, fake_pi_subagent: Path
) -> None:
    sessions = {s.uuid: s for s in pi.list_sessions()}
    assert sessions["test-pi-1"].kind == "agent"
    assert sessions["test-pi-1"].parent_uuid is None
    sub = sessions["test-pi-sub-1"]
    assert sub.kind == "subagent"
    assert sub.parent_uuid == "test-pi-1"
    # Backward compatibility: the extra bag still carries the raw field.
    assert sub.extra["parent_session"] == "test-pi-1"


# ---------------------------------------------------------------------------
# 3. Public surface: noise= on list_sessions / search_sessions / query
# ---------------------------------------------------------------------------


def _uuids(result: dict) -> set[str]:
    return {s["uuid"] for s in result["sessions"]}


def test_list_sessions_noise_modes_codex(
    fake_codex_session: Path, fake_codex_subagent: Path
) -> None:
    include = list_sessions(agent="codex", noise="include")
    assert _uuids(include) == {"test-codex-1", "test-codex-sub-1"}

    exclude = list_sessions(agent="codex", noise="exclude")
    assert _uuids(exclude) == {"test-codex-1"}

    only = list_sessions(agent="codex", noise="only")
    assert _uuids(only) == {"test-codex-sub-1"}
    (sub,) = only["sessions"]
    assert sub["kind"] == "subagent"
    assert sub["parent_uuid"] == "test-codex-1"


def test_list_sessions_default_is_include(
    fake_claude_session: Path, fake_claude_subagent: Path
) -> None:
    default = list_sessions(agent="claude")
    assert _uuids(default) == {"test-claude-1", "agent-sub-1"}


def test_list_sessions_noise_composes_with_kind(
    fake_claude_session: Path, fake_claude_subagent: Path
) -> None:
    # kind and noise AND together: contradictory filters → empty (+diagnostics).
    result = list_sessions(agent="claude", kind="agent", noise="only")
    assert result["total"] == 0
    assert "diagnostics" in result


def test_list_sessions_noise_invalid() -> None:
    result = list_sessions(noise="bogus")
    assert result["error"] == "invalid_argument"
    assert "noise" in result["message"]


def test_search_sessions_noise_modes_pi(
    fake_pi_session: Path, fake_pi_subagent: Path
) -> None:
    def uuids(res: dict) -> set[str]:
        return {s["uuid"] for s in res["results"]}

    include = search_sessions("task", agent="pi", scope="body")
    assert uuids(include) == {"test-pi-sub-1"}  # only the child says "task"

    # The child is noise → excluded even though it matches.
    exclude = search_sessions(
        "task", agent="pi", scope="body", noise="exclude"
    )
    assert uuids(exclude) == set()

    only = search_sessions("task", agent="pi", scope="body", noise="only")
    assert uuids(only) == {"test-pi-sub-1"}


def test_search_sessions_noise_invalid() -> None:
    result = search_sessions("anything", noise="bogus")
    assert result["error"] == "invalid_argument"
    assert "noise" in result["message"]


def test_query_noise_modes_codex(
    fake_codex_session: Path, fake_codex_subagent: Path
) -> None:
    def session_ids(res: dict) -> set[str]:
        return {e["session_id"] for e in res["events"]}

    include = query(agent="codex")
    assert session_ids(include) == {"test-codex-1", "test-codex-sub-1"}

    exclude = query(agent="codex", noise="exclude")
    assert session_ids(exclude) == {"test-codex-1"}

    only = query(agent="codex", noise="only")
    assert session_ids(only) == {"test-codex-sub-1"}


def test_query_noise_invalid() -> None:
    result = query(noise="bogus")
    assert result["error"] == "invalid_argument"
    assert "noise" in result["message"]
