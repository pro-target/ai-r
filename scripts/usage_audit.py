#!/usr/bin/env python3
"""Self-referential usage audit — the per-release ritual (CONTRIBUTING → Releasing).

Reads ai-r's own history: which ``mcp__ai-r__*`` verbs and parameters were
actually called since a given date, diffed against each verb's *declared*
schema. Turns "is this parameter real or invented?" from opinion into a
measurement — a zero-call declared parameter is a tombstone *candidate*, and
(post the ``_StrictArgsFastMCP`` fix) no call can carry an *undeclared*
parameter silently.

The judgement stays with a human: this prints a candidate list with the
honesty guards the raw idea lacks — safety-default parameters (``redact`` and
kin) are excluded (their absence means the default is right, not that they are
dead), and the sample size / single-agent coverage are reported so a thin
sample is never mistaken for "proven unused".

Usage:
    python scripts/usage_audit.py --since 2026-07-05

``--since`` should be the previous release date. Reads a real vault, so it is
host-dependent by nature; the pure report logic is unit-tested hermetically in
``tests/test_usage_audit.py``. Rationale: the *ADR: fail-loud on unknown MCP
arguments* in ``docs/architecture.md``.
"""

from __future__ import annotations

import argparse
from typing import Any, Iterable, Mapping

# Parameters whose *absence* from real calls means the default is correct, not
# that the parameter is dead surface. Excluded from tombstone candidates.
SAFETY_DEFAULT_PARAMS = frozenset({"redact"})

_MCP_PREFIX = "mcp__ai-r__"


def _verb_of(record: Mapping[str, Any]) -> str | None:
    """The ai-r verb a tool-call record targets, or ``None`` if not an ai-r call.

    Prefers ``tool_resolved`` (``"ai-r:<verb>"``); falls back to the raw
    ``mcp__ai-r__<verb>`` tool name.
    """
    resolved = record.get("tool_resolved")
    if isinstance(resolved, str) and resolved.startswith("ai-r:"):
        return resolved.split(":", 1)[1]
    name = record.get("tool")
    if isinstance(name, str) and name.startswith(_MCP_PREFIX):
        return name[len(_MCP_PREFIX):]
    return None


def build_report(
    records: Iterable[Mapping[str, Any]],
    declared: Mapping[str, set[str]],
    safety: frozenset[str] = SAFETY_DEFAULT_PARAMS,
) -> dict[str, Any]:
    """Fold call records into a per-verb usage report — the pure, testable core.

    Args:
        records: tool-call records (``find_tool_calls`` output); each carries a
            ``tool``/``tool_resolved`` name and an ``input`` dict.
        declared: verb -> its declared schema parameter names.
        safety: parameters excluded from tombstone candidates.

    Returns a dict with per-verb ``{calls, params_used, undeclared_used,
    zero_call_params}`` (``zero_call_params`` already excludes ``safety``),
    plus ``zero_call_verbs`` (declared verbs never called) and totals.
    """
    calls: dict[str, int] = {v: 0 for v in declared}
    used: dict[str, set[str]] = {v: set() for v in declared}
    total = 0
    for rec in records:
        verb = _verb_of(rec)
        if verb is None:
            continue
        total += 1
        calls.setdefault(verb, 0)
        calls[verb] += 1
        params = rec.get("input") or {}
        if isinstance(params, Mapping):
            used.setdefault(verb, set()).update(params.keys())

    per_verb: dict[str, Any] = {}
    for verb in sorted(set(declared) | set(calls)):
        decl = set(declared.get(verb, set()))
        seen = used.get(verb, set())
        per_verb[verb] = {
            "calls": calls.get(verb, 0),
            "params_used": sorted(seen),
            "undeclared_used": sorted(seen - decl),
            "zero_call_params": sorted((decl - seen) - safety),
        }

    return {
        "total_calls": total,
        "zero_call_verbs": sorted(v for v in declared if calls.get(v, 0) == 0),
        "verbs": per_verb,
    }


def _declared_params() -> dict[str, set[str]]:
    """Declared parameter names per ai-r MCP verb, read from the live schemas."""
    from ai_r.mcp_server import mcp

    out: dict[str, set[str]] = {}
    for tool in mcp._tool_manager.list_tools():
        props = (tool.parameters or {}).get("properties", {})
        out[tool.name] = set(props)
    return out


def _fetch_records(since: str) -> tuple[list[dict[str, Any]], int]:
    """Every ``mcp__ai-r__*`` tool call since ``since`` (real vault read)."""
    from ai_r.find_tool_calls import find_tool_calls

    resp = find_tool_calls(
        tool_name_pattern=_MCP_PREFIX, since=since, limit=0, redact=True
    )
    records = resp.get("records", []) if isinstance(resp, dict) else []
    return records, len(records)


def _agents_in(records: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({r.get("agent") for r in records if r.get("agent")})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--since",
        required=True,
        help="Previous release date (ISO), e.g. 2026-07-05.",
    )
    args = ap.parse_args()

    declared = _declared_params()
    records, n = _fetch_records(args.since)
    report = build_report(records, declared)
    agents = _agents_in(records)

    print(f"# ai-r self-referential usage audit — since {args.since}")
    print(f"# {report['total_calls']} ai-r calls; agents: {', '.join(agents) or '(none)'}")
    if len(agents) <= 1:
        print("# NOTE: single-agent coverage — cross-agent MCP usage NOT measured.")
    print()
    if report["zero_call_verbs"]:
        print(f"ZERO-CALL VERBS (strong retirement signal): {', '.join(report['zero_call_verbs'])}")
        print()

    print(f"{'verb':22} {'calls':>5}  zero-call params (candidates; safety-defaults excluded)")
    print("-" * 78)
    for verb in sorted(report["verbs"], key=lambda v: -report["verbs"][v]["calls"]):
        row = report["verbs"][verb]
        cand = ", ".join(row["zero_call_params"]) or "—"
        print(f"{verb:22} {row['calls']:>5}  {cand}")
        if row["undeclared_used"]:
            # Post-fix this should never happen (STRICT-1 rejects it); flag loudly.
            print(f"{'':22} {'':>5}  !! UNDECLARED PARAMS USED: {', '.join(row['undeclared_used'])}")

    print()
    print("# Candidates are advisory — a human decides. Small samples / single-agent")
    print("# coverage weaken any zero-call signal; re-measure next release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
