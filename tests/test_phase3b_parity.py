"""Phase-3b parity: legacy tools are now thin presets over the enriched verbs.

Phase 3b enriched the verbs (``query(with_intent=...)``,
``aggregate(rank_by=..., kind_split=...)``, ``diff`` over intent-bearing rows)
so two legacy tools could delegate their internals to a verb WITHOUT changing
their external output byte-for-byte:

* ``session_stats`` → ``aggregate(rank_by="stats", kind_split=True)`` over a
  per-session inventory row stream.
* ``session_diff`` (non-codex) → ``diff`` over
  ``query(type=edit|write, with_intent=True)``.

This module proves the delegation is byte-identical — on hermetic fixtures AND
on real host data (host-marked; skips when the host has no sessions).  The full
legacy test suites (``test_session_stats`` / ``test_session_diff``) staying
green is the other half of the compatibility proof.

Two tools stay standalone (documented in ``docs/methods.md``):

* ``find_file_edits`` / ``find_tool_calls`` — their record shape carries
  ``session_title`` / ``session_date`` / ``assistant`` / ``input`` which are
  NOT on a ``query`` event; reproducing them means re-reading the session
  (not a *thin* preset) and would drop codex shell-redirect edits.  This module
  pins that shape gap so a future "delegate it" attempt fails loudly.
* ``search_sessions`` / ``detect-*`` CLI — different granularity / surface.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List

import pytest

from ai_r.events import aggregate, diff, query
from ai_r.find_file_edits import find_file_edits
from ai_r.find_tool_calls import find_tool_calls
from ai_r.parsers import PARSERS, target_agents
from ai_r.session_diff import session_diff
from ai_r.session_stats import group_key, session_stats


# ---------------------------------------------------------------------------
# Hermetic fixture: a Claude session with two edits to the SAME file, each
# preceded by a DIFFERENT user request (so intent attribution is testable).
# ---------------------------------------------------------------------------


def _write_two_intent_edits(uuid: str, edit_path: str) -> None:
    home = Path(os.environ["AI_R_HOME"])
    jsonl = home / ".claude" / "projects" / "proj-3b" / f"{uuid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user",
         "message": {"role": "user", "content": "First request: rename foo"},
         "timestamp": "2026-06-14T10:00:00Z", "sessionId": uuid},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Edit", "input": {
                 "file_path": edit_path, "old_string": "foo", "new_string": "bar"}}]},
         "timestamp": "2026-06-14T10:00:05Z", "sessionId": uuid},
        {"type": "user",
         "message": {"role": "user", "content": "Second request: add docstring"},
         "timestamp": "2026-06-14T10:01:00Z", "sessionId": uuid},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Edit", "input": {
                 "file_path": edit_path, "old_string": "def bar():",
                 "new_string": 'def bar():\n    """d"""'}}]},
         "timestamp": "2026-06-14T10:01:05Z", "sessionId": uuid},
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# query(with_intent=True) reproduces the legacy previous_user_intent
# ---------------------------------------------------------------------------


def test_query_with_intent_matches_legacy_attribution(tmp_path: Path) -> None:
    uuid = "wi-1"
    _write_two_intent_edits(uuid, "/repo/src/mod.py")

    # Default: no intent key (base shape unchanged).
    ev0 = query(type="tool_call(edit)", session=uuid, agent="claude")[0]
    assert "intent" not in ev0

    # with_intent=True: each edit event carries the request behind it, matching
    # what session_diff's previous_user_intent walk-back produces.
    evs = query(type="tool_call(edit)", session=uuid, agent="claude", with_intent=True)
    assert [e["intent"] for e in evs] == [
        "First request: rename foo",
        "Second request: add docstring",
    ]
    legacy = session_diff(uuid, "claude")["files"][0]["edits"]
    assert [e["intent"] for e in legacy] == [e["intent"] for e in evs]


# ---------------------------------------------------------------------------
# session_diff == diff(query(with_intent=True))  — the delegation is live
# ---------------------------------------------------------------------------


def _diff_rows(uuid: str, agent: str = "claude") -> List[dict[str, Any]]:
    rows: List[dict[str, Any]] = []
    for ev in query(type="tool_call", session=uuid, agent=agent, with_intent=True):
        if ev.get("type") not in ("tool_call(edit)", "tool_call(write)"):
            continue
        if any("file" in r for r in ev.get("refs", ())):
            rows.append(ev)
    return rows


def _project_diff(d: dict[str, Any]) -> dict[str, Any]:
    """Symmetric comparison projection for a diff-shaped result.

    Applied to BOTH sides — projecting only the verb side made the parity
    assertion fail the moment the real vault gained a session with
    secret-shaped strings in its edits (F2.1 ``redactions`` appeared on
    the legacy side only).

    The ``redactions`` *counts* are deliberately NOT compared: each path
    counts replacements over its own emitted shape, and the ``diff`` verb
    additionally emits per-file ``hunks`` (the same secret is masked
    there too, inflating its count), while the legacy ``session_diff``
    shape has no file-level ``hunks``.  Equal masked content with
    different counts is correct behaviour, not a parity break.  What IS
    shape-independent — and therefore compared — is the *set of redaction
    types* that fired.
    """
    files = [
        {"file": f["file"], "edits": f["edits"], "diff": f["diff"]}
        for f in d["files"]
    ]
    return {
        "files": files,
        "count": d["count"],
        "caveats": d["caveats"],
        "redaction_types": sorted(d.get("redactions", {})),
    }


def _diff_preset(uuid: str, agent: str = "claude") -> dict[str, Any]:
    return _project_diff(diff(_diff_rows(uuid, agent)))


def test_session_diff_equals_diff_verb_hermetic(tmp_path: Path) -> None:
    uuid = "sd-parity-1"
    _write_two_intent_edits(uuid, "/repo/src/mod.py")
    legacy = _project_diff(session_diff(uuid, "claude"))
    viaverb = _diff_preset(uuid, "claude")
    assert legacy == viaverb  # byte-identical, incl. intents/diff/edits/order


# ---------------------------------------------------------------------------
# session_stats == aggregate(rank_by="stats", kind_split=True) preset
# ---------------------------------------------------------------------------


def _session_rows(agent: str = "claude") -> List[dict[str, Any]]:
    # Mirror ``session_stats``'s INTERNAL enrichment call byte-for-byte:
    # ``redact=False`` (a masked secret could merge two distinct raw intents,
    # drifting the distinct-intent count) and ``size_caps=False`` (the 4 MB
    # byte budget drops records + the intent cap truncates them on a big real
    # vault, undercounting edits/intents — the observed host-parity failure:
    # legacy 4210/623 vs a capped 1295/204).  A preset is a thin chain over the
    # base method with the SAME arguments, so the reconstruction must pass them.
    edits = find_file_edits(path="/", agent=agent, limit=0, redact=False, size_caps=False)
    by: dict[str, dict[str, Any]] = {}
    for r in edits["records"]:
        u = r.get("session_uuid")
        if not u:
            continue
        b = by.setdefault(u, {"edits": 0, "intents": set()})
        b["edits"] += 1
        it = r.get("intent")
        if isinstance(it, str) and it.strip():
            b["intents"].add(it.strip())
    rows: List[dict[str, Any]] = []
    for an in target_agents(agent):
        for s in PARSERS[an].list_sessions():
            e = by.get(s.uuid, {"edits": 0, "intents": set()})
            rows.append({
                "session_uuid": s.uuid,
                "agent": group_key(s, "agent"),
                "dir": group_key(s, "dir"),
                "date": group_key(s, "date"),
                "kind": group_key(s, "kind"),
                "edits": e["edits"],
                "intents": sorted(e["intents"]),
                "messages": int(getattr(s, "message_count", 0) or 0),
            })
    return rows


def _stats_preset(rows: List[dict[str, Any]], group_by: str, top: int) -> dict[str, Any]:
    agg = aggregate(rows, group_by=group_by,
                    metrics=["sessions", "edits", "intents", "agents", "messages"],
                    rank_by="stats", kind_split=True)
    if top:
        agg = {**agg, "groups": agg["groups"][:top]}
    preset = {
        "group_by": agg["group_by"],
        "groups": agg["groups"],
        "totals": {k: agg["totals"][k] for k in ("sessions", "edits", "agents", "agents_list")},
        "kind_split_available": agg["kind_split_available"],
    }
    if "note" in agg:
        preset["note"] = agg["note"]
    return preset


@pytest.mark.parametrize("group_by", ["agent", "date", "kind"])
def test_session_stats_equals_aggregate_preset_hermetic(
    group_by: str, tmp_path: Path
) -> None:
    # Two sessions on different days: day A has more sessions, day B more edits,
    # so the sessions-first (stats) rank is what makes them match.
    home = Path(os.environ["AI_R_HOME"])

    def _sess(uuid: str, day: str, n_edits: int) -> None:
        proj = home / ".claude" / "projects" / f"proj-{uuid}"
        proj.mkdir(parents=True, exist_ok=True)
        recs = []
        for i in range(n_edits):
            recs.append({"type": "user",
                         "message": {"role": "user", "content": f"req {i}"},
                         "timestamp": f"{day}T10:0{i}:00Z", "sessionId": uuid})
            recs.append({"type": "assistant",
                         "message": {"role": "assistant", "content": [
                             {"type": "tool_use", "name": "Edit", "input": {
                                 "file_path": f"/r/f{i}.py",
                                 "old_string": "a", "new_string": "b"}}]},
                         "timestamp": f"{day}T10:0{i}:05Z", "sessionId": uuid})
        (proj / f"{uuid}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
            encoding="utf-8")

    _sess("a1", "2026-06-10", 1)
    _sess("a2", "2026-06-10", 1)
    _sess("b1", "2026-06-11", 5)

    rows = _session_rows("claude")
    for top in (8, 0):
        legacy = session_stats(group_by=group_by, agent="claude", top=top)
        assert legacy == _stats_preset(rows, group_by, top), f"{group_by} top={top}"


# ---------------------------------------------------------------------------
# find_file_edits / find_tool_calls stay standalone — pin the record-shape gap
# ---------------------------------------------------------------------------


def test_find_file_edits_record_shape_richer_than_query_event(tmp_path: Path) -> None:
    """Record fields absent from a ``query`` event → cannot be a THIN preset.

    ``session_title`` / ``session_date`` / ``assistant`` / ``input`` are on the
    ``find_file_edits`` record but not on a ``query`` event, so reproducing the
    record means re-reading the session (a second parse, not a thin fold) — and
    would drop codex shell-redirect edits.  Pin the gap.
    """
    uuid = "ffe-shape-1"
    _write_two_intent_edits(uuid, "/repo/src/mod.py")
    rec = find_file_edits(path="/repo/src/mod.py", agent="claude", limit=1)["records"][0]
    ev = query(type="tool_call(edit)", session=uuid, agent="claude", with_intent=True)[0]
    for legacy_key in ("session_title", "session_date", "assistant", "input",
                       "file", "tool", "session_uuid"):
        assert legacy_key in rec, legacy_key
        assert legacy_key not in ev, legacy_key
    # ``intent`` IS reproducible via with_intent — so it is present on both.
    assert "intent" in rec and "intent" in ev


def test_find_tool_calls_record_shape_richer_than_query_event(tmp_path: Path) -> None:
    uuid = "ftc-shape-1"
    _write_two_intent_edits(uuid, "/repo/src/mod.py")
    rec = find_tool_calls(tool_name="Edit", agent="claude", limit=1)["records"][0]
    ev = query(type="tool_call(edit)", session=uuid, agent="claude", with_intent=True)[0]
    for legacy_key in ("session_title", "session_date", "assistant", "input",
                       "tool", "session_uuid"):
        assert legacy_key in rec, legacy_key
        assert legacy_key not in ev, legacy_key


# ---------------------------------------------------------------------------
# Host parity: session_stats + session_diff on REAL Claude data.
# ---------------------------------------------------------------------------


def test_session_diff_equals_diff_verb_on_real_data(frozen_claude_home: Path) -> None:
    """``session_diff`` == ``diff``-of-``query`` across real Claude sessions.

    Reads a FROZEN snapshot of the vault so the live session the test runs
    inside cannot mutate the data between the two reads.  Skips when the host
    has no sessions.
    """
    parser = PARSERS[list(target_agents("claude"))[0]]
    checked = 0
    examined = 0
    # Bound BOTH the number of edit-sessions verified AND the total sessions
    # scanned: the delegated path materializes a full event stream per session,
    # so an unbounded newest-first walk over a large vault is too slow for CI.
    for s in parser.list_sessions():
        examined += 1
        if examined > 25:
            break
        legacy = session_diff(s.uuid, "claude")
        if legacy["count"] == 0:
            continue
        assert _diff_preset(s.uuid, "claude") == _project_diff(legacy), s.uuid
        checked += 1
        if checked >= 3:
            break
    if checked == 0:
        pytest.skip("no real Claude session with edits in the first 25 scanned")


def test_session_stats_equals_aggregate_preset_on_real_data(
    frozen_claude_home: Path,
) -> None:
    """``session_stats`` == the ``aggregate`` preset across all group_by dims.

    ``session_stats`` re-scans the vault internally, so both sides must read a
    FROZEN snapshot for a clean byte comparison (the live vault mutates between
    the two independent ``find_file_edits`` scans otherwise).
    """
    rows = _session_rows("claude")
    for group_by in ("agent", "dir", "date", "kind"):
        for top in (8, 0):
            legacy = session_stats(group_by=group_by, agent="claude", top=top)
            assert legacy == _stats_preset(rows, group_by, top), f"{group_by} top={top}"
