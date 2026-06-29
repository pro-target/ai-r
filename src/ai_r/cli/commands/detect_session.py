"""``ai-r detect-session`` subcommand handler + its local helpers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

from ai_r.cli.shared import _AGENT_CHOICES, _exit_with_error
from ai_r.parsers import coerce_agent as _coerce_agent
from ai_r.session import (
    AmbiguousSessionError,
    SessionCandidate,
    detect_session_candidates,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``detect-session`` subcommand on ``subparsers``."""
    detect_session_p = subparsers.add_parser(
        "detect-session",
        help="Detect the current AI session id from env vars and flag files.",
    )
    detect_session_p.add_argument(
        "--agent",
        choices=_AGENT_CHOICES,
        help="Deprecated hint; cascade scans all agents regardless.",
    )
    detect_session_p.add_argument(
        "--quiet",
        action="store_true",
        help="Deprecated; ignored in list mode (use AI_SESSION_OUTPUT=first).",
    )
    detect_session_p.add_argument(
        "--json",
        action="store_true",
        help="Emit all candidates as a JSON array.",
    )
    detect_session_p.add_argument(
        "--count",
        action="store_true",
        help="Emit just the integer candidate count.",
    )
    detect_session_p.set_defaults(func=_run_detect_session)


def _format_candidate_line(cand: SessionCandidate) -> str:
    agent_str = cand.agent.value if cand.agent is not None else ""
    fp_str = cand.fingerprint if cand.fingerprint is not None else ""
    return (
        f"id={cand.session_id} agent={agent_str} source={cand.source} "
        f"verified={cand.verified} self={cand.is_self} fingerprint={fp_str}"
    )


def _candidate_to_dict(cand: SessionCandidate) -> dict[str, Any]:
    return {
        "id": cand.session_id,
        "agent": cand.agent.value if cand.agent is not None else "",
        "source": cand.source,
        "verified": cand.verified,
        "self": cand.is_self,
        "fingerprint": cand.fingerprint if cand.fingerprint is not None else "",
    }


def _pick_single(
    candidates: list[SessionCandidate], mode: str
) -> Optional[SessionCandidate]:
    if mode == "first":
        return candidates[0] if candidates else None
    if mode == "strict":
        if len(candidates) > 1:
            raise AmbiguousSessionError(
                f"ambiguous session_id: {len(candidates)} candidates"
            )
        return candidates[0] if candidates else None
    if mode == "self":
        for cand in candidates:
            if cand.is_self:
                return cand
        return None
    if mode.startswith("fingerprint:"):
        target = mode.split(":", 1)[1].strip()
        for cand in candidates:
            if cand.fingerprint == target:
                return cand
        return None
    raise ValueError(f"unknown AI_SESSION_OUTPUT mode {mode!r}")


def _resolve_output_mode() -> str:
    return (os.environ.get("AI_SESSION_OUTPUT", "list") or "list").strip().lower()


def _run_detect_session(args: argparse.Namespace) -> int:
    if getattr(args, "agent", None):
        try:
            _coerce_agent(args.agent)
        except ValueError as exc:
            return _exit_with_error(str(exc))
    candidates = detect_session_candidates()
    if getattr(args, "count", False):
        print(len(candidates))
        return 0
    if getattr(args, "json", False):
        json.dump(
            [_candidate_to_dict(c) for c in candidates],
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0
    mode = _resolve_output_mode()
    if mode == "list":
        if len(candidates) > 1:
            print(
                "ai-r: WARN: multiple session_id candidates; pass "
                "AI_SESSION_OUTPUT=strict|self|fingerprint:<hash> for "
                "disambiguation.",
                file=sys.stderr,
            )
        if not candidates:
            return _exit_with_error("could not detect current session id")
        for cand in candidates:
            print(_format_candidate_line(cand))
        return 0
    try:
        picked = _pick_single(candidates, mode)
    except AmbiguousSessionError as exc:
        return _exit_with_error(str(exc), code=2)
    except ValueError as exc:
        return _exit_with_error(str(exc))
    if picked is None:
        return _exit_with_error("could not detect current session id")
    print(f"session={picked.session_id}")
    return 0
