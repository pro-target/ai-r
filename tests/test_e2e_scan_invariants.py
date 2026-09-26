"""E2E (host) scan-invariant checks against the REAL Claude + ZCode stores.

Runs the live repro trio that surfaced the "stale scan" report (main
@ 623e28d, 2026-09-26):

1. a WIDE ``since 1970-01-01`` scan must EMIT everything it matches —
   pre-fix it tail-cut the newest records at the flat 4 MB byte budget
   (``output_truncated=True``, max ts stuck at 2026-09-12 while the store
   had 09-13/14 calls);
2. the wide EMISSION must be a superset of any NARROW ``since`` window
   inside it — the reported contradiction;
3. the unfiltered ``count`` must equal the sum of per-agent counts.

Byte-determinism: the scans run over :func:`real_live_stores` — a frozen
view of the real store bytes (see the fixture docstring for why equality
checks cannot read the live tree).  Auto-tagged ``host`` (deselected by
``make test-hermetic``); skips on hosts without the stores or without any
``mcp__`` calls.  Fails with a non-zero pytest exit code on any invariant
breach.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ai_r.find_tool_calls import find_tool_calls
from ai_r.parsers import PARSERS

# The wide-audit shape from the live repro: an explicit, generous count
# contract that the byte budget must not silently override.
_WIDE_LIMIT = 50_000


def _key(rec: dict) -> tuple:
    return (
        rec["agent"],
        rec["session_uuid"],
        rec["message_index"],
        rec["tool"],
        rec["timestamp"],
    )


def test_e2e_repro_trio_real_stores(real_live_stores) -> None:
    """The repro trio on the frozen REAL corpus — all three invariants."""
    wide = find_tool_calls(
        tool_name_pattern="mcp__",
        agent="claude",
        since="1970-01-01",
        limit=_WIDE_LIMIT,
    )
    if wide["count"] == 0:
        pytest.skip("real Claude store has no mcp__ tool calls on this host")

    # (1) The count contract holds at the emission level: everything
    # matched is emitted, nothing tail-cut by the byte budget.
    assert wide["output_truncated"] is False, (
        "wide scan hit the output byte budget — an explicit "
        f"limit={_WIDE_LIMIT} contract must scale it"
    )
    assert wide["truncated"] is False
    assert len(wide["records"]) == wide["count"], (
        f"emission {len(wide['records'])} != matched {wide['count']}"
    )
    stamps = [r["timestamp"] for r in wide["records"] if r["timestamp"]]
    assert stamps, "wide scan emitted no timestamped records"
    wide_keys = {_key(r) for r in wide["records"]}

    # (2) Wide ⊇ narrow: derive a narrow window INSIDE the wide range
    # (machine-independent — the live repro used a fixed 2026-09-13 cutoff,
    # here it is the newest wide record minus two days).
    newest = max(datetime.fromisoformat(s) for s in stamps)
    narrow_since = (newest - timedelta(days=2)).isoformat()
    narrow = find_tool_calls(
        tool_name_pattern="mcp__",
        agent="claude",
        since=narrow_since,
        limit=_WIDE_LIMIT,
    )
    assert narrow["count"] > 0, "narrow window matched nothing — widen it"
    assert narrow["output_truncated"] is False
    missing = {_key(r) for r in narrow["records"]} - wide_keys
    assert not missing, (
        f"wide since 1970 emission is missing {len(missing)} record(s) the "
        f"narrow since {narrow_since} window emits: {sorted(missing)[:3]}"
    )
    assert wide["count"] >= narrow["count"]

    # (3) Unfiltered count == sum of per-agent counts (exact on the frozen
    # bytes; exercises every parser in the registry).
    per_agent = {
        agent.value.lower(): find_tool_calls(
            tool_name_pattern="mcp__", agent=agent.value.lower(), limit=0
        )["count"]
        for agent in PARSERS
    }
    total = find_tool_calls(tool_name_pattern="mcp__", limit=0)["count"]
    assert total == sum(per_agent.values()), (
        f"unfiltered count {total} != per-agent sum "
        f"{sum(per_agent.values())} ({per_agent})"
    )

    # Every emitted record carries a timestamp (the reported zcode
    # ``timestamp: None`` defect must stay dead on the real corpus).
    assert all(r["timestamp"] for r in wide["records"])
