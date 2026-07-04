"""Phase-3a verb: ``aggregate`` — a pure rollup over already-materialized rows.

``aggregate`` is the generic rollup that reproduces what ``session_stats``
(group_by ∈ agent|dir|date|kind) and ``file_frequency`` (group_by=file, rank
by edit count) do today, without re-parsing.  It is *pure*: it never touches
the filesystem — it folds a list of already-materialized row dicts (the
output of ``query``, ``find_file_edits``, or a session inventory) into
``{groups: [...], totals: {...}}``.  All behaviour is parameters:

* ``group_by`` selects the bucket key (a row field name, or a callable
  ``row -> str``).
* ``metrics`` selects which numbers each bucket carries.  Each metric name
  maps to a reducer over the bucket's rows; unknown names raise ValueError.

The metric reducers are deliberately the SAME semantics the legacy tools
use so Phase 3b can retarget them onto this verb with byte-identical output:

* ``count``    — number of rows in the bucket.
* ``sessions`` — distinct ``session_uuid`` | ``session_id`` (falls back to
  ``count`` of rows when a row carries a pre-counted ``sessions`` int, so a
  session-inventory row = one session).
* ``edits``    — SUM of each row's ``edits`` int when present, else the
  number of rows (an edit-record stream = one edit per row).  This is the
  union of the ``session_stats`` (pre-summed per session) and
  ``file_frequency`` (one row per edit) conventions.
* ``intents``  — distinct count of ``intent`` (str) and/or the union of each
  row's ``intents`` (iterable of str), stripped, empties skipped.
* ``agents``   — sorted distinct ``agent`` values.
* ``messages`` — SUM of each row's ``messages`` | ``message_count`` int.
* ``files``    — distinct count of ``file``.
* ``tokens``   — fold of per-row ``tokens`` blocks (F3.3): sums the
  normalized usage sub-fields and counts row provenance
  (``exact``/``estimated``/``unknown``) — see :func:`_metric_tokens`.

``totals`` carries the same metrics folded over the WHOLE row set (never the
truncated ``groups``), plus ``sessions``/``agents``/``agents_list`` mirrors
so the shape lines up with both legacy tools' ``totals`` blocks.

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

from collections import OrderedDict as _OrderedDict
from typing import (
    Any,
    List,
    Optional,
    OrderedDict as OrderedDictType,
    Sequence,
    Tuple,
)


def _row_group_key(row: dict[str, Any], group_by: Any) -> str:
    """Resolve a row's bucket label under ``group_by`` (field name or callable)."""
    if callable(group_by):
        return str(group_by(row))
    val = row.get(group_by)
    if val is None or (isinstance(val, str) and not val):
        return "(unknown)"
    return str(val)


def _metric_sessions(rows: Sequence[dict[str, Any]]) -> int:
    seen: set[str] = set()
    counted = 0
    for r in rows:
        uuid = r.get("session_uuid") or r.get("session_id")
        if isinstance(uuid, str) and uuid:
            seen.add(uuid)
        elif isinstance(r.get("sessions"), int):
            counted += int(r["sessions"])
        else:
            counted += 1
    return len(seen) if seen else counted


def _metric_edits(rows: Sequence[dict[str, Any]]) -> int:
    total = 0
    for r in rows:
        val = r.get("edits")
        if isinstance(val, bool):
            total += 1
        elif isinstance(val, int):
            total += val
        else:
            total += 1
    return total


