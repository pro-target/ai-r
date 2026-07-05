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

Category breakdown (``breakdown=True``)
---------------------------------------

On request :func:`session_tokens` attaches a ``categories`` sub-block that
splits the transcript volume into four honest surfaces —

* ``text``        — each message's plain-text content;
* ``thinking``    — each message's model-reasoning text;
* ``tool_input``  — each tool-call's ``input``;
* ``tool_result`` — each tool-result's ``content``.

All four are tokenized with **one estimator chosen once** for the whole
block (:func:`_estimate_many`), so the ``estimator`` label can never differ
between categories.  The sub-block therefore ALWAYS carries its own
``source="estimate"`` + ``estimator`` — a **tier-separation invariant**:
even when the outer block is ``source="exact"`` the categories are an
independent estimate and are NEVER merged into the exact numbers.  The four
category counts sum exactly to ``categories["total"]``; on the estimate path
the outer ``total`` is *defined* as that same sum (single source of truth,
no drift between the two).  An all-empty transcript (nothing to measure)
yields ``categories: None``; a measured-but-empty category is an honest
``0`` — the pass ran, it just found no text there.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from ai_r.parsers import PARSERS, Session

__all__ = [
    "TOKEN_FIELDS",
    "estimate_tokens",
    "session_tokens",
    "transcript_categories",
]

