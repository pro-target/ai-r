"""Secret redaction at the emission boundary (F2.1).

ai-r reads RAW session transcripts, and real transcripts routinely contain
pasted secrets (API keys, tokens, passwords, private keys).  Every public
method that emits session-derived text masks those secrets **on output** by
default; ``redact=False`` returns the raw content.

Design rules (see ``docs/methods.md`` → *Redaction*):

* **Emission-time only.**  Redaction never touches scanning or matching:
  filters (``text`` / ``input_contains`` / search queries …) match the RAW
  stored text; only the returned payload is masked.  A ``[REDACTED_*]``
  placeholder therefore never exists in stored session text and can never
  match as a search term.
* **One pass, compiled once.**  All patterns are combined into a single
  compiled alternation (:data:`_COMBINED`); each emitted string is scanned
  exactly once.  Redaction runs on the already char-capped output fields, so
  the cost is bounded by the response size, not the corpus size.
* **Replacement is ``[REDACTED_<TYPE>]``.**  Patterns with a ``<TYPE>_v``
  value group replace only the secret *value* span (the key name /
  ``Bearer`` prefix / URL scheme survive so the output stays readable).
* **Bias against false positives.**  Value-shaped patterns require at least
  one digit (``sk-learn-pipeline`` / ``token = tokenize`` never trip);
  the generic ``key=value`` catch-all only fires with an explicit
  secret-ish key name followed by ``:`` or ``=``.  The honest trade-off:
  an all-letter password assigned to a generic key is NOT masked.

Alternation order matters: at the same start position the FIRST alternative
wins, so vendor-specific tokens sit above the generic catch-all.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

__all__ = [
    "REDACTION_MARKER_PREFIX",
    "REDACTION_TYPES",
    "merge_redaction_counts",
    "redact_text",
    "redact_value",
    "secret_like_types",
]

# Prefix every replacement token starts with — also the cheap probe for
# "this filter value is a redaction placeholder" in empty-result diagnostics.
REDACTION_MARKER_PREFIX = "[REDACTED_"

# --- pattern table ----------------------------------------------------------
# ``(TYPE, fragment)`` pairs, combined below into ONE compiled alternation.
# Inner groups must be non-capturing except the optional ``<TYPE>_v`` value
# group: when present, only that span is replaced.
_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # PEM private key blocks (RSA/EC/OPENSSH/PGP...).  Requires the END
    # marker: a lone BEGIN header without a body is not a leaked key.
    (
        "PRIVATE_KEY",
        r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S]{0,20000}?"
        r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----",
    ),
    # AWS access key id: fixed 4-char prefix + 16 uppercase/digits.
    ("AWS_KEY", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    # AWS secret access key: an aws-ish key name assigned a 40-char base64
    # value (the canonical secret-key length).
    (
        "AWS_SECRET",
        r"(?i:\baws[a-z0-9_\- ]{0,24}?['\"]?[ \t]*[:=][ \t]*['\"]?)"
        r"(?P<AWS_SECRET_v>[A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])",
    ),
    # GitHub tokens: classic ghp_/gho_/ghu_/ghs_/ghr_ and fine-grained PATs.
    (
        "GITHUB_TOKEN",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,251}"
        r"|github_pat_[A-Za-z0-9_]{22,255})\b",
    ),
    # GitLab personal access tokens.
    (
        "GITLAB_TOKEN",
        r"\bglpat-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_\-])",
    ),
    # Anthropic keys (before OPENAI: both start with ``sk-``; the first
    # alternative wins at the same position).
    (
        "ANTHROPIC_KEY",
        r"\bsk-ant-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{16,}(?![A-Za-z0-9_\-])",
    ),
    # OpenAI keys.  The digit lookahead keeps prose like
    # ``sk-learn-pipeline-tuning`` out.
    (
        "OPENAI_KEY",
        r"\bsk-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_\-])",
    ),
    # Slack tokens (bot/app/legacy...).
    (
        "SLACK_TOKEN",
        r"\bxox[baprs]-(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{10,}(?![A-Za-z0-9\-])",
    ),
    # ``user:password@`` inside a URL with an explicit scheme.  Only the
    # credentials span is replaced — scheme, host and path survive.
    (
        "URL_CREDENTIALS",
        r"(?<=://)(?P<URL_CREDENTIALS_v>[^\s/:@'\"]{1,64}:[^\s/@'\"]{1,256})(?=@)",
    ),
    # ``Authorization: Bearer <token>`` values.  Digit lookahead keeps
    # prose like ``Bearer authentication`` out.
    (
        "BEARER_TOKEN",
        r"(?i:\bbearer[ \t]+)"
        r"(?P<BEARER_TOKEN_v>(?=[A-Za-z0-9\-._~+/]*\d)[A-Za-z0-9\-._~+/]{16,}=*)",
    ),
    # Generic ``<secret-ish key> = <value>`` catch-all.  Fires only with an
    # explicit key name + ``:``/``=``; the value needs >= 8 chars of a
    # token-ish charset INCLUDING at least one digit (see module docstring).
    (
        "GENERIC_SECRET",
        r"(?i:\b(?:api[_-]?key|apikey|api[_-]?secret|secret[_-]?key"
        r"|client[_-]?secret|access[_-]?token|auth[_-]?token"
        r"|refresh[_-]?token|session[_-]?token|private[_-]?key"
        r"|password|passwd|pwd|secret|token|credentials?)\b"
        r"['\"]?[ \t]*[:=][ \t]*['\"]?)"
        r"(?P<GENERIC_SECRET_v>(?=[^\s'\",;]*\d)[A-Za-z0-9+/_\-=.~!@#$%^&*]{8,})",
    ),
)

REDACTION_TYPES: Tuple[str, ...] = tuple(t for t, _ in _PATTERNS)

# Types whose pattern carries a ``<TYPE>_v`` value group (replace value only).
_VALUE_GROUPS = frozenset(t for t, p in _PATTERNS if f"(?P<{t}_v>" in p)

_COMBINED = re.compile("|".join(f"(?P<{t}>{p})" for t, p in _PATTERNS))


def _replace(match: "re.Match[str]", counts: Dict[str, int]) -> str:
    """Replacement callback: count the matched TYPE, emit its token."""
    for t in REDACTION_TYPES:
        if match.group(t) is None:
            continue
        counts[t] = counts.get(t, 0) + 1
        token = f"{REDACTION_MARKER_PREFIX}{t}]"
        if t in _VALUE_GROUPS:
            vs, ve = match.span(f"{t}_v")
            if vs != -1:
                s = match.start()
                whole = match.group(0)
                return whole[: vs - s] + token + whole[ve - s:]
        return token
    return match.group(0)  # pragma: no cover — unreachable


def redact_text(text: object) -> Tuple[object, Dict[str, int]]:
    """Redact secrets in ``text``; return ``(redacted, counts_by_type)``.

    Non-string / empty input is returned unchanged with empty counts, so
    callers can pass optional fields without guarding.  A clean string
    costs exactly one pass of the combined pattern.
    """
    if not isinstance(text, str) or not text:
        return text, {}
    counts: Dict[str, int] = {}
    return _COMBINED.sub(lambda m: _replace(m, counts), text), counts


def redact_value(value: Any) -> Tuple[Any, Dict[str, int]]:
    """Recursively redact every string leaf of ``value`` (dict/list/str).

    Dicts and lists are rebuilt (values redacted, keys untouched); tuples
    come back as lists (JSON-safe).  Non-container, non-string leaves pass
    through unchanged.  Returns ``(redacted_value, counts_by_type)``.
    """
    counts: Dict[str, int] = {}
    return _redact_into(value, counts), counts


def _redact_into(value: Any, counts: Dict[str, int]) -> Any:
    if isinstance(value, str):
        if not value:
            return value
        return _COMBINED.sub(lambda m: _replace(m, counts), value)
    if isinstance(value, dict):
        return {k: _redact_into(v, counts) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_into(v, counts) for v in value]
    return value


def merge_redaction_counts(dst: Dict[str, int], src: Dict[str, int]) -> None:
    """Fold ``src`` counts into ``dst`` in place (missing keys created)."""
    for key, num in src.items():
        dst[key] = dst.get(key, 0) + num


def secret_like_types(text: object) -> List[str]:
    """Return the sorted redaction TYPEs that ``text`` itself would trip.

    Used by empty-result diagnostics: a search/filter value that *looks
    like a secret* earns a redaction hint.  Cheap — one combined-pattern
    pass over a (short) filter string.
    """
    _, counts = redact_text(text)
    return sorted(counts)
