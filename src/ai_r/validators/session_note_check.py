"""Session-note template validator.

The canonical session-note template lives in
``src/ai_r/templates/session_note.md``.  The required-sections list
is read from that template at runtime so the prompt and the validator
cannot drift apart — adding or removing a required section in the
template is the only edit needed.

Layer 4 — session_id authenticity
---------------------------------

When a note carries a ``## Session`` section with a ``Session ID:``
line, the validator re-derives the running session_id from the cascade
(see :mod:`ai_r.session`) and reports any mismatch.  The cascade
returns a *list* of :class:`~ai_r.session.SessionCandidate` so the
validator can match against any one of them — the typical use case
involves a single running agent, but parallel sessions (e.g. a
side-by-side ``codex`` + ``claude`` invocation) are first-class.

Error taxonomy:

* ``"mismatch"`` — declared id matches no candidate AND at least one
  candidate exists.  Hard FAIL.
* ``"unverifiable"`` — declared id is set, no candidates at all.
  Soft WARN (caller decides whether to FAIL).
* ``"unverifiable-ambiguous"`` — declared id matches no candidate but
  multiple candidates exist.  Hard FAIL (something is wrong).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ai_r.parsers.models import AgentName
from ai_r.session import (
    SessionCandidate,
    _is_valid_session_id,
    detect_session_candidates,
)

__all__ = [
    "SessionNoteValidationResult",
    "extract_session_id_from_note",
    "parse_required_sections",
    "validate_session_note",
    "validate_session_note_with_identity",
]


_HEADING_RE = re.compile(r"^## (.+)$")
_REQUIRED_MARKER = "<!-- required:"
_SESSION_SECTION_RE = re.compile(r"^##\s+Session\b", re.IGNORECASE)
_SESSION_ID_LINE_RE = re.compile(
    r"^\*?\*?Session\s*ID:?\s*\*?\*?\s*(.+?)\s*$",
    re.IGNORECASE,
)


_DEFAULT_TEMPLATE = (
    Path(__file__).parent.parent / "templates" / "session_note.md"
)


def parse_required_sections(template_path: Path) -> list[str]:
    """Return the list of section names marked ``required`` in the template.

    A section is considered required when its ``## <name>`` heading is
    followed (anywhere in the body up to the next ``##`` heading) by a
    comment of the form ``<!-- required: ... -->``.
    """
    text = template_path.read_text(encoding="utf-8")
    required: list[str] = []
    current: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current is not None and _REQUIRED_MARKER in "\n".join(current_body):
                required.append(current)
            current = m.group(1).strip()
            current_body = []
        elif current is not None:
            current_body.append(line)
    if current is not None and _REQUIRED_MARKER in "\n".join(current_body):
        required.append(current)
    return required


def _section_body_map(text: str) -> dict[str, str]:
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current is not None:
                bodies[current] = "\n".join(current_body)
            current = m.group(1).strip()
            current_body = []
        elif current is not None:
            current_body.append(line)
    if current is not None:
        bodies[current] = "\n".join(current_body)
    return bodies


def _clean_session_id_value(raw: str) -> str:
    return raw.strip().strip("`").strip()


def extract_session_id_from_note(note_path: Path) -> Optional[str]:
    """Return the ``Session ID`` declared in the note, or ``None``.

    Locates the first ``## Session`` heading (also matches
    ``## Session Identity`` / ``## Session Metadata`` / etc.) and parses
    the first line of the form ``Session ID: <value>`` within that
    section's body.  Tolerant of markdown bold (``**Session ID:**``)
    and inline backticks.  Returns ``None`` when no ``## Session``
    section is present, when no ``Session ID:`` line is found, or when
    the value fails ``session._is_valid_session_id`` validation.
    """
    text = note_path.read_text(encoding="utf-8")
    bodies = _section_body_map(text)
    session_body: Optional[str] = None
    for name, body in bodies.items():
        if _SESSION_SECTION_RE.match(f"## {name}"):
            session_body = body
            break
    if session_body is None:
        return None
    for line in session_body.splitlines():
        m = _SESSION_ID_LINE_RE.match(line.strip())
        if not m:
            continue
        value = _clean_session_id_value(m.group(1))
        if _is_valid_session_id(value):
            return value
    return None


def _missing_sections_for_note(
    note_path: Path, template_path: Path
) -> list[str]:
    template = template_path or _DEFAULT_TEMPLATE
    required = parse_required_sections(template)
    note_text = note_path.read_text(encoding="utf-8")
    note_bodies = _section_body_map(note_text)
    missing: list[str] = []
    for name in required:
        body = note_bodies.get(name)
        if body is None:
            missing.append(name)
            continue
        stripped = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
        if not stripped:
            missing.append(name)
    return missing


def validate_session_note(
    note_path: Path,
    template_path: Path | None = None,
) -> list[str]:
    """Return the list of required sections that are missing or empty.

    A section counts as missing when its ``## <name>`` heading is
    absent from the note.  A section counts as empty when the heading
    is present but the body (after stripping required-marker comments)
    is blank.

    Backward-compat shim — prefer
    :func:`validate_session_note_with_identity` when you also want the
    Layer-4 ``Session ID`` authenticity check.
    """
    return _missing_sections_for_note(note_path, template_path or _DEFAULT_TEMPLATE)


@dataclass(frozen=True)
class SessionNoteValidationResult:
    """Outcome of the full template + identity check.

    Attributes
    ----------
    missing_sections:
        Required ``## <name>`` sections that are absent or empty.
    declared_session_id:
        Value of ``Session ID:`` parsed from the note, or ``None`` if
        the note makes no claim.
    actual_session_id:
        First candidate's ``session_id`` returned by the cascade, or
        ``None`` when the cascade yields nothing.  Kept for
        backward-compat with single-id consumers; prefer ``candidates``
        for the full picture.
    actual_source:
        First candidate's ``source`` label.
    actual_agent:
        First candidate's :class:`AgentName`, or ``None``.
    identity_ok:
        ``True`` when the note makes no claim (declared is None) or
        when the claim matches ANY candidate.  ``False`` on mismatch
        or when a claim cannot be verified.
    errors:
        Human-readable descriptions of every identity violation
        (``mismatch``, ``unverifiable``, ``unverifiable-ambiguous``).
        Empty when ``identity_ok`` is True.  Missing-section issues
        live in ``missing_sections`` instead.
    candidates:
        Full :class:`~ai_r.session.SessionCandidate` list from
        the detection cascade — for caller introspection (rendering,
        fingerprint lookup, etc.).
    ambiguous:
        ``True`` when more than one candidate exists.  Suggests
        parallel sessions; the caller may want to require a
        fingerprint match or fail closed.
    """

    missing_sections: list[str] = field(default_factory=list)
    declared_session_id: Optional[str] = None
    actual_session_id: Optional[str] = None
    actual_source: Optional[str] = None
    actual_agent: Optional[AgentName] = None
    identity_ok: bool = True
    errors: list[str] = field(default_factory=list)
    candidates: list[SessionCandidate] = field(default_factory=list)
    ambiguous: bool = False


def _first_candidate_values(
    candidates: list[SessionCandidate],
) -> tuple[Optional[str], Optional[str], Optional[AgentName]]:
    if not candidates:
        return None, None, None
    first = candidates[0]
    return first.session_id, first.source, first.agent


def validate_session_note_with_identity(
    note_path: Path,
    template_path: Path | None = None,
) -> SessionNoteValidationResult:
    """Run the full template + identity check on a session note.

    Combines the legacy missing-sections check with the Layer-4
    ``Session ID`` authenticity check.  Contract:

    * **No claim** (no ``## Session`` section, or no ``Session ID:``
      line): identity is a no-op — ``identity_ok`` is ``True`` and
      ``errors`` is empty regardless of what the cascade returns.
    * **Match** (declared matches any candidate's ``session_id``):
      ``identity_ok`` is ``True``, ``errors`` is empty.
    * **Mismatch, single candidate**: ``identity_ok`` is ``False``,
      ``errors`` contains ``"mismatch"`` plus the concrete values.
    * **Mismatch, multiple candidates**: ``identity_ok`` is ``False``,
      ``errors`` contains ``"unverifiable-ambiguous"``.
    * **Unverifiable** (claim present, cascade empty):
      ``identity_ok`` is ``False``, ``errors`` contains
      ``"unverifiable"``.

    The function never raises on identity failures — it reports them.
    The caller decides whether to FAIL (e.g. exit 2) or WARN.
    """
    missing = _missing_sections_for_note(
        note_path, template_path or _DEFAULT_TEMPLATE
    )
    declared = extract_session_id_from_note(note_path)
    candidates = detect_session_candidates()
    actual, source, agent = _first_candidate_values(candidates)

    if declared is None:
        identity_ok = True
        errors: list[str] = []
    else:
        matches = [c for c in candidates if c.session_id == declared]
        if matches:
            identity_ok = True
            errors = []
        elif not candidates:
            identity_ok = False
            errors = [
                f"unverifiable: session_id declared in note ({declared}) "
                "but no candidates surfaced from the detection cascade"
            ]
        elif len(candidates) > 1:
            identity_ok = False
            actual_ids = ", ".join(c.session_id for c in candidates)
            errors = [
                f"unverifiable-ambiguous: session_id declared in note "
                f"({declared}) matches none of the {len(candidates)} "
                f"candidates ({actual_ids})"
            ]
        else:
            identity_ok = False
            cand = candidates[0]
            errors = [
                f"mismatch: session_id declared in note ({declared}) does not "
                f"match the single detected candidate ({cand.session_id}, "
                f"source={cand.source})"
            ]

    return SessionNoteValidationResult(
        missing_sections=missing,
        declared_session_id=declared,
        actual_session_id=actual,
        actual_source=source,
        actual_agent=agent,
        identity_ok=identity_ok,
        errors=errors,
        candidates=candidates,
        ambiguous=len(candidates) > 1,
    )
