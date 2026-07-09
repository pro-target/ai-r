"""Event model + query core — the unified session-event surface (Phase 1).

This package layers a single, agent-neutral *event stream* over the
per-agent parsers.  Every parser already returns
:class:`ai_r.parsers.models.Message` objects; :func:`iter_events`
normalises those (plus their embedded ``tool_use`` calls) into a flat
sequence of :class:`Event` records so downstream verbs never have to
know how any individual agent stores turns or tool calls.

Design (see ``_docs/knowledge/extraction-core.md``):

* **Event** is the atom: ``id, session_id, agent, ts, type, text?,
  refs[], source, sha256``.  ``type`` is one of ``user_turn``,
  ``assistant_turn``, ``tool_call(<sub>)`` or ``plan_event`` (the last
  is a Phase-2 placeholder — no producer emits it yet).
* **query(facets)** is the workhorse filter over that stream.  The
  killer facet is ``relative_to`` + ``direction`` + ``n``: a general
  timeline walk in either direction, of which the historical
  :func:`ai_r.find_file_edits.previous_user_intent` is the ``prev`` /
  ``n=1`` special case.
* ``text`` + ``sort=relevance`` re-uses the *exact* BM25 scorer that
  backs ``search_sessions`` (:mod:`ai_r.ranking`) — no algorithm is
  duplicated here.
* **intent** / **reaction** presets are thin wrappers over ``query``.

Everything is additive: existing tools/tests are untouched.

Package layout (formerly a single ``events.py`` module — split by concern,
the public + private import surface is preserved verbatim through the
re-exports below):

* :mod:`ai_r.events._common`   — :class:`Event`, ``classify_tool`` +
  tool-name vocabulary, content hashing, tool-input helpers.
* :mod:`ai_r.events.render`    — edit-hunk normalisation/rendering +
  caveat constants (``_hunk_from_tool`` / ``_render_hunk`` /
  ``_GIT_CAVEAT`` / ``_RISK3_CAVEAT``); lifted out of ``session_diff`` so
  the core no longer depends on that preset.
* :mod:`ai_r.events.model`     — plan-signal detection + ``iter_events``.
* :mod:`ai_r.events.query`     — ``query`` + ``intent`` / ``reaction``.
* :mod:`ai_r.events.plan`      — ``Plan``, ``plan``, ``get_body`` + task grouping.
* :mod:`ai_r.events.aggregate` — the ``aggregate`` rollup verb.
* :mod:`ai_r.events.diff`      — the ``diff`` stitching verb.
* :mod:`ai_r.events.detect`    — ``detect_current`` (runtime identity).
"""

from __future__ import annotations

# --- public surface (matches the historical ``events.__all__``) -----------
from ai_r.events._common import (
    TOOL_KIND,
    TOOL_SUBTYPE,
    Event,
    classify_tool,
    resolve_tool,
)
from ai_r.events.model import iter_events
from ai_r.events.query import (
    intent,
    query,
    reaction,
)
from ai_r.events.plan import (
    Plan,
    get_body,
    plan,
    plan_feedback,
)
from ai_r.events.aggregate import aggregate
from ai_r.events.diff import diff
from ai_r.events.detect import detect_current

# --- private surface preserved for backward-compatible import paths -------
# Historical callers/tests import some private helpers straight from
# ``ai_r.events``.  Re-export them here so every symbol that used to be
# importable as ``from ai_r.events import <name>`` still resolves after the
# split (import surface is 100% stable — public AND private).
from ai_r.events._common import (  # noqa: F401
    _BASH_NAMES,
    _EDIT_NAMES,
    _PATH_KEYS,
    _READ_NAMES,
    _WRITE_NAMES,
    _coerce_tool_input,
    _mk_event,
    _path_from_payload,
    _plan_ref_value,
    _sha256,
)
from ai_r.events.render import (  # noqa: F401
    _GIT_CAVEAT,
    _RISK3_CAVEAT,
    _hunk_from_tool,
    _render_hunk,
)
from ai_r.events.model import (  # noqa: F401
    _PlanSignal,
    _antigravity_plan_signal,
    _claude_plan_slug,
    _codex_plan_status,
    _messages_to_events,
    _normalize_task_key,
    _plan_signal_from_tool,
    _plan_signals_for_session,
    _title_from_markdown_body,
)
from ai_r.events.query import (  # noqa: F401
    _attach_intents,
    _event_to_dict,
    _type_matches,
    _walk_relative,
)
from ai_r.events.plan import (  # noqa: F401
    _assign_plan_kinds,
    _plan_to_dict,
    _resolve_plan_signal,
)
from ai_r.events.aggregate import (  # noqa: F401
    _KIND_SPLIT_NOTE,
    _METRICS,
    _collect_agents,
    _collect_intents,
    _metric_edits,
    _metric_files,
    _metric_messages,
    _metric_sessions,
    _row_group_keys,
)
from ai_r.events.diff import _edit_input_from_event  # noqa: F401

__all__ = [
    "Event",
    "TOOL_KIND",
    "TOOL_SUBTYPE",
    "classify_tool",
    "resolve_tool",
    "iter_events",
    "query",
    "intent",
    "reaction",
    "Plan",
    "plan",
    "plan_feedback",
    "get_body",
    "aggregate",
    "diff",
    "detect_current",
]
