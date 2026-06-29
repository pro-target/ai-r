#!/usr/bin/env python3
"""File-frequency audit: which file got edited most, and from how many directions.

Groups the cross-agent ``find_file_edits`` stream by file path and reports,
per file:

- **edits** — number of recorded edit tool-calls touching the file.
- **sessions** — number of *distinct* sessions that edited it.
- **agents** — the distinct set of agents (claude / codex / opencode /
  antigravity / pi) that touched it.
- **distinct-intents** — number of *distinct* triggering user requests
  (de-duplicated; ``None`` intents are ignored).

This is the aggregation half of the "WHY-audit": ``find-file-edits`` already
carries the *request behind each edit* (``intent``); this script rolls the
flat record stream up by file so "what got the most attention, and from how
many agents / sessions / distinct requests?" becomes one number per file.

The same logic ships as a first-class command: ``ai-r file-frequency`` (and
the public :func:`ai_r.file_frequency.file_frequency` core, used here). This
script is a thin CLI front-end over that core, kept as a tunable template.

Usage::

    python examples/audits/file_frequency.py --top 8
    python examples/audits/file_frequency.py --top 5 --agent codex
    python examples/audits/file_frequency.py --since 2026-06-01 --until 2026-06-29

Exit code 0 (informational) unless argument validation fails.

This is a template. Tune the "all files" matcher (``--path``), the ranking
key, and the columns to taste.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from ai_r.file_frequency import file_frequency


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Which file got edited most, and from how many agents / sessions / distinct requests.",
    )
    parser.add_argument(
        "--path",
        default="/",
        help=(
            "Substring matched against edited file paths (find_file_edits "
            "requires a non-empty path). Default '/' matches absolute paths; "
            "pass e.g. 'src/' to scope, or '.' to also catch relative paths."
        ),
    )
    parser.add_argument(
        "--agent",
        choices=["claude", "codex", "opencode", "antigravity", "pi"],
        default=None,
        help="Restrict to a single agent (default: all).",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 lower bound (inclusive) on edit timestamp.",
    )
    parser.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="ISO 8601 upper bound (inclusive) on edit timestamp.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="How many top files to show. 0 = all (default: 8).",
    )
    args = parser.parse_args(argv)

    try:
        result = file_frequency(
            path=args.path,
            agent=args.agent,
            since=args.since,
            until=args.until,
            top=args.top,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"scanned: {result['total_sessions']} sessions with edits "
        f"· {result['total_agents']} agents · {result['total_edits']} edits "
        f"· {result['total_files']} distinct files "
        f"(path filter: {args.path!r})"
    )
    print()
    header = f"{'file':<48} {'edits':>5} {'sessions':>8} {'intents':>7}  agents"
    print(header)
    print("-" * len(header))
    for row in result["files"]:
        agents = ",".join(row["agents"])
        print(
            f"{row['file'][:48]:<48} {row['edits']:>5} "
            f"{row['sessions']:>8} {row['intents']:>7}  {agents}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
