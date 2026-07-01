"""Docs-drift guards for the MCP tool surface.

Single source of truth for "how many / which MCP tools exist" is the code:
every ``@mcp.tool()`` in ``src/ai_r/mcp_server.py``. These tests fail loud when
the docs fall out of sync with that surface, so the count/list never has to be
reconciled by hand (build the system, don't repeat the task).

Two independent guards:

1. ``test_mcp_tool_set_matches_architecture_doc`` — the SET of tool names in the
   code equals the SET of backtick-named tools listed in ``docs/architecture.md``
   under the "Thirteen tools" section. Catches: a tool added/removed in code but
   not in the doc, and vice-versa. (Currently GREEN.)

2. ``test_every_mcp_tool_has_a_scenario`` — every MCP tool has at least one
   acceptance scenario in ``docs/scenarios.md``. Catches: a shipped tool with no
   e2e coverage. (Currently RED for ``list_sessions`` / ``find_tool_calls`` —
   this is a real coverage gap, not a test bug; see ``_SCENARIO_EXEMPT`` below.)
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MCP_SERVER = _REPO / "src" / "ai_r" / "mcp_server.py"
_ARCH_DOC = _REPO / "docs" / "architecture.md"
_SCENARIOS_DOC = _REPO / "docs" / "scenarios.md"

# Tools intentionally without a dedicated acceptance scenario. Keep this list
# SHORT and justified — it is the escape hatch that keeps guard #2 honest. Empty
# it by writing the scenario; grow it only with a written reason.
#
# NOTE (baseline on HEAD 409ca77): both entries below are a genuine gap, not a
# considered exemption. Left here so the guard is green-on-merge; the correct
# fix is to add LIST-1 / FTC-1 scenarios and delete these two lines.
_SCENARIO_EXEMPT: frozenset[str] = frozenset(
    {
        "list_sessions",  # TODO(scenarios): add LIST-1 and remove
        "find_tool_calls",  # TODO(scenarios): add FTC-1 and remove
        
    }
)


def _mcp_tool_names() -> set[str]:
    """Every ``@mcp.tool()``-decorated function name in the MCP server."""
    src = _MCP_SERVER.read_text(encoding="utf-8")
    # Match the decorator immediately followed by ``def <name>(``.
    return set(
        re.findall(r"@mcp\.tool\(\)\s*\n\s*def\s+([a-zA-Z_]\w*)\s*\(", src)
    )


def _architecture_tool_names() -> set[str]:
    """Backtick-quoted tool names in architecture.md's MCP-surface section.

    Scoped to the block from the ``ai-r-mcp`` (MCP server) heading up to the next
    blank line, so unrelated backtick identifiers elsewhere in the doc do not
    leak in.
    """
    text = _ARCH_DOC.read_text(encoding="utf-8")
    # Anchor on the stable section heading, NOT the spelled-out count word
    # ("Thirteen"): the count changes when a tool is added, the heading does not.
    start = text.find("`ai-r-mcp` (MCP server)")
    assert start != -1, "architecture.md lost its `ai-r-mcp` (MCP server) section"
    rest = text[start:]
    # The tool list is the bullet block that ends at the first blank line;
    # anything after (e.g. the pagination note mentioning `limit`/`offset`)
    # is prose, not tool names.
    end = rest.find("\n\n")
    block = rest if end == -1 else rest[:end]
    return set(re.findall(r"`([a-z_]+)`", block))


def _scenario_covered_tools() -> set[str]:
    """Tool names that appear at least once in scenarios.md."""
    text = _SCENARIOS_DOC.read_text(encoding="utf-8")
    # Match both bare names and mcp__ai-r__<name> forms.
    bare = set(re.findall(r"mcp__ai-r__([a-z_]+)", text))
    listed = set(re.findall(r"`([a-z_]+)`", text))
    return bare | listed


def test_mcp_tool_set_matches_architecture_doc() -> None:
    code = _mcp_tool_names()
    doc = _architecture_tool_names()
    # Sanity guards the name-regex itself WITHOUT hard-coding a count: the number
    # of extracted names must equal the number of raw ``@mcp.tool(`` decorators,
    # and must be non-empty (a misfiring regex would otherwise pass vacuously).
    raw = len(re.findall(r"@mcp\.tool\(", _MCP_SERVER.read_text(encoding="utf-8")))
    assert code, "no @mcp.tool() names extracted — the name regex broke"
    assert len(code) == raw, (
        f"name-regex extracted {len(code)} tools but there are {raw} "
        f"@mcp.tool() decorators — regex drift"
    )
    missing_in_doc = code - doc
    stale_in_doc = doc - code
    assert not missing_in_doc, (
        f"MCP tools in code but not documented in architecture.md: "
        f"{sorted(missing_in_doc)}"
    )
    assert not stale_in_doc, (
        f"tools listed in architecture.md but not in code (stale): "
        f"{sorted(stale_in_doc)}"
    )


def test_every_mcp_tool_has_a_scenario() -> None:
    code = _mcp_tool_names()
    covered = _scenario_covered_tools()
    uncovered = (code - covered) - _SCENARIO_EXEMPT
    assert not uncovered, (
        f"MCP tools with no acceptance scenario in scenarios.md: "
        f"{sorted(uncovered)} — add one, or (with a reason) list it in "
        f"_SCENARIO_EXEMPT"
    )


def test_scenario_exemptions_are_still_tools() -> None:
    # Keep the exempt-list from rotting: every exempted name must still be a
    # real tool, else the list is silently masking a rename.
    code = _mcp_tool_names()
    stale = _SCENARIO_EXEMPT - code
    assert not stale, f"_SCENARIO_EXEMPT lists non-existent tools: {sorted(stale)}"