def _collect_intents(rows: Sequence[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        single = r.get("intent")
        if isinstance(single, str) and single.strip():
            out.add(single.strip())
        many = r.get("intents")
        if isinstance(many, (list, tuple, set)):
            for it in many:
                if isinstance(it, str) and it.strip():
                    out.add(it.strip())
    return out


def _collect_agents(rows: Sequence[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        agent = r.get("agent")
        if isinstance(agent, str) and agent:
            out.add(agent)
        many = r.get("agents")
        if isinstance(many, (list, tuple, set)):
            for a in many:
                if isinstance(a, str) and a:
                    out.add(a)
    return out


def _metric_messages(rows: Sequence[dict[str, Any]]) -> int:
    total = 0
    for r in rows:
        val = r.get("messages")
        if val is None:
            val = r.get("message_count")
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            total += val
    return total


def _metric_files(rows: Sequence[dict[str, Any]]) -> int:
    seen: set[str] = set()
    for r in rows:
        f = r.get("file")
        if isinstance(f, str) and f:
            seen.add(f)
    return len(seen)


# Summable sub-fields of a row's ``tokens`` block (the normalized shape of
# :func:`ai_r.tokens.session_tokens`).
_TOKEN_SUM_FIELDS: tuple[str, ...] = (
    "input", "output", "reasoning", "cache_read", "cache_write", "total",
)


def _metric_tokens(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-row ``tokens`` blocks into one bucket summary (F3.3).

    Each row may carry ``tokens`` as the normalized dict produced by
    :func:`ai_r.tokens.session_tokens` (``{input, output, reasoning,
    cache_read, cache_write, total, source, [estimator]}``) or, as a
    convenience, a bare ``int`` total.  The reducer sums every ``int``
    sub-field over the rows that have one (a field no row carries stays
    ``None`` — never a fabricated ``0``) and keeps the provenance honest
    with three per-row counters:

    * ``exact``     — rows whose block says ``source == "exact"``;
    * ``estimated`` — rows whose block says ``source == "estimate"``;
    * ``unknown``   — rows with no usable total OR a total of unknown
      provenance (bare int / missing ``source``).

    Invariant: ``exact + estimated + unknown == len(rows)``.  Exact and
    estimated totals are summed together — the counters exist precisely so
    a reader can see how much of the sum is estimation.
    """
    sums: dict[str, Optional[int]] = {f: None for f in _TOKEN_SUM_FIELDS}
    exact = estimated = unknown = 0
    for r in rows:
        block = r.get("tokens")
        if isinstance(block, bool):
            block = None
        if isinstance(block, int):
            block = {"total": block}
        if not isinstance(block, dict) or not isinstance(block.get("total"), int) \
                or isinstance(block.get("total"), bool):
            unknown += 1
            continue
        source = block.get("source")
        if source == "exact":
            exact += 1
        elif source == "estimate":
            estimated += 1
        else:
            unknown += 1
        for field in _TOKEN_SUM_FIELDS:
            val = block.get(field)
            if isinstance(val, int) and not isinstance(val, bool):
                sums[field] = (sums[field] or 0) + val
    return {**sums, "exact": exact, "estimated": estimated, "unknown": unknown}


# Metric name → (reducer, kind).  ``kind`` shapes the emitted value:
# ``"int"`` scalar, ``"list"`` sorted-distinct-list.
_METRICS: "dict[str, tuple[Any, str]]" = {
    "count": (lambda rows: len(rows), "int"),
    "sessions": (_metric_sessions, "int"),
    "edits": (_metric_edits, "int"),
    "intents": (lambda rows: len(_collect_intents(rows)), "int"),
    "agents": (lambda rows: sorted(_collect_agents(rows)), "list"),
    "messages": (_metric_messages, "int"),
    "files": (_metric_files, "int"),
    "tokens": (_metric_tokens, "dict"),
}


# Note text reused VERBATIM from ``session_stats`` (RISK-4) so a
# ``kind_split=True`` aggregate reproduces its degenerate-split note byte-for-byte.
_KIND_SPLIT_NOTE: str = (
    "kind split is degenerate: no subagent sessions were in scope, so a "
    "group_by='kind' result shows only an 'agent' bucket. This is NOT a "
    "verified 'no subagents' — subagent detection is currently "
    "Claude-only; other agents always report kind='agent'."
)


def aggregate(
    rows: Sequence[dict[str, Any]],
    *,
    group_by: Any,
    metrics: Sequence[str] = ("count",),
    rank_by: str = "default",
    kind_split: bool = False,
) -> dict[str, Any]:
    """Roll a list of row dicts up by ``group_by`` — the generic stats verb.

    Reproduces ``session_stats`` (``group_by`` ∈ ``agent``/``dir``/``date``/
    ``kind`` over a session-inventory row stream) and ``file_frequency``
    (``group_by="file"`` over a ``find_file_edits`` record stream) without
    re-parsing — it is a pure fold over already-materialized rows.

    Args:
        rows: The row dicts to fold (``query`` output, ``find_file_edits``
            records, or a session inventory).
        group_by: The bucket key — a row field name (str) or a callable
            ``row -> str``.  Missing/empty values bucket under ``"(unknown)"``.
        metrics: Which numbers each bucket carries.  One or more of
            ``count`` / ``sessions`` / ``edits`` / ``intents`` / ``agents`` /
            ``messages`` / ``files`` / ``tokens`` (see the module-level
            table).  Unknown names raise :class:`ValueError`.
        rank_by: Group ordering.  ``"default"`` (edits desc, sessions desc,
            count desc, label asc — the ``file_frequency`` order) or
            ``"stats"`` (sessions desc, edits desc, label asc — the
            ``session_stats`` order).  The two differ whenever a
            more-sessions bucket has fewer edits than a fewer-sessions bucket,
            which is why ``session_stats`` needs its own rank to delegate.
        kind_split: When ``True``, add the ``session_stats`` RISK-4 fields —
            ``kind_split_available`` (``True`` iff any row's ``kind`` is
            ``"subagent"``) and, when ``False``, a human-readable ``note``
            (verbatim from ``session_stats``) so a degenerate kind split is
            never misread as a verified "no subagents".

    Returns:
        ``{"group_by": <label>, "groups": [{"group", <metrics...>}],
        "totals": {<metrics...>, "sessions", "agents", "agents_list"}}``,
        plus ``kind_split_available`` / ``note`` when ``kind_split=True``.
        ``totals`` fold over the WHOLE row set (never a truncated group list).

    Raises:
        ValueError: on an unknown metric name or ``rank_by`` value.
    """
    if rank_by not in ("default", "stats"):
        raise ValueError(
            f"rank_by must be 'default' or 'stats', got {rank_by!r}"
        )
    metric_list = list(metrics) if metrics else ["count"]
    for name in metric_list:
        if name not in _METRICS:
            raise ValueError(
                f"unknown metric {name!r}; expected one of {sorted(_METRICS)}"
            )

    buckets: "OrderedDictType[str, List[dict[str, Any]]]" = _OrderedDict()
    for row in rows:
        key = _row_group_key(row, group_by)
        buckets.setdefault(key, []).append(row)

    def _row_metrics(bucket_rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in metric_list:
            reducer, _kind = _METRICS[name]
            out[name] = reducer(bucket_rows)
        return out

    group_rows: List[dict[str, Any]] = []
    for label, bucket_rows in buckets.items():
        entry: dict[str, Any] = {"group": label}
        entry.update(_row_metrics(bucket_rows))
        group_rows.append(entry)

    # Rank.  ``default`` = edits desc, sessions desc, count desc, label asc
    # (the ``file_frequency`` order).  ``stats`` = sessions desc, edits desc,
    # label asc (the ``session_stats`` order) — these disagree whenever a
    # more-sessions bucket has fewer edits.
    def _default_rank(g: dict[str, Any]) -> Tuple[int, int, int, str]:
        edits = g.get("edits", 0) if isinstance(g.get("edits"), int) else 0
        sessions = g.get("sessions", 0) if isinstance(g.get("sessions"), int) else 0
        count = g.get("count", 0) if isinstance(g.get("count"), int) else 0
        return (-edits, -sessions, -count, g["group"])

    def _stats_rank(g: dict[str, Any]) -> Tuple[int, int, str]:
        sessions = g.get("sessions", 0) if isinstance(g.get("sessions"), int) else 0
        edits = g.get("edits", 0) if isinstance(g.get("edits"), int) else 0
        return (-sessions, -edits, g["group"])

    group_rows.sort(key=_stats_rank if rank_by == "stats" else _default_rank)

    totals: dict[str, Any] = {}
    for name in metric_list:
        reducer, _kind = _METRICS[name]
        totals[name] = reducer(rows)
    # Always surface the session/agent totals the legacy ``totals`` blocks
    # carry, even when not requested as a group metric.
    if "sessions" not in totals:
        totals["sessions"] = _metric_sessions(rows)
    agents_all = sorted(_collect_agents(rows))
    totals["agents"] = len(agents_all)
    totals["agents_list"] = agents_all

    label = group_by if isinstance(group_by, str) else "custom"
    result: dict[str, Any] = {
        "group_by": label, "groups": group_rows, "totals": totals,
    }
    if kind_split:
        # RISK-4: honesty flag + degenerate-split note, matching session_stats.
        subagent_seen = any(
            isinstance(r.get("kind"), str) and r["kind"] == "subagent"
            for r in rows
        )
        result["kind_split_available"] = subagent_seen
        if not subagent_seen:
            result["note"] = _KIND_SPLIT_NOTE
    return result
