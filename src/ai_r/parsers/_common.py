"""Shared helpers for the session parsers.

Several per-agent parser modules (``codex``, ``claude``, ``pi``,
``antigravity``) historically carried byte-for-byte copies of a handful
of small utilities.  This module is the single source of truth for the
ones whose behaviour is genuinely identical across parsers; each parser
re-imports the name it needs so module-level references and test
monkeypatches (e.g. ``codex._is_valid_uuid``) keep working unchanged.

Only behaviourally identical helpers live here.  Parser-specific
variants (e.g. Pi's tz-pinning ``_parse_iso_timestamp`` or Claude's
``_normalise_title`` that does not coerce whitespace-only input to
``"Untitled"``) intentionally stay in their own modules.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional, Tuple


# Maximum number of characters retained in a normalised session title.
_TITLE_MAX_LEN = 100

# Join token for multi-select answers (one chosen option per element).
_QA_ANSWER_JOIN = " | "

# Matches one ``"question"="answer"`` pair inside the human-readable result
# string emitted by Claude (``Your questions have been answered: ...``) and
# OpenCode (``User has answered your questions: ...``).  Both quote the
# question and answer; answers may themselves contain escaped quotes and
# newlines, so the answer group is non-greedy and we anchor on the closing
# quote that precedes ``,`` / ``.`` / end-of-string.
_QA_PAIR_RE = re.compile(
    r'"(?P<q>(?:[^"\\]|\\.)*)"\s*=\s*"(?P<a>(?:[^"\\]|\\.)*)"',
    re.DOTALL,
)


def _qa_options_from_question(question: dict) -> Tuple[str, ...]:
    """Return the offered option labels for one structured question.

    Accepts the shared interactive-question shape used by Claude
    (``AskUserQuestion``), Codex (``request_user_input``) and OpenCode
    (``question``): ``{"question": str, "options": [{"label": str,
    "description": str}, ...]}``.  Plain-string options are tolerated.
    Returns an empty tuple when no usable labels are present.
    """
    if not isinstance(question, dict):
        return ()
    raw_options = question.get("options")
    if not isinstance(raw_options, list):
        return ()
    labels: List[str] = []
    for opt in raw_options:
        if isinstance(opt, dict):
            label = opt.get("label")
            if isinstance(label, str) and label:
                labels.append(label)
        elif isinstance(opt, str) and opt:
            labels.append(opt)
    return tuple(labels)


def _qa_pairs_from_result_text(text: object) -> List[Tuple[str, str]]:
    """Extract ``(question, answer)`` pairs from a result-string answer blob.

    Claude and OpenCode both serialise the user's choice into a single
    human-readable string of ``"question"="answer"`` pairs.  This is the
    *only* place Claude records the answer text, so parsing it is the
    canonical way to recover the question→answer pairing for Claude.

    Returns an empty list when ``text`` is not a string or contains no
    recognisable pair.  Backslash escapes (``\\"``, ``\\n``) produced by
    JSON-style quoting are unescaped in the captured groups.
    """
    if not isinstance(text, str) or not text:
        return []

    def _unescape(s: str) -> str:
        return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")

    pairs: List[Tuple[str, str]] = []
    for match in _QA_PAIR_RE.finditer(text):
        q = _unescape(match.group("q")).strip()
        a = _unescape(match.group("a")).strip()
        if q or a:
            pairs.append((q, a))
    return pairs


def _qa_from_codex(questions: object, answers_obj: object) -> List[dict]:
    """Build ``qa`` entries from a Codex ``request_user_input`` exchange.

    Codex stores the call arguments as ``{"questions": [{"id", "header",
    "question", "options": [...]}]}`` and the answer output as
    ``{"answers": {"<question-id>": {"answers": ["<label>", ...]}}}``.
    Answers are keyed by the question ``id``; a question with no entry
    (the user skipped/dismissed) yields an empty ``answer`` string but is
    still surfaced so the question itself is not lost.

    Returns an empty list when ``questions`` is not a usable list.
    """
    if not isinstance(questions, list):
        return []
    answers_map: dict = {}
    if isinstance(answers_obj, dict):
        inner = answers_obj.get("answers")
        if isinstance(inner, dict):
            answers_map = inner
    out: List[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        q_text = q.get("question")
        if not isinstance(q_text, str):
            continue
        options = _qa_options_from_question(q)
        qid = q.get("id")
        chosen: List[str] = []
        if isinstance(qid, str):
            ans_entry = answers_map.get(qid)
            if isinstance(ans_entry, dict):
                raw = ans_entry.get("answers")
                if isinstance(raw, list):
                    chosen = [str(a) for a in raw if isinstance(a, (str, int, float))]
        out.append(_qa_entry(q_text.strip(), options, _QA_ANSWER_JOIN.join(chosen)))
    return out


def _qa_from_structured_answers(
    questions: object, answers: object
) -> List[dict]:
    """Build ``qa`` entries from a parallel-array answer structure.

    OpenCode's ``question`` tool stores the offered questions as
    ``state.input.questions`` and the chosen answers as
    ``state.metadata.answers`` — a list parallel to ``questions`` where
    each element is itself a list of chosen labels (multi-select yields
    more than one).  Pairs them positionally.

    Returns an empty list when ``questions`` is not a usable list.
    """
    if not isinstance(questions, list):
        return []
    answer_list = answers if isinstance(answers, list) else []
    out: List[dict] = []
    for idx, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        q_text = q.get("question")
        if not isinstance(q_text, str):
            continue
        options = _qa_options_from_question(q)
        chosen: List[str] = []
        if idx < len(answer_list):
            raw = answer_list[idx]
            if isinstance(raw, list):
                chosen = [str(a) for a in raw if isinstance(a, (str, int, float))]
            elif isinstance(raw, (str, int, float)):
                chosen = [str(raw)]
        out.append(_qa_entry(q_text.strip(), options, _QA_ANSWER_JOIN.join(chosen)))
    return out


def _qa_entry(question: str, options: Tuple[str, ...], answer: str) -> dict:
    """Build a single normalised ``qa`` entry dict.

    The schema is the cross-agent contract surfaced on
    :class:`~ai_r.parsers.models.Message.qa`: a question paired with the
    answer the user chose, plus the options that were offered.
    """
    return {"question": question, "options": options, "answer": answer}


def _parse_iso_timestamp(raw: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp, tolerating a trailing ``Z``.

    Returns ``None`` for empty input, non-strings, and unparseable
    values.  Only the first 23 characters are considered, which keeps
    fractional seconds while ignoring any trailing offset noise.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw[:23].replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _is_valid_uuid(uuid: str) -> bool:
    """Return ``True`` if ``uuid`` is a safe, path-free session identifier.

    Rejects empty values, non-strings, values with surrounding or
    embedded whitespace, and anything containing a path separator.
    """
    if not uuid or not isinstance(uuid, str):
        return False
    stripped = uuid.strip()
    if not stripped or stripped != uuid:
        return False
    if any(c.isspace() for c in stripped) or "/" in stripped or "\\" in stripped:
        return False
    return True


def _normalise_title(raw: str) -> str:
    """Collapse newlines and truncate to ``_TITLE_MAX_LEN`` chars.

    Whitespace-only (and empty) input collapses to ``"Untitled"``.
    """
    cleaned = raw.replace("\n", " ").replace("\r", " ").strip()
    return cleaned[:_TITLE_MAX_LEN] or "Untitled"
