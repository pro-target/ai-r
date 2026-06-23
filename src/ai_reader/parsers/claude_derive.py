"""Heuristic derivation helpers for Claude sessions.

Two pure helpers that operate on the :class:`Message` list produced by
:func:`ai_reader.parsers.claude.read_messages` (or any equivalent):

* :func:`extract_decisions` — pull decision phrases from assistant
  messages, filtered by tech-specificity tokens so an engineer
  reviewing the session gets actionable signal.
* :func:`summarize_task` — find the most recent non-trivial user
  request, walking past stopword tails like ``"thanks"`` or ``"ok"``
  that usually don't carry task intent.
"""
from __future__ import annotations

import re
from typing import List

from .models import Message


_DECISION_PATTERN = re.compile(
    r"\b(?:decided to|chose|will use|going with|using|switched to)\b",
    re.IGNORECASE,
)
_TECH_TOKEN_PATTERN = re.compile(
    r"\b(?:port \d+|\.py|\.ts|\.js|\.go|\.rs|docker|hook|api|function|class|module|lib|dependency|library)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_STOPWORDS = frozenset(
    {
        "ok",
        "thanks",
        "thank",
        "yes",
        "no",
        "please",
        "got it",
        "great",
        "sure",
        "fine",
        "the",
        "a",
        "an",
        "this",
        "that",
        "is",
        "are",
        "was",
        "were",
    }
)
_MAX_DECISIONS = 10
_MAX_DECISION_LEN = 200
_SHORT_TAIL_WORDS = 3


def extract_decisions(messages: List[Message]) -> List[str]:
    """Return up to 10 decision phrases from assistant messages.

    A sentence is kept when it both matches a decision verb
    (``decided to``, ``chose``, ``will use``, ``going with``,
    ``using``, ``switched to``) and mentions at least one
    tech-specificity token (``port 8080``, ``.py``, ``docker``,
    ``hook``, ``api``, ``function``, ``class``, ``module``, ``lib``,
    ``dependency``, ``library``).  Each decision is capped at 200
    characters.
    """
    decisions: List[str] = []
    for msg in messages:
        if msg.role != "assistant":
            continue
        for sentence in _SENTENCE_SPLIT.split(msg.text):
            sentence = sentence.strip()
            if not sentence:
                continue
            if not _DECISION_PATTERN.search(sentence):
                continue
            if not _TECH_TOKEN_PATTERN.search(sentence):
                continue
            decisions.append(sentence[:_MAX_DECISION_LEN])
            if len(decisions) >= _MAX_DECISIONS:
                return decisions
    return decisions


def summarize_task(messages: List[Message]) -> str:
    """Return the first sentence of the most recent non-trivial user task.

    Walks user messages in reverse.  When the most recent user message
    has three or fewer words it is treated as a stopword tail
    (``"thanks"``, ``"ok"``, …) and the prior user message is used
    instead.  Stopword tokens are stripped from the result.
    """
    user_texts: List[str] = [
        msg.text.strip() for msg in reversed(messages)
        if msg.role == "user" and msg.text.strip()
    ]
    if not user_texts:
        return ""
    chosen = user_texts[0]
    if len(chosen.split()) <= _SHORT_TAIL_WORDS and len(user_texts) > 1:
        chosen = user_texts[1]
    first_sentence = _SENTENCE_SPLIT.split(chosen)[0].strip()
    if not first_sentence:
        return ""
    words = [
        w for w in re.split(r"\s+", first_sentence) if w.lower() not in _STOPWORDS
    ]
    return " ".join(words)


__all__ = ["extract_decisions", "summarize_task"]
