#!/usr/bin/env python3
"""Completeness check: task drift / coverage.

For each changed file, checks whether its basename appears anywhere in the
assistant's output (``text`` or any ``tool_use`` input).  Mirrors the
plan-alignment half of ``git-auditor``'s ``scout_behavioral_audit``
(``~/.agents/skills/git-auditor/scripts/context_auditor.py``, lines
~460-477), which asks "do the changed files appear in the agent's
reasoning?" — here done via the public ``ai_reader.read_messages`` API.

The file list comes from repeated ``--file`` flags or from
``--from-git`` (runs ``git diff --cached --name-only``).

Usage::

    python examples/audits/completeness_check.py <uuid> --agent claude \\
        --file src/foo.py --file tests/test_foo.py
    python examples/audits/completeness_check.py <uuid> --agent claude --from-git

Exit code 0 if every file is mentioned, 1 if any are missing (potential
drift).

This is a template.  Tighten the matching (require path, not basename;
restrict to assistant messages; ignore generated files) to taste.
"""

from __future__ import annotations

import argparse
import os
import subprocess
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


PARSERS: Dict[str, Callable] = {
    "claude": claude,
    "codex": codex,
    "opencode": opencode,
    "antigravity": antigravity,
    "pi": pi,
}


def files_from_git() -> List[str]:
    """Return staged file paths via ``git diff --cached --name-only``."""
    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(
            f"error: git diff failed (exit {res.returncode}): {res.stderr.strip()}",
            file=sys.stderr,
        )
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def assistant_haystack(messages: List[Message]) -> str:
    """Lowercased concatenation of assistant text + tool_use inputs.

    Only assistant messages are considered: the question is whether the
    agent reasoned about the files it touched, not whether the user named
    them.
    """
    chunks: List[str] = []
    for msg in messages:
        if msg.role != "assistant":
            continue
        if msg.text:
            chunks.append(msg.text)
        for tool in msg.tool_use:
            inp = tool.get("input", "") or ""
            chunks.append(inp)
    return "\n".join(chunks).lower()


def check_files(files: List[str], messages: List[Message]) -> tuple:
    """Return (matched, unmatched) lists of file paths."""
    haystack = assistant_haystack(messages)
    matched: List[str] = []
    unmatched: List[str] = []
    for path in files:
        basename = os.path.basename(path).lower()
        # An empty basename (e.g. trailing slash) is meaningless; skip it.
        if not basename:
            continue
        if basename in haystack:
            matched.append(path)
        else:
            unmatched.append(path)
    return matched, unmatched


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Task drift / coverage: are changed files mentioned in the session?",
    )
    parser.add_argument("uuid", help="Session uuid to audit.")
    parser.add_argument(
        "--agent",
        required=True,
        choices=sorted(PARSERS.keys()),
        help="Which agent produced the session.",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="Changed file to check (repeatable).",
    )
    parser.add_argument(
        "--from-git",
        action="store_true",
        help="Populate the file list from `git diff --cached --name-only`.",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Optional base_dir pass-through to read_messages (testing).",
    )
    args = parser.parse_args(argv)

    files: List[str] = list(args.file)
    if args.from_git:
        files.extend(files_from_git())
    if not files:
        print("error: no files to check (pass --file or --from-git)", file=sys.stderr)
        return 2

    read_messages = PARSERS[args.agent].read_messages
    try:
        messages = read_messages(args.uuid, base_dir=args.base_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: could not read session {args.uuid!r}: {exc}", file=sys.stderr)
        return 2

    matched, unmatched = check_files(files, messages)
    total = len(matched) + len(unmatched)
    print(f"Coverage: {len(matched)}/{total} changed files mentioned in session.")
    if unmatched:
        print("unmatched (potential drift):")
        for path in unmatched:
            print(f"  - {path}")
        return 1
    print("all changed files appear in the session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
