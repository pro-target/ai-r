#!/usr/bin/env python3
"""Dry-run check: did the agent actually RUN tests, or just claim to?

Scans every ``Message``'s ``tool_use`` entries (``name`` + ``input``) and
``text`` for test-runner command patterns.  Mirrors the regex set used by
``git-auditor``'s ``scout_behavioral_audit``
(``~/.agents/skills/git-auditor/scripts/context_auditor.py``, lines
~393-477), but goes through the clean public ``ai_reader.read_messages``
API instead of shelling out to the legacy ``agent-audit.py`` wrapper.

Usage::

    python examples/audits/dry_run_check.py <uuid> --agent claude
    python examples/audits/dry_run_check.py <uuid> --agent codex --base-dir /tmp/sessions

Exit code 0 if at least one test execution is detected, 1 otherwise.

This is a template.  Adapt the runner patterns and the evidence threshold
to your team's bar.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Callable, Dict, List

from ai_reader.parsers import (
    antigravity,
    claude,
    codex,
    opencode,
    pi,
)
from ai_reader.parsers.models import Message


# agent name (lowercase) -> module exposing read_messages(uuid, base_dir=None)
PARSERS: Dict[str, Callable] = {
    "claude": claude,
    "codex": codex,
    "opencode": opencode,
    "antigravity": antigravity,
    "pi": pi,
}

# Same intent as git-auditor's test_patterns list, extended with a couple
# of common runners for completeness.  Patterns are matched case-insensitively
# against the lowercased haystack.
TEST_RUNNER_PATTERNS: List[tuple] = [
    ("phpunit", re.compile(r"\bphpunit\b")),
    ("artisan test", re.compile(r"\bartisan\s+test\b")),
    ("pytest", re.compile(r"\bpytest\b")),
    ("jest", re.compile(r"\bjest\b")),
    ("vitest", re.compile(r"\bvitest\b")),
    ("playwright", re.compile(r"\bplaywright\b")),
    ("npm test", re.compile(r"\bnpm\s+(?:run\s+)?test\b")),
    ("yarn test", re.compile(r"\byarn\s+test\b")),
    ("pnpm test", re.compile(r"\bpnpm\s+test\b")),
    ("go test", re.compile(r"\bgo\s+test\b")),
    (
        "docker exec ... test",
        re.compile(r"\bdocker(?:-compose)?\s+exec\b.*\b(?:phpunit|test|pytest)\b"),
    ),
]


def _iter_haystacks(messages: List[Message]):
    """Yield (source_label, haystack) tuples for every searchable surface.

    Covers assistant text, tool-use names and their serialised inputs, and
    tool-result content.  ``source_label`` identifies what matched so the
    report can name the tool that invoked the runner.
    """
    for msg in messages:
        if msg.text:
            yield ("text", msg.text.lower())
        for tool in msg.tool_use:
            name = tool.get("name", "") or ""
            inp = tool.get("input", "") or ""
            yield (f"tool_use[{name}].name", name.lower())
            yield (f"tool_use[{name}].input", inp.lower())
        for res in msg.tool_result:
            content = res.get("content", "") or ""
            yield ("tool_result.content", content.lower())


def find_test_runs(messages: List[Message]) -> List[tuple]:
    """Return a list of (runner_label, source_label) for each hit."""
    hits: List[tuple] = []
    seen: set = set()
    for source_label, haystack in _iter_haystacks(messages):
        for label, pattern in TEST_RUNNER_PATTERNS:
            if pattern.search(haystack):
                key = (label, source_label)
                if key not in seen:
                    seen.add(key)
                    hits.append((label, source_label))
    return hits


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run check: confirm test execution in a session.",
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

    print(f"Scanned {len(messages)} messages from agent={args.agent} uuid={args.uuid}")
    hits = find_test_runs(messages)
    if not hits:
        print("no test execution detected")
        print(
            "Looked for: "
            + ", ".join(label for label, _ in TEST_RUNNER_PATTERNS)
        )
        return 1

    runners = sorted({label for label, _ in hits})
    print(f"test execution detected ({len(runners)} runner(s)):")
    for label in runners:
        sources = sorted({src for r, src in hits if r == label})
        print(f"  - {label}: matched in {', '.join(sources)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
