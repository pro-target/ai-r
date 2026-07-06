"""Shared helpers for the session parsers.

Several per-agent parser modules (``codex``, ``claude``, ``pi``,
``antigravity``) historically carried byte-for-byte copies of a handful
of small utilities.  This module is the single source of truth for the
ones whose behaviour is genuinely identical across parsers; each parser
re-imports the name it needs so module-level references and test
monkeypatches (e.g. ``codex._is_valid_uuid``) keep working unchanged.

Only behaviourally identical helpers live here.  Parser-specific
variants (e.g. Pi's ``_parse_iso_timestamp`` that also accepts non-str
input, or Claude's ``_normalise_title`` that does not coerce
whitespace-only input to ``"Untitled"``) intentionally stay in their
own modules.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


# --- JSONL reading caps -------------------------------------------------
#
# Every per-agent parser reads newline-delimited JSON (one record per
# line).  A single pathological file must not be able to exhaust memory:
# a .jsonl with no newlines would otherwise be slurped whole by
# ``for line in fh``.  These caps bound both a single line and the
# cumulative bytes read.  They are exposed as module constants so the
# streaming/event layer (a later refactor) can align on the same limits.

# Largest single line (bytes, measured on the decoded text) we will
# hand to ``json.loads``.  A line longer than this is SKIPPED whole — we
# do not truncate, because a truncated JSON object is not valid JSON and
# would only ever raise ``json.loads`` failure; skipping is the same
# observable outcome without the wasted parse.  16 MiB comfortably fits
# any legitimate transcript record (large tool outputs included) while
# refusing a runaway newline-free blob.
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024

# Largest cumulative decoded size we will read from one file before
# stopping iteration.  Generous (1 GiB) — real session files are orders
# of magnitude smaller; this only trips on corruption/abuse.
MAX_JSONL_TOTAL_BYTES = 1024 * 1024 * 1024


def iter_jsonl_records(
    path: Path,
    *,
    max_line_bytes: int = MAX_JSONL_LINE_BYTES,
    max_total_bytes: int = MAX_JSONL_TOTAL_BYTES,
    errors: str = "replace",
) -> Iterator[dict]:
    """Yield each valid ``dict`` record from a JSONL file, guarded.

    This is the single source of truth for the read loop every parser
    historically copied: ``strip → skip blank → json.loads → skip on
    failure → skip non-dict``.  Two hardening properties over the old
    hand-rolled loops:

    * **Bounded per line.** The file is read in chunks and split on
      ``\\n`` so a single newline-free multi-gigabyte file cannot be
      slurped into memory.  Any line whose decoded length exceeds
      ``max_line_bytes`` is skipped whole (a truncated JSON object is
      not valid JSON, so truncating would only ever fail to parse).

    * **Encoding-tolerant.** Opened with ``errors="replace"`` so a stray
      non-UTF-8 byte yields a replacement character rather than raising
      ``UnicodeDecodeError`` and making the whole session vanish.

    Reading stops once ``max_total_bytes`` of decoded text has been
    consumed.  ``OSError`` while reading is swallowed (iteration simply
    ends) so callers keep whatever they collected — matching the prior
    per-parser ``except OSError`` behaviour.
    """
    try:
        with path.open("r", encoding="utf-8", errors=errors) as fh:
            total = 0
            pending = ""
            over_long = False  # current physical line already exceeded cap
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_total_bytes:
                    break
                pending += chunk
                # Emit every complete line (all but the trailing fragment).
                while True:
                    nl = pending.find("\n")
                    if nl == -1:
                        break
                    raw = pending[:nl]
                    pending = pending[nl + 1 :]
                    if over_long:
                        # We already decided this physical line is too
                        # long; discard its tail up to the newline.
                        over_long = False
                        continue
                    yield from _parse_jsonl_line_str(raw, max_line_bytes)
                # Bound the still-incomplete fragment. In ``over_long`` state we
                # are discarding everything up to the next newline, so the tail
                # accumulated so far can be dropped now — otherwise a
                # newline-free (or long-tailed) file would let ``pending``
                # regrow chunk-by-chunk all the way to ``max_total_bytes``
                # (~1 GiB), defeating the per-line cap. Otherwise, if the
                # fragment alone already blew the cap, start dropping its line.
                if over_long:
                    pending = ""
                elif len(pending) > max_line_bytes:
                    over_long = True
                    pending = ""
            # Flush any final line without a trailing newline.
            if pending and not over_long:
                yield from _parse_jsonl_line_str(pending, max_line_bytes)
    except OSError:
        return


def _parse_jsonl_line_str(raw: str, max_line_bytes: int) -> Iterator[dict]:
    """Parse one physical JSONL line into at most one ``dict`` record."""
    if len(raw) > max_line_bytes:
        return
    line = raw.strip()
    if not line:
        return
    try:
        record = json.loads(line)
    except ValueError:
        # ValueError is the base of json.JSONDecodeError; also covers the
        # rare non-decode ValueError from json.loads.
        return
    if isinstance(record, dict):
        yield record


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
    """Parse an ISO 8601 timestamp, always returning a tz-aware datetime.

    Returns ``None`` for empty input, non-strings, and unparseable
    values.  The full string is parsed first (so ``Z``/explicit offsets
    are honoured); a 23-character truncation is the fallback for values
    with trailing noise.  Naive results are pinned to UTC — a naive
    datetime would mix with tz-aware ones (e.g. Desktop-overlay epoch
    dates) and break ``sessions.sort(key=s.date)`` with a ``TypeError``.
    """
    if not raw or not isinstance(raw, str):
        return None
    for candidate in (raw, raw[:23]):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
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


def project_dir_matches(candidate: Optional[str], wanted: str) -> bool:
    """Whether a session's ``project_dir`` matches a ``project_dir`` filter.

    Semantics (documented in ``docs/methods.md``): the filter matches a
    session whose ``project_dir`` is the SAME directory or a
    **descendant** of it (path-boundary aware — ``/home/u/dev/ai``
    never matches a ``/home/u/dev/ai-r`` session).  Rationale: "sessions
    of this project" must include sessions started in a subdirectory of
    the project root, while a plain prefix test would leak sibling
    directories that merely share a name prefix.

    A session without a ``project_dir`` signal (``None``/empty) never
    matches — absence of a signal is not a wildcard.  Trailing slashes
    on either side are ignored; no other normalisation (``~``, ``..``,
    symlinks) is applied — both sides are compared as recorded.
    """
    if not candidate:
        return False
    base = wanted.rstrip("/") or "/"
    cand = candidate.rstrip("/") or "/"
    if cand == base:
        return True
    prefix = "/" if base == "/" else base + "/"
    return cand.startswith(prefix)