# The four transcript surfaces split out by ``transcript_categories``.
CATEGORY_FIELDS: Tuple[str, ...] = (
    "text",
    "thinking",
    "tool_input",
    "tool_result",
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
    category breakdown relies on to keep all four surfaces on the same
    tier.  Never raises: a tokenizer failure mid-batch degrades the whole
    batch to the heuristic.
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


def transcript_categories(messages: Sequence[Any]) -> Optional[dict[str, Any]]:
    """Split a transcript's estimated token volume into four surfaces.

    Builds four separate concatenations from ``messages`` —

    * ``text``        — each ``Message.text``;
    * ``thinking``    — each ``Message.thinking``;
    * ``tool_input``  — each ``tool_use`` dict's ``"input"``;
    * ``tool_result`` — each ``tool_result`` dict's ``"content"``.

    All four are tokenized with a **single** estimator (via
    :func:`_estimate_many`) so the ``estimator`` label is identical across
    categories.  Returns::

        {"text": n, "thinking": n, "tool_input": n, "tool_result": n,
         "total": <sum of the four>, "source": "estimate",
         "estimator": "tiktoken" | "chars/4"}

    ``None`` when all four surfaces are empty (nothing to measure).  A
    measured-but-empty category is an honest ``0`` (the pass ran).
    """
    text_chunks: List[str] = []
    thinking_chunks: List[str] = []
    tool_input_chunks: List[str] = []
    tool_result_chunks: List[str] = []
    for m in messages:
        text = getattr(m, "text", "")
        if isinstance(text, str) and text:
            text_chunks.append(text)
        thinking = getattr(m, "thinking", "")
        if isinstance(thinking, str) and thinking:
            thinking_chunks.append(thinking)
        for tool in getattr(m, "tool_use", ()) or ():
            if isinstance(tool, dict):
                inp = tool.get("input", "")
                if inp:
                    tool_input_chunks.append(str(inp))
        for res in getattr(m, "tool_result", ()) or ():
            if isinstance(res, dict):
                content = res.get("content", "")
                if content:
                    tool_result_chunks.append(str(content))

    surfaces = [
        "\n".join(text_chunks),
        "\n".join(thinking_chunks),
        "\n".join(tool_input_chunks),
        "\n".join(tool_result_chunks),
    ]
    if not any(s for s in surfaces):
        # Nothing to measure at all → honest absence (not a block of zeros).
        return None

    counts, estimator = _estimate_many(surfaces)
    block: dict[str, Any] = dict(zip(CATEGORY_FIELDS, counts))
    block["total"] = sum(counts)
    block["source"] = "estimate"
    block["estimator"] = estimator
    return block


def _empty_block() -> dict[str, Any]:
    return {field: None for field in TOKEN_FIELDS}


def session_tokens(
    session: Session,
    *,
    breakdown: bool = False,
    messages: Optional[Sequence[Any]] = None,
) -> dict[str, Any]:
    """Return the normalized token-usage block for ``session``.

    Resolution order (see the module docstring for the tier semantics):

    1. the owning parser's ``read_token_usage`` → ``source="exact"``;
    2. transcript-volume estimate → ``source="estimate"`` +
       ``estimator`` (``"tiktoken"`` | ``"chars/4"``);
    3. nothing to go on → all-``None`` fields, ``source=None``.

    Any parser-level I/O / decode failure degrades down the same ladder —
    this function never raises on a readable inventory row.

    Args:
        breakdown: When ``True`` attach a ``categories`` sub-block
            (:func:`transcript_categories`) on BOTH the exact and estimate
            paths.  On the exact path the outer fields stay exact and the
            sub-block carries its own ``source="estimate"`` + ``estimator``
            — the tier-separation invariant (never merged).  A parser read
            failure while gathering messages for the categories yields
            ``"categories": None``.  When ``False`` the output shape is
            byte-identical to the historical block (no ``categories`` key).
        messages: Already-parsed :class:`~ai_r.parsers.models.Message`
            objects to reuse for the estimate / categories.  When ``None``
            the messages are read from the owning parser on demand — pass
            them to avoid a second parse when the caller already has them
            (e.g. ``read_session``).

    On the estimate path the outer ``total`` is defined as the sum of the
    four categories (single source of truth), so ``total`` ==
    ``categories["total"]`` there.
    """
    parser = PARSERS.get(session.agent)
    block = _empty_block()
    if parser is None:  # pragma: no cover — every AgentName has a parser
        return {**block, "source": None}

    # Lazily read messages at most once, reusing a caller-supplied list.
    # ``_read_failed`` distinguishes "read raised" (categories → None) from
    # "read returned an empty list" (categories → None too, but via the
    # empty-surface path).
    _msg_cache: dict[str, Any] = {"loaded": messages is not None,
                                   "value": messages, "failed": False}

    def _messages() -> Sequence[Any]:
        if not _msg_cache["loaded"]:
            _msg_cache["loaded"] = True
            try:
                _msg_cache["value"] = parser.read_messages(session.uuid)
            except (FileNotFoundError, ValueError, OSError):
                _msg_cache["value"] = []
                _msg_cache["failed"] = True
        return _msg_cache["value"] or []

    def _categories() -> Optional[dict[str, Any]]:
        msgs = _messages()
        if _msg_cache["failed"]:
            return None
        return transcript_categories(msgs)

    reader = getattr(parser, "read_token_usage", None)
    if callable(reader):
        try:
            exact = reader(session.uuid)
        except (FileNotFoundError, ValueError, OSError):
            exact = None
        if isinstance(exact, dict) and isinstance(exact.get("total"), int):
            result = {**block, **exact, "source": "exact"}
            if breakdown:
                # Tier separation: the exact outer numbers stay exact; the
                # categories are an independent estimate labeled as such.
                result["categories"] = _categories()
            return result

    if breakdown:
        cats = _categories()
        if cats is not None:
            # Estimate path: the outer total IS the category sum (SSOT).
            return {
                **block,
                "total": cats["total"],
                "source": "estimate",
                "estimator": cats["estimator"],
                "categories": cats,
            }
        # Nothing to estimate from (empty or unreadable transcript).
        return {**block, "source": None, "categories": cats}

    # ``breakdown=False`` — historical shape, single estimate total.
    cats = transcript_categories(_messages())
    if cats is not None:
        return {
            **block,
            "total": cats["total"],
            "source": "estimate",
            "estimator": cats["estimator"],
        }
    return {**block, "source": None}
