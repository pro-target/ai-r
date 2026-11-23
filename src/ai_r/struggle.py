"""Cross-agent ``struggle`` core: detect agents dying silently or in loops.

"Struggle" is what the operator sees from the outside — a session that
burned tokens and died anyway.  This module turns the taxonomy from the
struggle-detect spec (``tmp/struggle-detect/reports/report.md``) into
detectable signatures over the ZCode sqlite registry:

* **sd** (silent death) — a turn ended ``error`` with zero output tokens
  after at least one model request: the model was asked and produced
  nothing (spec case A, 483 turns / 45 days);
* **qd** (quota death) — an errored session whose tail carries the
  provider quota marker ``[1308][Usage limit reached …]`` in a *tool
  output* (spec case B).  The strict provider regex rejects the two
  confirmed false-positive sources: skill/docs quotes and the marker
  echoed by an agent grepping the database itself (an input, not an
  output).  ``loose_markers=True`` admits substring markers on tool
  outputs at the cost of those false positives.
* **ll** (overspending builder) — one turn with ``tool_call_count``
  above the corpus P95 preset (spec case C: 245+ call turns, all
  subagent builders, 4 of the top 10 died error);
* **br** (blind retry) — consecutive tool calls with the same name and
  canonically identical input, at least one of the pair errored (spec
  case D).  Canonicalization masks hex hashes and digit runs, so a
  retry that only changes a uuid or a counter still matches.  v1 cost
  bound: only errored/signalled sessions (or a pinned ``--session``)
  are scanned for pairs.

Every record carries the alert-ready shape ``{session_id, agent, kind,
severity, evidence, ts}`` (plus ``reset_at`` for qd) so the existing
TG-alerter can consume ``--json`` output directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

# Private intra-package import: the ZCode DB path-resolution and
# read-only-open helpers (override → $ZCODE_DB → default layout), shared
# with the zcode parser instead of being re-derived here.
from ai_r.parsers.zcode import _open_db, _resolve_db_paths

__all__ = [
    "PRESETS",
    "STRUGGLE_KINDS",
    "struggle",
]

#: Kinds v1 can detect (spec §3 ``--kinds``).
STRUGGLE_KINDS = ("sd", "qd", "br", "ll")

#: Thresholds and patterns from the spec presets — tune here, once.
PRESETS: dict[str, Any] = {
    # Provider quota-death contract (stable 2026-10 → 2026-11): group 1
    # captures the reset timestamp for alerts.
    "QD_STRICT_RE": re.compile(
        r"\[\s*1308\s*\]\[\s*Usage limit reached for \d+ hour\. "
        r"Your limit will reset at ([^]]+)\]\[[0-9a-f]{16,}\]"
    ),
    # Loose markers (``--loose-markers``): substring hits on tool
    # outputs only; documented to catch skill/docs quotes.
    "QD_LOOSE_MARKERS": ("usage limit", "quota", "rate limit", "billing"),
    # Spec case C: corpus P95 of per-turn tool_call_count.
    "LL_ABS": 245,
    # Spec case D: normalization masks hex hashes, then digit runs.
    "BR_HEX_RE": re.compile(r"[0-9a-f]{8,}"),
    "BR_DIGIT_RE": re.compile(r"\d+"),
}


def _iso(ms: int) -> str:
    """Epoch-milliseconds → ISO-8601 UTC (the corpus timezone)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _to_epoch_ms(value: datetime) -> int:
    """Datetime → epoch milliseconds; naive datetimes read as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _detect_sd(
    conn: sqlite3.Connection, where: str, params: list[Any]
) -> list[dict]:
    """Silent-death turns: error + ``output_tokens=0`` + ≥1 request."""
    rows = conn.execute(
        "SELECT t.session_id AS sid, t.turn_id AS tid, t.started_at AS ts, "
        "       t.model_request_count AS reqs, t.output_tokens AS out_tok, "
        "       t.error_code AS err, s.last_outcome AS outcome "
        "FROM turn_usage t JOIN session s ON s.id = t.session_id "
        f"WHERE t.status = 'error' AND t.output_tokens = 0 "
        f"AND t.model_request_count >= 1 {where} "
        "ORDER BY t.started_at DESC",
        params,
    ).fetchall()
    return [
        {
            "session_id": r["sid"],
            "agent": "zcode",
            "kind": "sd",
            "severity": "high" if r["outcome"] == "error" else "medium",
            "evidence": (
                f"turn {r['tid']} error output_tokens=0 "
                f"model_requests={r['reqs']} error_code={r['err']}"
            ),
            "ts": _iso(r["ts"]),
        }
        for r in rows
    ]


def _quota_hits(
    data: str, *, loose: bool
) -> Optional[tuple[str, Optional[str]]]:
    """Return ``(marker_text, reset_at)`` if a tool *output* matches.

    Only ``state.output`` is inspected: tool inputs and user text are
    the confirmed false-positive sources (agents grep the marker
    themselves; skill docs quote it).  Strict mode requires the full
    provider contract, loose mode accepts substring markers and is
    flagged ``low`` severity by the caller.
    """
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "tool":
        return None
    state = payload.get("state")
    if not isinstance(state, dict):
        return None
    output = state.get("output")
    if not isinstance(output, str):
        return None
    match = PRESETS["QD_STRICT_RE"].search(output)
    if match:
        return (match.group(0), match.group(1))
    if loose:
        lowered = output.lower()
        for marker in PRESETS["QD_LOOSE_MARKERS"]:
            if marker in lowered:
                return (marker, None)
    return None


def _error_sessions(
    conn: sqlite3.Connection, where: str, params: list[Any]
) -> list[str]:
    """Uuids of in-scope sessions having at least one errored turn."""
    return [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT t.session_id FROM turn_usage t "
            f"WHERE 1=1 {where} AND t.status = 'error' ORDER BY 1",
            params,
        )
    ]


def _detect_qd(
    conn: sqlite3.Connection, where: str, params: list[Any], *, loose: bool
) -> list[dict]:
    """Quota-death sessions: error turn + quota marker in a tool output.

    Candidates come from the errored turns already in scope (spec §2 B:
    never scan parts outside error sessions), the part rows are
    pre-filtered by a cheap ``LIKE`` in sqlite and only then matched by
    the strict regex in Python.
    """
    error_sessions = _error_sessions(conn, where, params)
    if not error_sessions:
        return []
    chunk_size = 500
    records: list[dict] = []
    for i in range(0, len(error_sessions), chunk_size):
        chunk = error_sessions[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT p.session_id AS sid, p.time_created AS ts, p.data AS data "
            "FROM part p "
            f"WHERE p.session_id IN ({placeholders}) "
            "AND (p.data LIKE '%usage limit%' OR p.data LIKE '%quota%' "
            "     OR p.data LIKE '%rate limit%' "
            "     OR p.data LIKE '%billing%') "
            "ORDER BY p.session_id, p.time_created DESC",
            chunk,
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            hit = _quota_hits(row["data"], loose=loose)
            if hit is None or row["sid"] in seen:
                continue
            seen.add(row["sid"])
            marker, reset_at = hit
            records.append(
                {
                    "session_id": row["sid"],
                    "agent": "zcode",
                    "kind": "qd",
                    "severity": "high" if reset_at else "low",
                    "evidence": f"quota marker in tool output: {marker}",
                    "ts": _iso(row["ts"]),
                    **({"reset_at": reset_at} if reset_at else {}),
                }
            )
    return records


def _detect_ll(
    conn: sqlite3.Connection, where: str, params: list[Any]
) -> list[dict]:
    """Overspending-builder turns: ``tool_call_count`` above the preset."""
    threshold = PRESETS["LL_ABS"]
    rows = conn.execute(
        "SELECT t.session_id AS sid, t.turn_id AS tid, t.started_at AS ts, "
        "       t.status AS status, t.tool_call_count AS tools, "
        "       s.task_type AS task "
        "FROM turn_usage t JOIN session s ON s.id = t.session_id "
        f"WHERE t.tool_call_count > {threshold} {where} "
        "ORDER BY t.tool_call_count DESC",
        params,
    ).fetchall()
    return [
        {
            "session_id": r["sid"],
            "agent": "zcode",
            "kind": "ll",
            "severity": "high" if r["status"] == "error" else "medium",
            "evidence": (
                f"turn {r['tid']} tool_calls={r['tools']} "
                f"status={r['status']} task={r['task']} "
                f"(threshold {threshold})"
            ),
            "ts": _iso(r["ts"]),
        }
        for r in rows
    ]


def _canonical_fingerprint(tool: str, state_input: Any) -> str:
    """MD5 of the call identity after masking volatile fragments.

    Spec case D: a blind retry often changes only a uuid or a counter,
    so hex runs of 8+ become ``H`` and digit runs become ``N`` before
    hashing the sorted-keys JSON of the input.
    """
    try:
        canon = json.dumps(state_input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        canon = str(state_input)
    canon = PRESETS["BR_HEX_RE"].sub("H", canon)
    canon = PRESETS["BR_DIGIT_RE"].sub("N", canon)
    return hashlib.md5(f"{tool}\x00{canon}".encode()).hexdigest()


def _detect_br(
    conn: sqlite3.Connection, candidates: Sequence[str]
) -> list[dict]:
    """Blind retries: consecutive same-tool calls with equal fingerprint.

    Scans the tool parts of ``candidates`` in corpus order (message
    creation time, then part sequence/id — the same ordering the zcode
    parser uses) and flags pairs where both calls agree on tool and
    canonical input and at least one of the two errored.
    """
    if not candidates:
        return []
    chunk_size = 500
    records: list[dict] = []
    for i in range(0, len(candidates), chunk_size):
        chunk = candidates[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT p.id AS pid, p.session_id AS sid, "
            "       p.time_created AS ts, p.data AS data "
            "FROM part p JOIN message m ON m.id = p.message_id "
            f"WHERE p.session_id IN ({placeholders}) "
            "ORDER BY p.session_id, m.time_created, m.id, p.time_created, "
            "COALESCE(p.sequence, 2147483647), p.id",
            chunk,
        ).fetchall()
        prev: Optional[dict] = None
        for row in rows:
            try:
                payload = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("type") != "tool":
                continue
            state = payload.get("state")
            if not isinstance(state, dict):
                continue
            tool = payload.get("tool")
            if not isinstance(tool, str):
                continue
            cur = {
                "sid": row["sid"],
                "pid": row["pid"],
                "ts": row["ts"],
                "tool": tool,
                "status": state.get("status"),
                "fingerprint": _canonical_fingerprint(
                    tool, state.get("input")
                ),
            }
            if (
                prev is not None
                and prev["sid"] == cur["sid"]
                and prev["tool"] == cur["tool"]
                and prev["fingerprint"] == cur["fingerprint"]
                and "error" in (prev["status"], cur["status"])
            ):
                records.append(
                    {
                        "session_id": cur["sid"],
                        "agent": "zcode",
                        "kind": "br",
                        "severity": "medium",
                        "evidence": (
                            f"{cur['tool']} repeated: {prev['pid']} "
                            f"({prev['status']}) -> {cur['pid']} "
                            f"({cur['status']}), canonical input match"
                        ),
                        "ts": _iso(cur["ts"]),
                    }
                )
                prev = None  # count each call once per retry chain
            else:
                prev = cur
    return records


def struggle(
    *,
    agent: str = "zcode",
    db: Optional[str] = None,
    session: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    kinds: Optional[Sequence[str]] = None,
    top: int = 20,
    loose_markers: bool = False,
) -> list[dict]:
    """Detect struggle signatures; return alert-ready records.

    ``db`` overrides the ZCode DB path (tests, offline copies);
    ``session`` pins one session uuid; ``since``/``until`` bound turn
    start times (naive datetimes read as UTC); ``kinds`` selects a
    subset of :data:`STRUGGLE_KINDS` (default: all); ``top`` caps the
    per-kind record count.  Raises :class:`ValueError` on an unknown
    kind or an agent without a v1 adapter.
    """
    if agent != "zcode":
        raise ValueError(
            f"agent '{agent}' has no struggle adapter yet; v1 covers "
            "'zcode' only"
        )
    selected = tuple(STRUGGLE_KINDS) if kinds is None else tuple(kinds)
    unknown = set(selected) - set(STRUGGLE_KINDS)
    if unknown:
        raise ValueError(
            f"unknown struggle kind(s): {', '.join(sorted(unknown))}; "
            f"expected any of {', '.join(STRUGGLE_KINDS)}"
        )

    if db is None:
        resolved = _resolve_db_paths()
        if not resolved:
            raise ValueError("no ZCode DB found; pass --db explicitly")
        db = resolved[0]
    conn = _open_db(db)
    if conn is None:
        raise ValueError(f"cannot open ZCode DB read-only: {db}")
    conn.row_factory = sqlite3.Row

    where = ""
    params: list[Any] = []
    if session is not None:
        where += " AND t.session_id = ?"
        params.append(session)
    if since is not None:
        where += " AND t.started_at >= ?"
        params.append(_to_epoch_ms(since))
    if until is not None:
        where += " AND t.started_at < ?"
        params.append(_to_epoch_ms(until))

    records: list[dict] = []
    try:
        if "sd" in selected:
            records.extend(_detect_sd(conn, where, params))
        if "qd" in selected:
            records.extend(_detect_qd(conn, where, params, loose=loose_markers))
        if "ll" in selected:
            records.extend(_detect_ll(conn, where, params))
        if "br" in selected:
            # v1 cost bound: pairs are scanned only in errored sessions,
            # sessions signalled by another selected kind, or the pinned
            # --session (spec §2 D; blind retries cluster in dying builds).
            candidates = set(
                _error_sessions(conn, where, params)
            )
            candidates.update(r["session_id"] for r in records)
            if session is not None:
                candidates.add(session)
            records.extend(_detect_br(conn, sorted(candidates)))
    except sqlite3.Error as exc:  # schema drift / torn DB: fail loud
        raise ValueError(f"ZCode DB query failed: {exc}") from exc
    finally:
        conn.close()

    records.sort(key=lambda r: r["ts"], reverse=True)
    if top:
        kept: list[dict] = []
        counts: dict[str, int] = {}
        for rec in records:
            used = counts.get(rec["kind"], 0)
            if used >= top:
                continue
            counts[rec["kind"]] = used + 1
            kept.append(rec)
        records = kept
    return records
