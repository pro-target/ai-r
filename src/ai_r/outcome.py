"""Session outcome classification (F2.3).

Answers "did this session end well?" from two *cheap, honest* signals and
returns ``"unknown"`` when neither is present — the status is never guessed:

1. **Tool-call error rate** — the share of tool results the agent itself
   flagged as failed.  The flag is a *real* source signal only for Claude
   (``tool_result.is_error``) and OpenCode (``state.status == "error"``);
   Codex / Pi / Antigravity records carry no per-result error flag, so for
   them ``tool_errors`` / ``error_rate`` are ``None`` (absence is honest,
   mirrors ``find_tool_calls.is_error_reliable``).
2. **User-verdict dictionary** — bilingual (ru + en) success/failure
   markers matched against the *tail user turns* (the user's own closing
   words).  Only user text is scanned — assistant self-reports ("Готово,
   всё зелёное") are systematically optimistic and are never trusted.

Dictionary provenance: seeded from the web-harvested reference
(``_docs/reference-6c18b957/cass_memory/src/outcome.ts`` —
POSITIVE/NEGATIVE_PATTERNS) and **calibrated against this host's real
history** (audit 2026-07-04, 107 Claude + 48 OpenCode sessions):

* real tail turns are Russian → the dictionary is ru-first with the
  reference's English patterns kept;
* «повтори» was *dropped* from the negative set: in real history it
  overwhelmingly means "I interrupted by accident, run it again", not a
  failure verdict;
* error-rate thresholds: the audited corpus has median error rate
  0.09 (Claude) / 0.02 (OpenCode) and p90 ≈ 0.22 / 0.08, so
  ``rate >= 0.5`` is far outside normal noise; requiring ``>= 4``
  results stops a 1-error-of-2-calls micro-session from flipping.

Decision table (each contributing reason is spelled out in ``signals``)::

    user verdict  errors dominant   status
    ------------  ---------------   ---------
    negative      (any)             failure
    positive      no                success
    positive      yes               mixed
    neutral       yes               failure
    neutral       no                unknown

The emitted dict contains only ai-r-authored strings and dictionary
marker labels — never raw session text — so it needs no redaction pass.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from ai_r.parsers.models import AgentName, Message

__all__ = [
    "ERROR_FLAG_RELIABLE_AGENTS",
    "session_outcome",
]


# Agents whose parsers surface a *real* per-result error flag (see
# ``parsers/models.py`` → ``Message.tool_result.is_error``).  Kept in sync
# with ``find_tool_calls``'s ``is_error_reliable``.
ERROR_FLAG_RELIABLE_AGENTS: frozenset[AgentName] = frozenset(
    {AgentName.CLAUDE, AgentName.OPENCODE}
)

# How many closing *human* user turns the verdict dictionary scans.  The
# reference scans the transcript tail; the last few user turns are the
# purest equivalent (the user's reaction to the final state).
_TAIL_USER_TURNS = 3

# Calibrated error-rate thresholds (see module docstring).
_MIN_RESULTS_FOR_ERROR_SIGNAL = 4
_ERROR_RATE_DOMINANT = 0.5

# User-turn texts that are not human speech: XML-ish tool wrappers
# (``<command-name>``, ``<system-reminder>``), harness placeholders
# (``[Request interrupted by user]``) and the IDE caveat preamble.
_NON_HUMAN_PREFIXES = ("<", "[", "Caveat:")


def _marker(label: str, pattern: str) -> tuple[str, "re.Pattern[str]"]:
    return label, re.compile(pattern, re.IGNORECASE)


# Success markers.  Labels (not the matched raw text) are what the result
# carries, so the output stays free of session content.
_POSITIVE_MARKERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # -- Russian (calibrated on real host history) --
    _marker("работает", r"\bзаработал[оа]?\b|\bработает\b|\bсработало\b"),
    _marker("готово", r"\bготово\b"),
    _marker("отлично", r"\bотлично\b|\bсупер\b|\bкласс\b|\bогонь\b|\bидеально\b"),
    _marker("спасибо", r"\bспасибо\b|\bблагодарю\b"),
    _marker("ок", r"\bок\b|\bокей\b"),
    _marker("молодец", r"\bмолодец\b|\bкрасава\b"),
    _marker("всё чисто/верно", r"вс[её] чисто|вс[её] верно|вс[её] ок"),
    # -- English (from the cass_memory reference dictionary) --
    _marker("that worked", r"that worked|it works\b|works now"),
    _marker("perfect", r"\bperfect\b"),
    _marker("thanks", r"\bthanks\b|\bthank you\b"),
    _marker("great", r"\bgreat\b"),
    _marker("solved", r"\bsolved\b"),
    _marker("lgtm", r"\blgtm\b|looks good"),
    _marker("nice work", r"nice work|well done"),
    _marker("ship it", r"\bship it\b"),
)

# Failure markers.
_NEGATIVE_MARKERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # -- Russian (calibrated on real host history) --
    _marker(
        "не работает",
        r"не работает|не заработало|не сработало|перестало работать",
    ),
    _marker("сломано", r"\bсломал(ось|ась|о|а)?\b|\bполомал"),
    _marker("откати", r"\bоткати(ть)?\b|верни как было"),
    _marker(
        "неправильно",
        r"\bневерно\b|\bнеправильно\b|не то сделал|вс[её] не так",
    ),
    _marker("переделай", r"\bпеределай\b|начни заново|начать заново"),
    _marker(
        "не помогло",
        r"не помогло|стало хуже|та же ошибка|опять ошибка|снова ошибка",
    ),
    _marker("негатив-лексика", r"говн|хуйн|херн|фигн|ерунд|\bчушь\b|\bбред\b"),
    _marker("косяк", r"\bкосяк"),
    # -- English (from the cass_memory reference dictionary) --
    _marker(
        "doesn't work",
        r"doesn'?t work|does not work|not working|still (fails|failing|broken)",
    ),
    _marker("broke", r"\bbroke\b|\bbroken\b"),
    _marker("wrong", r"\bwrong\b"),
    _marker("not what i wanted", r"not what i (wanted|asked)"),
    _marker("try again", r"\btry again\b"),
    _marker("undo/revert", r"\bundo\b|\brevert\b|roll ?back|start over"),
)


def _tail_user_texts(messages: Sequence[Message]) -> list[str]:
    """The last :data:`_TAIL_USER_TURNS` *human* user-turn texts.

    Human = ``role == "user"`` with non-empty text that is not a tool
    wrapper / harness placeholder (see :data:`_NON_HUMAN_PREFIXES`).
    Tool-result-only user records (Claude embeds results in user
    records) have empty ``text`` and drop out naturally.
    """
    texts: list[str] = []
    for msg in messages:
        if msg.role != "user":
            continue
        text = (msg.text or "").strip()
        if not text or text.startswith(_NON_HUMAN_PREFIXES):
            continue
        texts.append(text)
    return texts[-_TAIL_USER_TURNS:]


def _match_markers(
    texts: Sequence[str],
    markers: Sequence[tuple[str, "re.Pattern[str]"]],
) -> list[str]:
    """Labels of every marker that fires in at least one text (dict order)."""
    joined = "\n".join(texts)
    return [label for label, pattern in markers if pattern.search(joined)]


def session_outcome(
    messages: Sequence[Message],
    agent: Optional[AgentName],
) -> dict[str, Any]:
    """Classify the outcome of a session from its parsed messages.

    Args:
        messages: The session's full structured message list
            (``parsers.<agent>.read_messages`` output).
        agent: The owning agent — decides whether the tool-result error
            flag is a real source signal (:data:`ERROR_FLAG_RELIABLE_AGENTS`)
            or best-effort noise that must be reported as ``None``.

    Returns:
        A dict::

            {
              "status": "success" | "failure" | "mixed" | "unknown",
              "signals": [<human-readable reason>, ...],   # empty <=> unknown
              "user_verdict": "positive" | "negative" | "neutral",
              "markers": {"positive": [<label>...], "negative": [...]},
              "tool_results": <int>,          # counted for every agent
              "tool_errors": <int> | None,    # None when flag unreliable
              "error_rate": <float> | None,   # errors / results, 4 digits
              "error_rate_reliable": <bool>,
            }

        Contains only ai-r-authored strings / dictionary labels — no raw
        session text — so it is safe to emit without a redaction pass.
    """
    reliable = agent in ERROR_FLAG_RELIABLE_AGENTS

    n_results = 0
    n_errors = 0
    for msg in messages:
        for result in msg.tool_result or ():
            n_results += 1
            if result.get("is_error"):
                n_errors += 1

    error_rate: Optional[float] = None
    tool_errors: Optional[int] = None
    if reliable:
        tool_errors = n_errors
        if n_results:
            error_rate = round(n_errors / n_results, 4)

    errors_dominant = (
        reliable
        and n_results >= _MIN_RESULTS_FOR_ERROR_SIGNAL
        and error_rate is not None
        and error_rate >= _ERROR_RATE_DOMINANT
    )

    tail = _tail_user_texts(messages)
    positive = _match_markers(tail, _POSITIVE_MARKERS)
    negative = _match_markers(tail, _NEGATIVE_MARKERS)
    if len(positive) > len(negative):
        verdict = "positive"
    elif len(negative) > len(positive):
        verdict = "negative"
    else:
        verdict = "neutral"

    signals: list[str] = []
    if verdict != "neutral":
        winning = positive if verdict == "positive" else negative
        signals.append(
            f"user verdict: {verdict} "
            f"(markers in last {_TAIL_USER_TURNS} user turns: "
            f"{', '.join(winning)})"
        )
    if errors_dominant:
        signals.append(
            f"tool error rate {error_rate:.2f} ({n_errors}/{n_results}) "
            f">= {_ERROR_RATE_DOMINANT} across >= "
            f"{_MIN_RESULTS_FOR_ERROR_SIGNAL} tool results"
        )

    if verdict == "negative":
        status = "failure"
    elif verdict == "positive":
        status = "mixed" if errors_dominant else "success"
    elif errors_dominant:
        status = "failure"
    else:
        status = "unknown"

    return {
        "status": status,
        "signals": signals,
        "user_verdict": verdict,
        "markers": {"positive": positive, "negative": negative},
        "tool_results": n_results,
        "tool_errors": tool_errors,
        "error_rate": error_rate,
        "error_rate_reliable": reliable,
    }
