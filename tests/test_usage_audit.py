"""Hermetic coverage for the pure core of scripts/usage_audit.py.

The data-gathering shell (`_fetch_records`, `_declared_params`) reads a real
vault and is exercised by the `make usage-audit` release ritual; here we prove
the fold logic — verb attribution, undeclared-param detection, safety-default
exclusion.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "usage_audit",
    Path(__file__).resolve().parent.parent / "scripts" / "usage_audit.py",
)
usage_audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(usage_audit)

build_report = usage_audit.build_report
_verb_of = usage_audit._verb_of


# --- _verb_of --------------------------------------------------------------

def test_verb_of_prefers_tool_resolved() -> None:
    assert _verb_of({"tool_resolved": "ai-r:query", "tool": "x"}) == "query"


def test_verb_of_falls_back_to_mcp_name() -> None:
    assert _verb_of({"tool": "mcp__ai-r__plan"}) == "plan"


def test_verb_of_ignores_non_ai_r() -> None:
    assert _verb_of({"tool": "Read"}) is None
    assert _verb_of({"tool_resolved": "shell:bash", "tool": "Bash"}) is None


# --- build_report ----------------------------------------------------------

def test_zero_call_param_is_candidate() -> None:
    declared = {"plan": {"session", "kind", "redact"}}
    records = [{"tool_resolved": "ai-r:plan", "input": {"session": "a"}}]
    rep = build_report(records, declared)
    row = rep["verbs"]["plan"]
    assert row["calls"] == 1
    # kind never used -> candidate; redact is a safety-default -> excluded
    assert row["zero_call_params"] == ["kind"]


def test_undeclared_param_flagged() -> None:
    declared = {"plan": {"session", "kind"}}
    records = [{"tool_resolved": "ai-r:plan", "input": {"session": "a", "limit": 1}}]
    rep = build_report(records, declared)
    assert rep["verbs"]["plan"]["undeclared_used"] == ["limit"]


def test_zero_call_verb_reported() -> None:
    declared = {"plan": {"session"}, "network": {"agent"}}
    records = [{"tool_resolved": "ai-r:plan", "input": {"session": "a"}}]
    rep = build_report(records, declared)
    assert rep["zero_call_verbs"] == ["network"]


def test_safety_default_never_a_candidate() -> None:
    declared = {"get_body": {"id", "redact", "shallow"}}
    records = [{"tool": "mcp__ai-r__get_body", "input": {"id": "e1"}}]
    rep = build_report(records, declared)
    # shallow is a real candidate; redact (safety default) is excluded
    assert rep["verbs"]["get_body"]["zero_call_params"] == ["shallow"]


def test_non_ai_r_records_ignored_in_total() -> None:
    declared = {"query": {"text"}}
    records = [
        {"tool": "Read", "input": {}},
        {"tool_resolved": "ai-r:query", "input": {"text": "x"}},
    ]
    rep = build_report(records, declared)
    assert rep["total_calls"] == 1
