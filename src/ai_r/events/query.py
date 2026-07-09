"""The ``query`` workhorse + the ``intent`` / ``reaction`` presets.

``query`` filters/searches the normalized :class:`~ai_r.events._common.Event`
stream; ``intent`` / ``reaction`` are thin wrappers over its ``relative_to``
walk.  ``text`` + ``sort="relevance"`` re-uses the exact BM25 scorer that backs
``search_sessions`` (:mod:`ai_r.ranking`) — no algorithm is duplicated here.

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change.
"""

from __future__ import annotations

import warnings
from typing import Any, List, Optional, Sequence

from ai_r.find_file_edits import parse_iso_bound, previous_user_intent
from ai_r.parsers import PARSERS, Message, target_agents
from ai_r.ranking import bm25_scores as _bm25_scores, tokenize as _tokenize
from ai_r.redact import merge_redaction_counts, redact_text
from ai_r.semantic import semantic_order as _semantic_order

from ai_r.parsers._noise import validate_noise

from ai_r.events._common import Event, TOOL_KIND, TOOL_SUBTYPE
from ai_r.events.model import iter_events, normalize_session_filter


# --- query facets ----------------------------------------------------------

# The wrapper-aware tool kinds (F3.1) that are NOT event-``type`` subtypes: a
# Task/Skill/MCP/web call keeps ``type="tool_call(other)"`` (or its base
# subtype) and carries the real kind in ``refs.tool_kind`` — so a
# ``type="tool_call(task)"`` facet cannot match by ``event.type`` alone and is
# instead translated into a ``tool_kind`` filter (see ``_split_type_facet``).
_TYPE_ONLY_KINDS: frozenset[str] = TOOL_KIND - TOOL_SUBTYPE


