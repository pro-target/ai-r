"""User-attached references — the ``user_ref`` extraction vocabulary.

A *user reference* is an external data source the USER attached inside their
own turn — a link, a file path, an @-mention, an image/attachment, or an
IDE-injected file-context tag.  It points at information that is NOT in the
chat text itself, so a downstream consumer can notice "the user handed me an
external source here" and decide whether to fetch it — and, because anything
outside the session is untrusted, wrap it via
:func:`ai_r.security.sanitize_session_text` before trusting it.

ai-r only *marks* the reference; it never fetches or sanitizes the target —
that trust decision belongs to the consumer (see ``docs/security.md``).

This is a **leaf module**: it depends only on the stdlib, so it can be
imported by the parsers, the event layer, and the ``network`` preset alike
without creating an intra-package import cycle (the parsers must not import
``ai_r.events``; the event layer imports the parsers).
"""

from __future__ import annotations

import re
from typing import List, Optional

__all__ = [
    "URL_IN_TEXT_RE",
    "USER_REF_KIND",
    "USER_REF_ORIGIN",
    "make_user_ref",
    "extract_user_refs_from_text",
    "dedup_user_refs",
]

# First http(s) URL embedded in free text.  Conservative charset: stop at
# whitespace / quotes / brackets.  Canonical home for the pattern — the
# ``network`` preset imports it from here so both share one vocabulary.
URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\)\]]+", re.IGNORECASE)

# The closed vocabulary of user-reference kinds.  A new DIMENSION over the
# existing ``Event.refs`` taxonomy (mirrors ``TOOL_KIND``), NOT a second
# classifier.  ``ide_context`` covers IDE-injected ``<ide_*>`` tags (the IDE
# put them there — weaker than a deliberate ``<doc>``/@-mention), so a
# consumer can tell "the user attached this" from "the editor added this".
USER_REF_KIND = frozenset({"file", "url", "image", "attachment", "ide_context"})

# ``structured`` = pulled from a distinct content block/part (the user
# definitely attached it); ``text`` = extracted from the prose (a URL, an
# @-mention, or an IDE tag) — a weaker, best-effort signal.
USER_REF_ORIGIN = frozenset({"structured", "text"})


def make_user_ref(kind: str, target: Optional[str], origin: str) -> dict:
    """Build one validated ``user_ref`` payload dict.

    ``kind`` must be in :data:`USER_REF_KIND` and ``origin`` in
    :data:`USER_REF_ORIGIN` — a bad value is a programmer error and raises
    ``ValueError`` (callers pass fixed literals).  ``target`` may be ``None``
    when the signal exists but carries no address (an inline image with no
    filename) — absence is honest, never fabricated.
    """
    if kind not in USER_REF_KIND:
        raise ValueError(f"unknown user_ref kind: {kind!r}")
    if origin not in USER_REF_ORIGIN:
        raise ValueError(f"unknown user_ref origin: {origin!r}")
    return {"kind": kind, "target": target, "origin": origin}


# --- Text-embedded reference patterns -------------------------------------
# Claude expands an @-mention of a file into a ``<doc path="…">`` block, and
# the IDE integration injects ``<ide_opened_file>`` / ``<ide_selection>``
# context tags carrying the file path in prose.  These are matched on the RAW
# text (tags may wrap code); bare URLs / @-mentions are matched AFTER fenced
# code is stripped (a URL inside a ``` block is a code sample, not a source).
_DOC_TAG_RE = re.compile(r"<doc\b[^>]*\bpath=\"([^\"]+)\"", re.IGNORECASE)
_IDE_OPENED_RE = re.compile(
    r"<ide_opened_file>\s*The user opened the file (.+?) in the IDE",
    re.IGNORECASE | re.DOTALL,
)
_IDE_SELECTION_RE = re.compile(
    r"<ide_selection>.*?\bfrom (\S+)", re.IGNORECASE | re.DOTALL
)
# Conservative @-mention: require a path shape (contains a ``/``) so bare
# ``@handle`` mentions do not masquerade as file references.
_AT_MENTION_RE = re.compile(r"(?:^|\s)@([\w.~+-]*/[\w./~+-]+)")

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)


def _strip_fenced_code(text: str) -> str:
    """Blank out fenced ``` … ``` blocks so code samples don't yield refs."""
    return _FENCED_CODE_RE.sub(" ", text)


def extract_user_refs_from_text(text: Optional[str]) -> List[dict]:
    """Extract ``origin="text"`` user references from a user turn's prose.

    Returns a list of :func:`make_user_ref` dicts — ``<doc path>`` and
    ``<ide_*>`` tags (matched on raw text), plus bare http(s) URLs and
    path-shaped @-mentions (matched after fenced code is stripped).  Empty
    for empty/non-string input.  Structured attachments (image/file parts)
    are transported separately via ``Message.user_refs`` and are NOT this
    function's concern.
    """
    if not text or not isinstance(text, str):
        return []
    refs: List[dict] = []
    for m in _DOC_TAG_RE.finditer(text):
        refs.append(make_user_ref("file", m.group(1), "text"))
    for m in _IDE_OPENED_RE.finditer(text):
        refs.append(make_user_ref("ide_context", m.group(1).strip(), "text"))
    for m in _IDE_SELECTION_RE.finditer(text):
        refs.append(make_user_ref("ide_context", m.group(1).strip(), "text"))
    scan = _strip_fenced_code(text)
    for m in URL_IN_TEXT_RE.finditer(scan):
        refs.append(make_user_ref("url", m.group(0), "text"))
    for m in _AT_MENTION_RE.finditer(scan):
        refs.append(make_user_ref("file", m.group(1), "text"))
    return refs


def dedup_user_refs(refs: List[dict]) -> List[dict]:
    """Collapse duplicate references by normalized ``target``.

    When the same target surfaces twice (e.g. a path both as a structured
    part and mentioned in prose), keep ONE — preferring ``origin="structured"``
    (the stronger signal).  Order is preserved.  Refs with no ``target``
    (unnamed inline images) are all kept — they cannot be de-duplicated.
    """
    out: List[dict] = []
    by_target: dict = {}
    for r in refs:
        target = r.get("target")
        if not target:
            out.append(r)
            continue
        norm = target.strip()
        if norm in by_target:
            idx = by_target[norm]
            if out[idx].get("origin") == "text" and r.get("origin") == "structured":
                out[idx] = r
            continue
        by_target[norm] = len(out)
        out.append(r)
    return out
