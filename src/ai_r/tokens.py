"""Cross-agent per-session token usage (F3.3) — exact where recorded, honest otherwise.

Answers "how many tokens did this session consume?" **at request time** — the
session's own files are read on demand, nothing runs in the background and no
index is kept.

Three honest tiers, per session (never mixed, never guessed):

1. ``source="exact"`` — the agent's own recorded numbers, straight from the
   session file.  Every parser that has a signal exposes
   ``read_token_usage(uuid)`` (feature-for-all-where-signal rule):

   * **Claude** — per-API-call ``message.usage`` on assistant JSONL records
     (deduplicated by ``(message.id, requestId)`` — streamed responses write
     one record per content block, all carrying the same usage).
   * **Codex** — the LAST ``token_count`` event's cumulative
     ``info.total_token_usage``.
   * **OpenCode** — per-assistant-message ``tokens`` blocks in the
     ``message.data`` JSON of the SQLite DB.
   * **Pi** — per-assistant-message ``usage`` blocks in the session JSONL.
   * **Antigravity** — the brain format stores **no** usage → never exact.

2. ``source="estimate"`` — no exact record exists, so the transcript text
   volume (message text + tool inputs + tool results) is tokenized:

   * with the **optional** `tiktoken <https://github.com/openai/tiktoken>`_
     dependency installed (``pip install "ai-r[tokens]"``) →
     ``estimator="tiktoken"`` (``o200k_base``);
   * without it → ``estimator="chars/4"``, a deliberately rough
     characters-per-token heuristic.  ai-r stays zero-dependency by default;
     the estimate degrades, it never crashes.

   An estimate approximates the *transcript volume once* — unlike exact
   numbers it cannot see how many times the context was re-sent, so it is
   NOT comparable to ``exact`` and is always labeled.

3. ``source=None`` — no exact record AND nothing to estimate from (e.g. a
   reference-only session).  All fields stay ``None`` — absence is honest,
   never fabricated.

The emitted block carries only integers and ai-r-authored labels (never raw
session text), so it stays outside the F2.1 redaction pass by construction.

Consumed by ``session_stats(with_tokens=True)`` (per-session blocks folded by
the ``aggregate`` ``tokens`` metric) — see :mod:`ai_r.events.aggregate`.

Component breakdown (:func:`component_tokens`)
----------------------------------------------

:func:`component_tokens` splits a transcript's estimated token volume across
ai-r's **existing event taxonomy** — it reuses the same classifiers the
event layer uses (``resolve_tool`` for ``tool_kind``, the ``_plan_signal_*``
detector for plans, the ``user``/``assistant`` role), so it is one more
*measurement* over the established components, NOT a second classifier:

* ``user_turn``      — each user message's text (the question / request);
* ``assistant_turn`` — each assistant message's text (the answer);
* ``thinking``       — each message's model-reasoning text;
* ``plan``           — the input of plan-authoring tool calls (Claude
  ``ExitPlanMode`` / ``Write plans/*.md``, Codex ``update_plan``), detected
  via the SAME plan-signal machinery the event stream uses, so their tokens
  land here and are NOT double-counted under ``tool_call``;
* ``tool_call``      — a ``{tool_kind: tokens}`` dict; each non-plan call's
  ``input`` plus its correlated ``tool_result`` ``content`` (matched by
  ``tool_use_id``; an orphan result falls under ``other``), bucketed by the
  wrapper-aware ``tool_kind`` (``edit``/``write``/``read``/``bash``/``task``/
  ``skill``/``mcp``/``web``/``other``).

All surfaces are tokenized with **one estimator chosen once** for the whole
block (:func:`_estimate_many`), so the ``estimator`` label can never differ
between components.  The block ALWAYS carries ``source="estimate"`` +
``estimator`` — it is a volume estimate, never mixed with the ``exact``
recorded-usage tier.  ``total`` is the sum of every component (scalars +
the ``tool_call`` sub-values).  An all-empty transcript (nothing to measure)
yields ``None``; a measured-but-empty component is an honest ``0``.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from ai_r.parsers import PARSERS, Session

__all__ = [
    "TOKEN_FIELDS",
    "COMPONENT_FIELDS",
    "component_tokens",
    "estimate_tokens",
    "rollup_component_tokens",
    "session_tokens",
]

# The scalar (non-``tool_call``) components emitted by ``component_tokens``.
# ``tool_call`` is a separate ``{tool_kind: tokens}`` sub-dict.
COMPONENT_FIELDS: Tuple[str, ...] = (
    "user_turn",
    "assistant_turn",
    "thinking",
    "plan",
)


# The normalized per-session usage fields.  Sub-fields are format-native
# (documented per agent in each parser's ``read_token_usage``); ``total`` is
# the one cross-agent comparable number.
TOKEN_FIELDS: Tuple[str, ...] = (
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
    "total",
)

# Rough fallback when tiktoken is not installed: ~4 characters per token
# (the classic English-text rule of thumb; knowingly coarse for code and
# non-Latin scripts — which is exactly why the label says so).
_HEURISTIC_CHARS_PER_TOKEN = 4

# Estimator labels surfaced in the ``estimator`` field.
ESTIMATOR_TIKTOKEN = "tiktoken"
ESTIMATOR_HEURISTIC = "chars/4"

# Lazy one-shot tiktoken loader state: ``loaded`` flips on first use so a
# missing/broken tiktoken is probed exactly once per process.  Tests reset
# this dict to force either branch.
_ENCODER_STATE: dict[str, Any] = {"loaded": False, "encoder": None}


def _tiktoken_encoder() -> Any:
    """Return a cached tiktoken encoder, or ``None`` when unavailable.

    ``None`` covers every degradation path — package not installed, import
    error, or the encoding file being unavailable (tiktoken may need to
    fetch its BPE table on first use; an offline host must not crash).
    """
    if not _ENCODER_STATE["loaded"]:
        _ENCODER_STATE["loaded"] = True
        try:
            import tiktoken  # optional dependency: ai-r[tokens]

            _ENCODER_STATE["encoder"] = tiktoken.get_encoding("o200k_base")
        except Exception:
            _ENCODER_STATE["encoder"] = None
    return _ENCODER_STATE["encoder"]


def estimate_tokens(text: str) -> Tuple[int, str]:
    """Estimate the token count of ``text`` → ``(count, estimator_label)``.

    Uses tiktoken (``o200k_base``) when the optional dependency is
    importable and functional; otherwise falls back to the ``chars/4``
    heuristic.  Never raises — a tokenizer failure mid-call degrades to the
    heuristic too.
    """
    enc = _tiktoken_encoder()
    if enc is not None:
        try:
            # ``disallowed_special=()`` — session text is untrusted data; a
            # literal special-token string must count, not raise.
            return len(enc.encode(text, disallowed_special=())), ESTIMATOR_TIKTOKEN
        except Exception:
            pass
    count = (len(text) + _HEURISTIC_CHARS_PER_TOKEN - 1) // _HEURISTIC_CHARS_PER_TOKEN
    return count, ESTIMATOR_HEURISTIC


def _estimate_many(texts: Sequence[str]) -> Tuple[List[int], str]:
    """Tokenize several texts with ONE estimator → ``(counts, label)``.

    The estimator is chosen exactly once for the whole batch (tiktoken when
    importable and functional, else the ``chars/4`` heuristic), so the
    label can never differ between the texts fed in — the guarantee the
    :func:`component_tokens` breakdown relies on to keep every surface on
    the same tier.  Never raises: a tokenizer failure mid-batch degrades the
    whole batch to the heuristic.
    """
    enc = _tiktoken_encoder()
    if enc is not None:
        try:
            counts = [
                # ``disallowed_special=()`` — session text is untrusted data;
                # a literal special-token string must count, not raise.
                len(enc.encode(t, disallowed_special=()))
                for t in texts
            ]
            return counts, ESTIMATOR_TIKTOKEN
        except Exception:
            pass
    counts = [
        (len(t) + _HEURISTIC_CHARS_PER_TOKEN - 1) // _HEURISTIC_CHARS_PER_TOKEN
        for t in texts
    ]
    return counts, ESTIMATOR_HEURISTIC


def component_tokens(
    messages: Sequence[Any], *, agent: str
) -> Optional[dict[str, Any]]:
    """Estimate token volume per ai-r conversation component.

    Reuses ai-r's existing taxonomy — the ``user``/``assistant`` role, the
    wrapper-aware :func:`ai_r.events._common.resolve_tool` classifier and
    the ``_plan_signal_from_tool`` detector from :mod:`ai_r.events.model` —
    so this is one more *measurement* over the established components, never
    a second classifier.  ``agent`` is required: the plan-signal detector is
    per-agent (Claude ``ExitPlanMode``/``Write plans/*.md``, Codex
    ``update_plan``).

    Returns (all surfaces tokenized with ONE estimator via
    :func:`_estimate_many`)::

        {"user_turn": n, "assistant_turn": n, "thinking": n, "plan": n,
         "tool_call": {"<tool_kind>": n, ...},
         "total": <sum of every component>, "source": "estimate",
         "estimator": "tiktoken" | "chars/4"}

    ``tool_call`` carries only the tool kinds actually present.  Plan-authoring
    tool calls land in ``plan`` (not ``tool_call``) — no double count.  A
    ``tool_result`` is bucketed under its call's kind via ``tool_use_id``; an
    orphan result falls under ``other``.  ``None`` when every surface is empty
    (nothing to measure); a measured-but-empty scalar component is an honest
    ``0``.
    """
    # Lazy import keeps module load order robust (tokens is imported early by
    # mcp_server; events.model pulls in the parser/event graph).
    from ai_r.events._common import _coerce_tool_input, resolve_tool
    from ai_r.events.model import _plan_signal_from_tool

    # ``_plan_signal_from_tool`` compares the agent against lowercase literals
    # (``"claude"`` / ``"codex"``); an ``AgentName`` enum (value ``"CLAUDE"``)
    # or any cased string is normalized here so plan detection is never
    # silently skipped when a caller passes the enum straight through.
    agent = getattr(agent, "value", agent)
    agent = agent.lower() if isinstance(agent, str) else agent

    user_chunks: List[str] = []
    assistant_chunks: List[str] = []
    thinking_chunks: List[str] = []
    plan_chunks: List[str] = []
    tool_chunks: dict[str, List[str]] = {}
    # ``tool_use_id`` → the bucket its result should join: ("plan", None) or
    # ("tool", "<kind>").  Built in the tool_use pass, read in the result pass.
    id_bucket: dict[str, Tuple[str, Optional[str]]] = {}

    def _tool_bucket(kind: str) -> List[str]:
        return tool_chunks.setdefault(kind, [])

    for idx, m in enumerate(messages):
        role = getattr(m, "role", None)
        text = getattr(m, "text", "")
        if isinstance(text, str) and text:
            if role == "user":
                user_chunks.append(text)
            elif role == "assistant":
                assistant_chunks.append(text)
        thinking = getattr(m, "thinking", "")
        if isinstance(thinking, str) and thinking:
            thinking_chunks.append(thinking)
        for tool in getattr(m, "tool_use", ()) or ():
            if not isinstance(tool, dict):
                continue
            inp = tool.get("input", "")
            inp_str = str(inp) if inp else ""
            tuid = tool.get("tool_use_id")
            tuid = tuid if isinstance(tuid, str) and tuid else None
            sig = _plan_signal_from_tool(tool, agent=agent, message_index=idx)
            if sig is not None:
                if inp_str:
                    plan_chunks.append(inp_str)
                if tuid is not None:
                    id_bucket[tuid] = ("plan", None)
                continue
            kind, _ = resolve_tool(
                tool.get("name", ""), _coerce_tool_input(inp)
            )
            if inp_str:
                _tool_bucket(kind).append(inp_str)
            if tuid is not None:
                id_bucket[tuid] = ("tool", kind)

    # Second pass: correlate every tool_result to its call's bucket.
    for m in messages:
        for res in getattr(m, "tool_result", ()) or ():
            if not isinstance(res, dict):
                continue
            content = res.get("content", "")
            if not content:
                continue
            content = str(content)
            tuid = res.get("tool_use_id")
            bucket = id_bucket.get(tuid) if isinstance(tuid, str) else None
            if bucket is not None and bucket[0] == "plan":
                plan_chunks.append(content)
            elif bucket is not None:
                _tool_bucket(bucket[1] or "other").append(content)
            else:
                _tool_bucket("other").append(content)

    # Tokenize every surface in ONE batch so the estimator label is shared.
    tool_kinds = sorted(tool_chunks)
    surfaces = [
        "\n".join(user_chunks),
        "\n".join(assistant_chunks),
        "\n".join(thinking_chunks),
        "\n".join(plan_chunks),
    ] + ["\n".join(tool_chunks[k]) for k in tool_kinds]
    if not any(surfaces):
        # Nothing to measure at all → honest absence (not a block of zeros).
        return None

    counts, estimator = _estimate_many(surfaces)
    scalars = counts[: len(COMPONENT_FIELDS)]
    tool_counts = counts[len(COMPONENT_FIELDS):]
    block: dict[str, Any] = dict(zip(COMPONENT_FIELDS, scalars))
    block["tool_call"] = {
        k: c for k, c in zip(tool_kinds, tool_counts) if c
    }
    block["total"] = sum(scalars) + sum(block["tool_call"].values())
    block["source"] = "estimate"
    block["estimator"] = estimator
    return block


def rollup_component_tokens(
    parent: Optional[dict[str, Any]],
    children: Sequence[Optional[dict[str, Any]]],
) -> dict[str, Any]:
    """Fold a parent + its spawned children ``component_tokens`` blocks — SSOT.

    This is the ONE place that knows the parent/child token relationship, so
    both rollup callers (``read_session(include_subagents)`` and the CLI
    ``ai-r read --with-tokens --subagents``) share identical semantics.

    Two correctness rules baked in here:

    * **No subagent double count (NIT 2).**  A subagent spawn writes its
      input + returned report into the PARENT transcript as a ``task``-kind
      tool call, and the SAME text is the child's own turns in the child
      transcript.  Summing the parent block's ``tool_call["task"]`` *and*
      every child block would count that subagent's work twice.  When at
      least one child is rolled up, the parent's ``task`` bucket is therefore
      dropped from the fold — the child blocks are the authoritative,
      per-child accounting of exactly that work.  A childless rollup keeps
      the parent ``task`` bucket (nothing else accounts for it — e.g. an
      agent that records no ``parent_uuid`` and whose children are invisible).
    * **Absence stays absent (NIT 3).**  When neither the parent nor any
      child contributed a usable block, ``total`` is ``None`` (unknown), never
      a fabricated ``0`` — mirrors :func:`component_tokens` returning ``None``
      for an all-empty transcript.

    Returns a fold with the same shape ``component_tokens`` emits plus two
    provenance counters::

        {"user_turn": n, ..., "tool_call": {<kind>: n, ...},
         "total": <int|None>, "estimated": <#blocks>, "unknown": <#no-block>,
         "source": "estimate"?, "estimator": <label>?}

    ``source``/``estimator`` are present only when at least one block
    contributed.  ``estimated + unknown == 1 + len(children)``.
    """
    has_children = len(children) > 0

    scalars: dict[str, int] = {}
    tool_call: dict[str, int] = {}
    estimated = unknown = 0
    estimator: Optional[str] = None

    def _fold(block: Optional[dict[str, Any]], *, is_parent: bool) -> None:
        nonlocal estimated, unknown, estimator
        if isinstance(block, bool) or not isinstance(block, dict):
            unknown += 1
            return
        if block.get("source") == "estimate":
            estimated += 1
        else:
            unknown += 1
        if estimator is None and isinstance(block.get("estimator"), str):
            estimator = block["estimator"]
        for field in COMPONENT_FIELDS:
            val = block.get(field)
            if isinstance(val, int) and not isinstance(val, bool):
                scalars[field] = scalars.get(field, 0) + val
        sub = block.get("tool_call")
        if isinstance(sub, dict):
            for kind, val in sub.items():
                # Drop the parent's ``task`` bucket when children are rolled
                # up separately (they already account for that work) — the
                # NIT 2 double-count fix.
                if is_parent and has_children and kind == "task":
                    continue
                if isinstance(kind, str) and isinstance(val, int) \
                        and not isinstance(val, bool):
                    tool_call[kind] = tool_call.get(kind, 0) + val

    _fold(parent, is_parent=True)
    for child in children:
        _fold(child, is_parent=False)

    out: dict[str, Any] = dict(scalars)
    if tool_call:
        out["tool_call"] = tool_call
    # NIT 3: nothing measurable anywhere → total is unknown (None), not 0.
    if estimated == 0:
        out["total"] = None
    else:
        out["total"] = sum(scalars.values()) + sum(tool_call.values())
    out["estimated"] = estimated
    out["unknown"] = unknown
    if estimated:
        out["source"] = "estimate"
    if estimator is not None:
        out["estimator"] = estimator
    return out


def _empty_block() -> dict[str, Any]:
    return {field: None for field in TOKEN_FIELDS}


def session_tokens(
    session: Session,
    *,
    messages: Optional[Sequence[Any]] = None,
) -> dict[str, Any]:
    """Return the normalized token-usage block for ``session``.

    Resolution order (see the module docstring for the tier semantics):

    1. the owning parser's ``read_token_usage`` → ``source="exact"``;
    2. transcript-volume estimate → ``source="estimate"`` +
       ``estimator`` (``"tiktoken"`` | ``"chars/4"``);
    3. nothing to go on → all-``None`` fields, ``source=None``.

    The estimate total is the sum of the :func:`component_tokens` breakdown
    (single source of truth — the same estimator, no drift).  The flat block
    carries no per-component detail; ``read_session`` / ``aggregate`` surface
    that via :func:`component_tokens` separately.

    Any parser-level I/O / decode failure degrades down the same ladder —
    this function never raises on a readable inventory row.

    Args:
        messages: Already-parsed :class:`~ai_r.parsers.models.Message`
            objects to reuse for the estimate.  When ``None`` the messages
            are read from the owning parser on demand — pass them to avoid a
            second parse when the caller already has them (``read_session``).
    """
    parser = PARSERS.get(session.agent)
    block = _empty_block()
    if parser is None:  # pragma: no cover — every AgentName has a parser
        return {**block, "source": None}

    reader = getattr(parser, "read_token_usage", None)
    if callable(reader):
        try:
            exact = reader(session.uuid)
        except (FileNotFoundError, ValueError, OSError):
            exact = None
        if isinstance(exact, dict) and isinstance(exact.get("total"), int):
            return {**block, **exact, "source": "exact"}

    if messages is None:
        try:
            messages = parser.read_messages(session.uuid)
        except (FileNotFoundError, ValueError, OSError):
            messages = []
    cats = component_tokens(messages or [], agent=session.agent)
    if cats is not None:
        return {
            **block,
            "total": cats["total"],
            "source": "estimate",
            "estimator": cats["estimator"],
        }
    return {**block, "source": None}