def _split_type_facet(wanted: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split a ``type`` facet into ``(type_needle, kind_from_type)``.

    ``type="tool_call(<k>)"`` where ``<k>`` is a wrapper-aware kind that is
    NOT an event-``type`` subtype (``task``/``skill``/``mcp``/``web`` — see
    :data:`_TYPE_ONLY_KINDS`) resolves to ``("tool_call", "<k>")``: the base
    ``tool_call`` type gate plus a ``tool_kind`` filter, since those calls are
    recorded as ``tool_call(other)`` with the real kind in ``refs`` (a
    ``tool_call(task)`` filter that matched only ``event.type`` would find
    nothing — the F3.1 asymmetry).  Every other value (``tool_call(edit)``,
    bare ``tool_call``, ``user_turn`` …) passes through as
    ``(wanted, None)``.
    """
    if wanted is None:
        return None, None
    if wanted.startswith("tool_call(") and wanted.endswith(")"):
        sub = wanted[len("tool_call("):-1]
        if sub in _TYPE_ONLY_KINDS:
            return "tool_call", sub
    return wanted, None


def _type_matches(event_type: str, wanted: str) -> bool:
    """Match an event ``type`` against a ``type`` facet value.

    * ``"tool_call"`` matches every ``tool_call(<sub>)`` event.
    * ``"tool_call(edit)"`` matches only that subtype.
    * ``"user_turn"`` / ``"assistant_turn"`` / ``"plan_event"`` match exactly.

    Wrapper-aware kinds (``tool_call(task)`` etc.) are NOT handled here —
    they are translated to a base ``tool_call`` gate + ``tool_kind`` filter
    by :func:`_split_type_facet` before this is called.
    """
    if event_type == wanted:
        return True
    if wanted == "tool_call" and event_type.startswith("tool_call("):
        return True
    return False


def _ref_value(refs: Sequence[dict], key: str) -> Optional[Any]:
    """Return the first ``refs[*][key]`` value, or ``None``."""
    for r in refs:
        if key in r:
            return r[key]
    return None


def _event_to_dict(event: Event) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": event.id,
        "session_id": event.session_id,
        "agent": event.agent,
        "ts": event.ts,
        "type": event.type,
        "text": event.text,
        "refs": [dict(r) for r in event.refs],
        "source": event.source,
        "sha256": event.sha256,
        "message_index": event.message_index,
    }
    # F3.1: hoist the wrapper classification out of refs so downstream
    # ``aggregate(group_by="tool_kind")`` works on query rows directly.
    # Only tool_call events carry these; other event types are unchanged.
    kind = _ref_value(event.refs, "tool_kind")
    if kind is not None:
        out["tool_kind"] = kind
    resolved = _ref_value(event.refs, "tool_resolved")
    if resolved is not None:
        out["tool_resolved"] = resolved
    # Model dimension: emitted only when the format recorded one (mirrors
    # the tool_kind hoisting — no-signal events keep the base shape), so
    # ``aggregate(group_by="model")`` works on query rows directly and a
    # row without a signal folds into the honest "(unknown)" bucket.
    if event.model is not None:
        out["model"] = event.model
    return out


def _attach_intents(event_dicts: List[dict[str, Any]]) -> None:
    """Attach a top-level ``intent`` to each event dict, in place.

    The intent of an event is the request behind it: the previous user turn's
    text, resolved by the SAME :func:`previous_user_intent` walk-back the
    legacy tools (``find_file_edits`` / ``find_tool_calls`` / ``session_diff``)
    use — over the FULL raw message list, indexed by the event's stored
    ``message_index``.  Reusing that exact helper is what makes the enriched
    ``query`` output byte-identical to the legacy ``intent`` attribution.

    Sessions are read once and cached across the batch so enrichment is
    O(sessions) not O(events).  An event whose session/message cannot be
    resolved gets ``intent=None`` (the same default the legacy tools emit when
    no preceding user turn exists).
    """
    msgs_cache: dict[str, Sequence[Any]] = {}

    def _messages_for(session_id: str, agent: str) -> Optional[Sequence[Any]]:
        if session_id in msgs_cache:
            return msgs_cache[session_id]
        for agent_name in target_agents(agent or None):
            parser = PARSERS[agent_name]
            for sess in parser.list_sessions():
                if sess.uuid != session_id:
                    continue
                messages: list[Message] = []
                try:
                    messages = parser.read_messages(sess.uuid)
                except (FileNotFoundError, ValueError, OSError):
                    messages = []
                msgs_cache[session_id] = messages
                return messages
        msgs_cache[session_id] = ()
        return ()

    for ev in event_dicts:
        session_id = ev.get("session_id") or ""
        agent = ev.get("agent") or ""
        idx = ev.get("message_index", -1)
        messages = _messages_for(session_id, agent)
        if messages and isinstance(idx, int) and 0 <= idx < len(messages):
            ev["intent"] = previous_user_intent(messages, idx)
        else:
            ev["intent"] = None


def _redact_events(
    event_dicts: List[dict[str, Any]],
    redactions_out: Optional[dict[str, int]],
) -> None:
    """Mask secrets in the emitted ``text``/``intent`` fields, in place (F2.1).

    Runs at the END of ``query`` — after filtering, sorting and the limit
    slice — so only emitted events pay for it and every filter above
    matched the RAW stored text.  Per-type replacement counts are folded
    into ``redactions_out`` when the caller provided one.
    """
    for ev in event_dicts:
        for field in ("text", "intent", "tool_resolved"):
            val = ev.get(field)
            if not isinstance(val, str) or not val:
                continue
            new_val, counts = redact_text(val)
            if counts:
                ev[field] = new_val
                if redactions_out is not None:
                    merge_redaction_counts(redactions_out, counts)
                # Keep the refs mirror consistent with the hoisted field.
                if field == "tool_resolved":
                    for ref in ev.get("refs") or ():
                        if isinstance(ref, dict) and "tool_resolved" in ref:
                            ref["tool_resolved"] = new_val


def _walk_relative(
    events: Sequence[Event],
    anchor_id: str,
    direction: str,
    n_all: bool,
    n: int,
    *,
    step_type: str = "user_turn",
) -> List[Event]:
    """Timeline walk from ``anchor_id`` in ``direction`` collecting turns.

    Generalises :func:`ai_r.find_file_edits.previous_user_intent`:
    ``direction="prev"`` walks backwards (the historical ``intent``
    behaviour), ``"next"`` walks forwards (its mirror).  Collects up to
    ``n`` events of ``step_type`` (default ``user_turn``); ``n_all``
    collects every match in that direction.  Returns them in timeline
    order (ascending index), regardless of walk direction.
    """
    pos = next(
        (i for i, ev in enumerate(events) if ev.id == anchor_id), None
    )
    if pos is None:
        return []
    step = -1 if direction == "prev" else 1
    out: List[Event] = []
    j = pos + step
    while 0 <= j < len(events):
        ev = events[j]
        if _type_matches(ev.type, step_type):
            out.append(ev)
            if not n_all and len(out) >= n:
                break
        j += step
    out.sort(key=lambda e: int(e.id.rsplit(":", 1)[-1]))
    return out


def query(
    *,
    type: Optional[str] = None,
    agent: Optional[str] = None,
    session: Optional[Any] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    file: Optional[str] = None,
    tool: Optional[str] = None,
    tool_kind: Optional[str] = None,
    model: Optional[str] = None,
    text: Optional[str] = None,
    sort: str = "date",
    relative_to: Optional[str] = None,
    direction: str = "prev",
    n: Any = 1,
    step_type: str = "user_turn",
    limit: int = 0,
    with_intent: bool = False,
    noise: str = "include",
    project_dir: Optional[str] = None,
    parent: Optional[str] = None,
    group: Optional[str] = None,
    scanned_sessions_out: Optional[dict[str, Any]] = None,
    redact: bool = True,
    redactions_out: Optional[dict[str, int]] = None,
    semantic_out: Optional[dict[str, Any]] = None,
) -> List[dict[str, Any]]:
    """Filter/search the normalized Event stream — the Phase-1 workhorse.

    Facets (all optional, all parameters — no hard-wired variants):

    * ``type``  — ``user_turn`` | ``assistant_turn`` | ``tool_call`` |
      ``tool_call(<sub>)`` | ``plan_event``.  Bare ``tool_call`` matches
      every subtype.
    * ``agent`` — restrict to one agent (``claude``/``codex``/...).
    * ``session`` — restrict to one session uuid, OR a list of uuids
      (F3.2): the union of those sessions' events, one scan.  Duplicates
      collapse; a uuid absent from the corpus contributes nothing (same
      honest semantics as the single-uuid miss).  Fail-loud validation
      (SSOT :func:`~ai_r.events.model.normalize_session_filter`): an
      empty list or a non-string item raises :class:`ValueError` —
      never a silent unfiltered scan.
    * ``since`` / ``until`` — ISO-8601 bounds (inclusive) on ``ts``.
    * ``file`` — substring matched against any ``refs[*].file``.
    * ``tool`` — substring (pattern) matched against any ``refs[*].tool``
      OR ``refs[*].tool_resolved`` (case-insensitive) — so ``tool="commit"``
      also finds the Skill-wrapped call whose resolved skill is ``commit``.
    * ``tool_kind`` — exact match against the F3.1 wrapper-aware
      classification (one of :data:`~ai_r.events._common.TOOL_KIND`:
      ``edit``/``write``/``read``/``bash``/``task``/``skill``/``mcp``/
      ``web``/``other``).  Unknown values raise :class:`ValueError`
      (fail-loud, never a silent no-op).
    * ``model`` — exact, case-insensitive match against the event's
      inherited ``model`` (the model that produced the hosting message —
      see :class:`~ai_r.events._common.Event`).  There is no fixed model
      vocabulary (ids are agent-defined strings), so any non-empty string
      is accepted; events without a model signal never match.  An
      empty-string value is a fail-loud :class:`ValueError`.
    * ``text`` — substring matched against event ``text``
      (case-insensitive) AND, for a ``tool_call``, its FULL input body —
      so a pattern buried inside a multi-line command (an ``rm`` in a
      ``for … do rm …; done`` loop) is found even though a tool_call's
      ``text`` holds only the raw tool name.  With ``sort="relevance"``
      the survivors are BM25-ranked using the **same scorer** as
      ``search_sessions`` (ranking is over the turn ``text``, unchanged).
    * ``sort`` — ``"date"`` (default, ts-ascending), ``"relevance"``
      (BM25 over ``text``; requires a ``text`` facet, else falls back to
      date order), or ``"semantic"`` (F5.1: the BM25 top-50 candidates
      re-ranked by a local multilingual embedding model — see
      :mod:`ai_r.semantic`; without the optional ``ai-r[semantic]``
      dependencies/model it honestly degrades to the plain BM25 order,
      never a crash; the outcome lands in ``semantic_out``).
    * ``relative_to`` + ``direction`` (``prev``|``next``) + ``n``
      (``1`` | ``"all"``) — the neighbouring-turn walk.  Generalises
      ``previous_user_intent`` (prev/1) to both directions and any count.
      ``step_type`` selects which event type to collect (default
      ``user_turn``).  When ``relative_to`` is set, other filter facets
      are ignored (the anchor + walk fully specify the result).
    * ``with_intent`` — when ``True``, attach a top-level ``intent`` to each
      returned event: the request behind it (previous user turn), resolved by
      the SAME :func:`previous_user_intent` walk-back the legacy tools use.
      Default ``False`` so the base event shape is unchanged.  This is what
      lets ``diff`` / the ``find_file_edits`` preset reproduce the legacy
      ``intent`` field byte-for-byte.
    * ``noise`` — session-level noise filter (SSOT:
      :mod:`ai_r.parsers._noise`; noise == spawned subagent session):
      ``"include"`` (default, no filtering), ``"exclude"`` (top-level
      sessions only), ``"only"`` (subagent sessions only).  Applied at the
      session level, before messages are read.  Ignored on the
      ``relative_to`` walk (the anchor pins one concrete session), like
      every other filter facet.
    * ``project_dir`` — session-level project filter: keep only events of
      sessions whose ``Session.project_dir`` equals this path or is a
      **descendant** of it (path-boundary aware, trailing slashes
      ignored; semantics SSOT:
      :func:`ai_r.parsers._common.project_dir_matches`).  Sessions
      without a ``project_dir`` signal never match.  Like ``noise``,
      applied before any message is read, and ignored on the
      ``relative_to`` walk.
    * ``parent`` — session-level subtree filter: keep events of every
      session that is a **descendant** (transitively, any depth) of this
      session uuid in the ``parent_uuid`` tree — the whole spawned-subagent
      subtree below ``parent`` (direct children plus nested).  ``parent``
      itself is excluded (its own events are reachable via
      ``session=<parent>``).  Closure SSOT:
      :func:`ai_r.events.model._descendant_uuids` (built per-agent —
      ``parent_uuid`` never crosses agents).  An unknown uuid matches
      nothing (honest empty result, never an error); an empty-string value
      is a fail-loud :class:`ValueError`.  Like ``noise``, applied before
      any message is read, and ignored on the ``relative_to`` walk.
    * ``group`` — event-level ``plan_event`` filter: keep only the
      plan_events whose ``task_id`` equals this value (the plan-task
      grouping key computed by the SSOT :func:`_assign_plan_kinds` — the
      plan-file slug when the agent has one, else the normalized title).
      NON-plan_event events never match when ``group`` is set (it is a
      plan-only facet), so combining ``group`` with a non-plan ``type`` is
      an honest empty result.  Applied AFTER the events are collected.  An
      empty-string value is a fail-loud :class:`ValueError`.

    ``scanned_sessions_out`` is a caller-owned out-dict forwarded to
    :func:`iter_events` — it collects the per-agent ``list_sessions()``
    results of the scan so the MCP wrapper can build empty-result
    diagnostics WITHOUT a second corpus walk.

    ``redact`` (default ``True``) masks secrets in the emitted ``text`` /
    ``intent`` fields as ``[REDACTED_<TYPE>]`` (see :mod:`ai_r.redact`);
    ``redactions_out`` is a caller-owned dict that collects the per-type
    replacement counts.  Redaction is emission-time only: the ``text``
    facet (and every other filter) matches the RAW stored text.

    ``semantic_out`` is a caller-owned out-dict filled only when
    ``sort="semantic"`` was requested with a ``text`` facet: either the
    active-ranking report (``active``/``model``/``candidates``/``weight``)
    or the honest degradation notice (``active: false`` + plain-words
    ``reason`` + ``fallback: "bm25"``).

    Returns a list of event dicts (see :func:`_event_to_dict`).  Invalid
    arguments raise :class:`ValueError` (the MCP wrapper converts these
    to the error-dict convention).
    """
    if direction not in ("prev", "next"):
        raise ValueError(
            f"direction must be 'prev' or 'next', got {direction!r}"
        )
    sort_lc = (sort or "date").lower()
    if sort_lc not in ("date", "relevance", "semantic"):
        raise ValueError(
            f"sort must be 'relevance', 'date' or 'semantic', got {sort!r}"
        )
    validate_noise(noise)
    if tool_kind is not None:
        if not isinstance(tool_kind, str) or tool_kind not in TOOL_KIND:
            raise ValueError(
                f"tool_kind must be one of {sorted(TOOL_KIND)}, "
                f"got {tool_kind!r}"
            )
    if model is not None and (
        not isinstance(model, str) or not model.strip()
    ):
        raise ValueError(
            f"model must be a non-empty model-id string or None, got {model!r}"
        )
    if project_dir is not None and (
        not isinstance(project_dir, str) or not project_dir.strip()
    ):
        raise ValueError(
            "project_dir must be a non-empty path string or None, "
            f"got {project_dir!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")
    # Symmetric with the other record-capping tools (network/incidents/
    # find_file_edits): ``limit`` is a non-negative record cap (``0`` = no
    # cap).  Fail loud on negatives instead of the silent ``survivors[:-1]``
    # slice that would drop the newest event.
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(
            f"limit must be a non-negative integer, got {limit!r}"
        )
    # F3.2: validate the session facet up front (single uuid or a list of
    # uuids), so a malformed value fails loud even on the ``relative_to``
    # walk — where, like every other facet, it may otherwise go unused.
    normalize_session_filter(session)
    # Normalize ``n``: accepts 1/all (or any positive int / "all").
    n_all = False
    n_int = 1
    if isinstance(n, str):
        if n.strip().lower() == "all":
            n_all = True
        else:
            try:
                n_int = int(n)
            except ValueError as exc:
                raise ValueError(
                    f"n must be a positive integer or 'all', got {n!r}"
                ) from exc
            # A numeric string ("3") still walks, but the surface is moving to
            # the clean ``int | Literal["all"]`` shape: warn now, reject in
            # 0.6.0. The int arm and "all" sentinel are unaffected.
            warnings.warn(
                'n as a numeric string is deprecated; pass an int '
                '(or "all"); string ints will be rejected in 0.6.0',
                DeprecationWarning,
                stacklevel=2,
            )
    elif isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"n must be a positive integer or 'all', got {n!r}")
    else:
        n_int = n
    if not n_all and n_int < 1:
        raise ValueError(f"n must be >= 1 or 'all', got {n!r}")

    # ``parent`` (session-subtree) and ``group`` (plan-task) facets: if given
    # they must be non-empty strings (an empty string is a caller mistake, not
    # "match nothing" — fail loud, consistent with the other facets).
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ValueError(
            f"parent must be a non-empty session-uuid string or None, "
            f"got {parent!r}"
        )
    if group is not None and (not isinstance(group, str) or not group.strip()):
        raise ValueError(
            f"group must be a non-empty task-id string or None, got {group!r}"
        )

    # --- relative_to walk: needs a single, contiguous, ordered stream ----
    if relative_to is not None:
        # The anchor id is ``"{session}:{seq}"`` — scope the scan to that
        # session so the walk is over one contiguous timeline.
        anchor_session = relative_to.rsplit(":", 1)[0] if ":" in relative_to else session
        stream = list(iter_events(
            agent,
            session=anchor_session or session,
            scanned_sessions_out=scanned_sessions_out,
        ))
        walked = _walk_relative(
            stream, relative_to, direction, n_all, n_int, step_type=step_type
        )
        out = [_event_to_dict(ev) for ev in walked]
        if with_intent:
            _attach_intents(out)
        if redact:
            _redact_events(out, redactions_out)
        return out

    # --- ordinary facet filter ------------------------------------------
    since_dt = parse_iso_bound(since, "since")
    until_dt = parse_iso_bound(until, "until")
    file_needle = file if file else None
    tool_needle = tool.lower() if tool else None
    model_needle = model.strip().lower() if model else None
    text_needle = text.lower() if text else None

    # ``type="tool_call(task|skill|mcp|web)"`` is a wrapper-aware kind, not an
    # event-``type`` subtype: split it into the base ``tool_call`` type gate
    # plus a ``tool_kind`` filter (the F3.1 asymmetry — those calls are
    # recorded as ``tool_call(other)`` with the kind in refs).  A plain
    # subtype / bare type passes through unchanged.
    type_needle, kind_from_type = _split_type_facet(type)
    # Combine with an explicit ``tool_kind`` facet: both must hold (AND).  A
    # contradiction (``type="tool_call(task)"`` + ``tool_kind="web"``) is an
    # honest empty result, never a silent override.
    if kind_from_type is not None and tool_kind is not None and (
        tool_kind != kind_from_type
    ):
        return []
    kind_needle = tool_kind if tool_kind is not None else kind_from_type

    # ``group`` is a plan-only facet: pre-restrict the type gate to
    # plan_event so a non-plan event never survives (and it composes with an
    # explicit ``type`` — a contradictory ``type != plan_event`` then yields an
    # honest empty result).
    if group is not None and type_needle is not None and not _type_matches(
        "plan_event", type_needle
    ):
        return []

    survivors: List[Event] = []
    score_texts: List[str] = []
    for ev in iter_events(
        agent,
        session=session,
        noise=noise,
        project_dir=project_dir,
        parent=parent,
        scanned_sessions_out=scanned_sessions_out,
    ):
        if group is not None and ev.type != "plan_event":
            continue
        if type_needle is not None and not _type_matches(ev.type, type_needle):
            continue
        if since_dt is not None or until_dt is not None:
            ev_dt = parse_iso_bound(ev.ts, "ts") if ev.ts else None
            if since_dt is not None and (ev_dt is None or ev_dt < since_dt):
                continue
            if until_dt is not None and (ev_dt is None or ev_dt > until_dt):
                continue
        if file_needle is not None:
            files = [r.get("file", "") for r in ev.refs if "file" in r]
            if not any(file_needle in f for f in files):
                continue
        if tool_needle is not None:
            # Match the raw name AND the resolved name under a wrapper, so
            # a Skill/Task/MCP call is findable by what it really ran.
            tools = [
                str(r[key]).lower()
                for r in ev.refs
                for key in ("tool", "tool_resolved")
                if key in r
            ]
            if not any(tool_needle in t for t in tools):
                continue
        if kind_needle is not None:
            kinds = [r.get("tool_kind") for r in ev.refs if "tool_kind" in r]
            if kind_needle not in kinds:
                continue
        if model_needle is not None:
            # Exact, case-insensitive: model ids are identity-like values
            # (typically taken from an ``aggregate(group_by="model")``
            # bucket), not search patterns.  No signal → never matches.
            if ev.model is None or ev.model.lower() != model_needle:
                continue
        if text_needle is not None:
            # Match the turn text AND — for a tool_call — the FULL input
            # body, so a pattern buried in a multi-line command (e.g. an
            # ``rm`` inside a ``for … do rm …; done`` loop) is found even
            # though ``ev.text`` holds only the raw tool name.
            hay = ev.text.lower() if ev.text else ""
            body_hay = ev.body.lower() if ev.body else ""
            if text_needle not in hay and text_needle not in body_hay:
                continue
        survivors.append(ev)
        score_texts.append((ev.text or "").lower())

    if group is not None and survivors:
        # Plan-task grouping SSOT: run the SAME ``_assign_plan_kinds`` the
        # ``plan`` preset uses over the collected plan_events, map each id to
        # its ``task_id``, and keep only the requested task.  Lazy import — the
        # plan module imports ``query`` (avoid the package-load cycle).
        from ai_r.events.plan import _assign_plan_kinds

        plan_dicts = [_event_to_dict(ev) for ev in survivors]
        task_by_id = {
            p.id: p.task_id for p in _assign_plan_kinds(plan_dicts)
        }
        keep = {ev.id for ev in survivors if task_by_id.get(ev.id) == group}
        paired = [
            (ev, txt)
            for ev, txt in zip(survivors, score_texts)
            if ev.id in keep
        ]
        survivors = [ev for ev, _ in paired]
        score_texts = [txt for _, txt in paired]

    if sort_lc in ("relevance", "semantic") and text_needle and survivors:
        # Re-use the SAME BM25 scorer that backs search_sessions.
        query_tokens = _tokenize(text_needle)
        docs_tokens = [_tokenize(t) for t in score_texts]
        scores = _bm25_scores(query_tokens, docs_tokens)
        order: Optional[List[int]] = None
        if sort_lc == "semantic":
            # F5.1: BM25 supplies the candidates; the local embedding
            # model re-ranks them by meaning.  ``None`` = the optional
            # dependencies/model are missing or failed — an honest
            # BM25 fallback (reported via ``semantic_out``), never a
            # crash.  Embedding sees the RAW event text (not the
            # lowercased match copy).
            order, sem_info = _semantic_order(
                text or "", [ev.text or "" for ev in survivors], scores
            )
            if semantic_out is not None:
                semantic_out.update(sem_info)
        if order is None:
            order = sorted(
                range(len(survivors)), key=lambda i: scores[i], reverse=True
            )
        survivors = [survivors[i] for i in order]
    else:
        # Date order: ts-ascending, None ts last (stable within session).
        survivors.sort(key=lambda e: (e.ts is None, e.ts or ""))

    if limit and len(survivors) > limit:
        survivors = survivors[:limit]
    out = [_event_to_dict(ev) for ev in survivors]
    if with_intent:
        _attach_intents(out)
    if redact:
        _redact_events(out, redactions_out)
    return out


# --- presets (thin wrappers, no duplicated logic) --------------------------


def intent(event: str, n: Any = 1, *, agent: Optional[str] = None) -> List[dict[str, Any]]:
    """Preset: previous user turn(s) before ``event`` — the request behind it.

    Expands to ``query(relative_to=event, direction="prev", n=n)``.  The
    ``n=1`` case reproduces :func:`previous_user_intent`.
    """
    return query(relative_to=event, direction="prev", n=n, agent=agent)


def reaction(event: str, n: Any = 1, *, agent: Optional[str] = None) -> List[dict[str, Any]]:
    """Preset: following user turn(s) after ``event`` — the user's reaction.

    Expands to ``query(relative_to=event, direction="next", n=n)`` — the
    forward mirror of :func:`intent`.
    """
    return query(relative_to=event, direction="next", n=n, agent=agent)
