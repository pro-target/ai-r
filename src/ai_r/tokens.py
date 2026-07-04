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
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

from ai_r.parsers import PARSERS, Session

__all__ = [
    "TOKEN_FIELDS",
    "estimate_tokens",
    "session_tokens",
]


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


def _transcript_text(messages: Sequence[Any]) -> str:
    """Concatenate a session's text surface for volume estimation.

    Message text + tool-call inputs + tool-result contents — the same
    surface the body search indexes, so the estimate reflects what actually
    flowed through the conversation, not just the narration.
    """
    chunks: List[str] = []
    for m in messages:
        text = getattr(m, "text", "")
        if isinstance(text, str) and text:
            chunks.append(text)
        for tool in getattr(m, "tool_use", ()) or ():
            if isinstance(tool, dict):
                inp = tool.get("input", "")
                if inp:
                    chunks.append(str(inp))
        for res in getattr(m, "tool_result", ()) or ():
            if isinstance(res, dict):
                content = res.get("content", "")
                if content:
                    chunks.append(str(content))
    return "\n".join(chunks)


def _empty_block() -> dict[str, Any]:
    return {field: None for field in TOKEN_FIELDS}


def session_tokens(session: Session) -> dict[str, Any]:
    """Return the normalized token-usage block for ``session``.

    Resolution order (see the module docstring for the tier semantics):

    1. the owning parser's ``read_token_usage`` → ``source="exact"``;
    2. transcript-volume estimate → ``source="estimate"`` +
       ``estimator`` (``"tiktoken"`` | ``"chars/4"``);
    3. nothing to go on → all-``None`` fields, ``source=None``.

    Any parser-level I/O / decode failure degrades down the same ladder —
    this function never raises on a readable inventory row.
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

    try:
        messages = parser.read_messages(session.uuid)
    except (FileNotFoundError, ValueError, OSError):
        messages = []
    text = _transcript_text(messages)
    if text.strip():
        total, estimator = estimate_tokens(text)
        return {
            **block,
            "total": total,
            "source": "estimate",
            "estimator": estimator,
        }

    return {**block, "source": None}
