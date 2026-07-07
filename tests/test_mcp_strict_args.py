"""Fail-loud on unknown MCP tool arguments.

Regression guard for the silent-drop class the self-referential usage audit
surfaced: callers passed ``plan(limit=…)`` and ``list_sessions(since=…)`` —
parameters absent from those tools' schemas — and the FastMCP transport
dropped them without error, returning an unfiltered result that *looked*
scoped.  ``_StrictArgsFastMCP`` now rejects any undeclared argument up front.
"""

from __future__ import annotations

import asyncio

from ai_r.mcp_server import _unknown_tool_args, mcp


# --- _unknown_tool_args (pure) ---------------------------------------------

def test_unknown_args_flags_undeclared_keys() -> None:
    schema = {"properties": {"session": {}, "kind": {}}}
    assert _unknown_tool_args(schema, {"session": "x", "limit": 1}) == ["limit"]


def test_unknown_args_empty_when_all_declared() -> None:
    schema = {"properties": {"session": {}, "kind": {}}}
    assert _unknown_tool_args(schema, {"session": "x", "kind": "final"}) == []


def test_unknown_args_sorted_multiple() -> None:
    schema = {"properties": {"a": {}}}
    assert _unknown_tool_args(schema, {"z": 1, "a": 2, "m": 3}) == ["m", "z"]


def test_unknown_args_missing_properties_treats_all_as_unknown() -> None:
    assert _unknown_tool_args({}, {"a": 1}) == ["a"]


# --- _StrictArgsFastMCP.call_tool (integration, hermetic) ------------------

def test_call_tool_rejects_phantom_plan_limit() -> None:
    """``plan(limit=…)`` — the real footgun — fails loud before any data read."""
    result = asyncio.run(mcp.call_tool("plan", {"session": "abc", "limit": 1}))
    assert isinstance(result, dict)
    assert result["error"] == "invalid_argument"
    assert "limit" in result["message"]
    # The message points the caller at the real surface.
    assert "plan accepts:" in result["message"]


def test_call_tool_rejects_phantom_list_sessions_since() -> None:
    result = asyncio.run(mcp.call_tool("list_sessions", {"since": "2026-07-05"}))
    assert isinstance(result, dict)
    assert result["error"] == "invalid_argument"
    assert "since" in result["message"]


def test_call_tool_allows_declared_args_through() -> None:
    """A fully-declared call is NOT short-circuited — it reaches the tool.

    ``detect_current`` is hermetic (reads runtime env, no vault), so a valid
    call returns the tool's own result rather than the unknown-argument error.
    """
    result = asyncio.run(mcp.call_tool("detect_current", {"agent": "claude"}))
    # Not our rejection envelope: either non-dict tool content, or a dict that
    # is not the invalid_argument error for an unknown key.
    if isinstance(result, dict):
        assert result.get("error") != "invalid_argument" or "unknown argument" not in result.get(
            "message", ""
        )
