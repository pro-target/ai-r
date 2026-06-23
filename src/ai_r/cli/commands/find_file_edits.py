"""``ai-r find-file-edits`` subcommand handler."""

from __future__ import annotations

import argparse
import json
import sys

from ai_r.cli.shared import _exit_with_error


def _run_find_file_edits(args: argparse.Namespace) -> int:
    """Run the ``find-file-edits`` subcommand.

    Delegates the actual scan to :mod:`ai_r.find_file_edits`
    (the same core the MCP tool uses) and renders either a
    human-readable summary or a JSON blob.
    """
    from ai_r.find_file_edits import find_file_edits as _ffe_core

    try:
        result = _ffe_core(
            path=args.path,
            agent=args.agent,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    except ValueError as exc:
        return _exit_with_error(str(exc), code=2)

    records = result["records"]

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not records:
        print("(no edits found)", file=sys.stderr)
        return 0

    for r in records:
        ts = r.get("timestamp") or r.get("session_date") or "?"
        print(
            f"[{ts}] {r['agent']}/{r['session_uuid'][:8]} "
            f"{r['tool']} {r['file']}"
        )
        if r.get("intent"):
            first = r["intent"].splitlines()[0][:120]
            print(f"    intent: {first}")

    suffix = " (truncated)" if result["truncated"] else ""
    print(f"\n{result['count']} edit(s){suffix}.")
    return 0
