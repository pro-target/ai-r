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

from datetime import datetime
from typing import Optional


# Maximum number of characters retained in a normalised session title.
_TITLE_MAX_LEN = 100


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
