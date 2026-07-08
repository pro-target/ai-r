"""Shared parity-test helper: rebuild the row stream ``session_stats`` folds.

``session_stats`` delegates its edit/intent enrichment to
:func:`ai_r.find_file_edits.find_file_edits` and its grouping/ranking to
:func:`ai_r.events.aggregate`.  The parity tests prove that delegation is
byte-identical by reconstructing the per-session row stream here and feeding it
to ``aggregate`` directly.

Single source of truth on purpose: this helper used to be copy-pasted into
``test_verbs`` and ``test_phase3b_parity``, and the copies drifted — commit
56eb87b taught only ONE copy to opt out of the ``find_file_edits`` size caps
and the host parity test on the other file silently diverged (edits 1295 vs
4212) until it was run on a real vault.  Keeping the reconstruction in one place
means a future flag change in ``session_stats`` can't split the two again.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ai_r.find_file_edits import find_file_edits
from ai_r.parsers import PARSERS, target_agents
from ai_r.session_stats import group_key


def session_rows(agent: Optional[str] = None) -> List[Dict[str, Any]]:
    """One row per session, carrying the fields ``session_stats`` rolls up.

    Mirrors ``session_stats``'s INTERNAL ``find_file_edits`` call byte-for-byte
    — ``redact=False`` and ``size_caps=False``.  Both matter on a big real
    vault: the 4 MB byte budget drops records and the intent cap truncates them
    (undercounting edits/intents), and a masked secret could merge two distinct
    raw intents (a distinct-intent drift).  A preset is a thin chain over the
    base method with the SAME arguments, so the reconstruction must pass them.
    """
    edits = find_file_edits(
        path="/", agent=agent, limit=0, redact=False, size_caps=False
    )
    by: Dict[str, Dict[str, Any]] = {}
    for r in edits["records"]:
        u = r.get("session_uuid")
        if not u:
            continue
        b = by.setdefault(u, {"edits": 0, "intents": set()})
        b["edits"] += 1
        it = r.get("intent")
        if isinstance(it, str) and it.strip():
            b["intents"].add(it.strip())

    rows: List[Dict[str, Any]] = []
    for agent_name in target_agents(agent):
        for s in PARSERS[agent_name].list_sessions():
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
