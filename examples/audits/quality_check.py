#!/usr/bin/env python3
"""Quality check: surface quality signals from a session.

Counts three families of signals across the message stream:

- **lint/format runs** — tool names/inputs matching linters and formatters
  (``eslint``, ``ruff``, ``black``, ``prettier``, ``mypy``, ``flake8``,
  ``pyright``, ``tsc``, ``rubocop``).
- **error->retry loops** — tool-result content carrying error signatures
  (``Traceback``, ``Error:``, ``FAILED``, ``error TS``), followed by a
  subsequent tool_use (i.e. the agent tried again).
- **explicit verification steps** — tool names/inputs matching
  ``verify``, ``--check``, ``dry-run``, ``pytest``, ``test``.

Same spirit as ``git-auditor``'s ``scout_behavioral_audit``
(``~/.agents/skills/git-auditor/scripts/context_auditor.py``, lines
~393-477), extended to a broader quality-signal pass and driven through
the public ``ai_r.read_messages`` API.

Usage::

    python examples/audits/quality_check.py <uuid> --agent claude

Exit code 0 (informational) unless session parsing fails.

This is a template.  Tune the signal vocabularies and thresholds to your
team's definition of "good session".
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Callable, Dict, List

from ai_r.parsers import (
    antigravity,
    claude,
    codex,
    opencode,
    pi,
)
from ai_r.parsers.models import Message


PARSERS: Dict[str, Callable] = {
    "claude": claude,
    "codex": codex,
    "opencode": opencode,
    "antigravity": antigravity,
    "pi": pi,
}

LINT_PATTERNS: List[tuple] = [
    ("eslint", re.compile(r"\beslint\b")),
    ("ruff", re.compile(r"\bruff\b")),
    ("black", re.compile(r"\bblack\b")),
    ("prettier", re.compile(r"\bprettier\b")),
    ("mypy", re.compile(r"\bmypy\b")),
    ("flake8", re.compile(r"\bflake8\b")),
    ("pyright", re.compile(r"\bpyright\b")),
    ("tsc", re.compile(r"\btsc\b")),
    ("rubocop", re.compile(r"\brubocop\b")),
]

ERROR_PATTERNS: List[tuple] = [
    ("Traceback", re.compile(r"\btraceback\b")),
    ("Error:", re.compile(r"\berror:")),
    ("FAILED", re.compile(r"\bfailed\b")),
    ("error TS", re.compile(r"\berror\s+ts\d")),
]

VERIFY_PATTERNS: List[tuple] = [
    ("verify", re.compile(r"\bverify\b")),
    ("--check", re.compile(r"--check\b")),
    ("dry-run", re.compile(r"\bdry[- ]?run\b")),
    ("pytest", re.compile(r"\bpytest\b")),
    ("test", re.compile(r"\btest\b")),
]


def _count_signals(
    messages: List[Message], patterns: List[tuple]
) -> Dict[str, int]:
    """Count pattern matches across text + tool_use name/input surfaces."""
    counts: Dict[str, int] = {label: 0 for label, _ in patterns}
    for msg in messages:
        hay = msg.text.lower()
        for tool in msg.tool_use:
            name = (tool.get("name", "") or "").lower()
            inp = (tool.get("input", "") or "").lower()
            hay += "\n" + name + "\n" + inp
        for label, pattern in patterns:
            # Use findall so repeated invocations register, not just presence.
            counts[label] += len(pattern.findall(hay))
    return counts


def count_error_retries(messages: List[Message]) -> int:
    """Count error-bearing tool_results followed by a later tool_use.

    A retry is defined as: a message with a ``tool_result.content`` that
    matches an error signature, with at least one ``tool_use`` entry in
    a strictly later message.
    """
    # First, indices of messages carrying an error signature.
    error_indices: List[int] = []
    for idx, msg in enumerate(messages):
        for res in msg.tool_result:
            content = (res.get("content", "") or "").lower()
            if any(pat.search(content) for _, pat in ERROR_PATTERNS):
                error_indices.append(idx)
                break
    if not error_indices:
        return 0
    last_error = error_indices[-1]
    # Any tool_use strictly after the last error counts as a retry attempt.
    retries = 0
    for msg in messages[last_error + 1 :]:
        if msg.tool_use:
            retries += len(msg.tool_use)
    return retries


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Surface quality signals from a past session.",
    )
    parser.add_argument("uuid", help="Session uuid to audit.")
    parser.add_argument(
        "--agent",
        required=True,
        choices=sorted(PARSERS.keys()),
        help="Which agent produced the session.",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Optional base_dir pass-through to read_messages (testing).",
    )
    args = parser.parse_args(argv)

    read_messages = PARSERS[args.agent].read_messages
    try:
        messages = read_messages(args.uuid, base_dir=args.base_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: could not read session {args.uuid!r}: {exc}", file=sys.stderr)
        return 2

    print(f"Quality signals for agent={args.agent} uuid={args.uuid}")
    print(f"  messages scanned: {len(messages)}")

    lint_counts = _count_signals(messages, LINT_PATTERNS)
    lint_total = sum(lint_counts.values())
    print(f"  lint/format runs: {lint_total}")
    for label, count in lint_counts.items():
        if count:
            print(f"      {label}: {count}")

    retries = count_error_retries(messages)
    print(f"  error->retry loops: {retries}")

    verify_counts = _count_signals(messages, VERIFY_PATTERNS)
    verify_total = sum(verify_counts.values())
    print(f"  verification steps: {verify_total}")
    for label, count in verify_counts.items():
        if count:
            print(f"      {label}: {count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
