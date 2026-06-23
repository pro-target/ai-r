"""Legacy compatibility shim for ``ai-local-reader`` skill scripts.

The original ``~/.agents/skills/ai-local-reader/scripts/`` shipped two
fat CLI scripts (``get_latest_context.py`` and ``agent-audit.py``) that
exposed ~25 flags and a custom human-readable output format.  When the
``ai-r`` package was extracted into its own library we did **not**
want to break the public surface of those scripts — operators have
dashes, READMEs and muscle memory pointed at them.

This module is the thin compatibility layer that lets the legacy
scripts prefer the new ``ai-r`` CLI when:

1. ``ai-r`` is installed (``shutil.which`` returns non-None), and
2. The requested flag set is something ``ai-r`` can express.

If either condition fails, the wrappers return ``None`` and the caller
falls through to the original 2649 lines of legacy code.

The wrappers never raise — a broken shim must never break the legacy
script.  All subprocess output is streamed to ``sys.stdout`` /
``sys.stderr`` so that callers see the same content they'd get from
running ``ai-r`` directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence


__all__ = [
    "run_legacy_get_latest_context",
    "run_legacy_agent_audit",
    "is_ai_r_available",
]


_AI_R_BIN = "ai-r"


# Maps the legacy UPPERCASE agent label (used by the original scripts)
# to the lowercase value the new ``ai-r`` CLI accepts.  Anything
# not in this map has no equivalent in ai-r and forces a fallback
# to the legacy implementation.
_LEGACY_AGENT_TO_AI_R = {
    "CLAUDE": "claude",
    "CODEX": "codex",
    "OPENCODE": "opencode",
    "PI": "pi",
    "ANTIGRAVITY": "antigravity",
    "ANTIGRAVITY_CLI": "antigravity",
    "ANTIGRAVITY_IDE": "antigravity",
    "GEMINI": "antigravity",
    # ROO has no equivalent in ai-r; intentionally absent.
}


# Flags accepted by ai-r ``list`` / ``read`` (lowercase form).
# Used for fast pre-checks; the per-script lists below are the source
# of truth for "what is supported".
_AI_R_LIST_FLAGS = {"--agent", "--json"}
_AI_R_READ_FLAGS = {"--agent", "--json"}


def is_ai_r_available() -> bool:
    """Return True if the ``ai-r`` executable is on PATH."""
    return shutil.which(_AI_R_BIN) is not None


def _run_ai_r(args: Sequence[str]) -> int:
    """Run ``ai-r`` with ``args``, streaming I/O to the parent.

    Returns the child's exit code.  We never raise from subprocess
    failures — the caller (a legacy script) needs a clean exit code
    to decide what to do next.
    """
    try:
        completed = subprocess.run(
            [_AI_R_BIN, *args],
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
        return int(completed.returncode)
    except FileNotFoundError:
        return 127
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[legacy_compat] failed to invoke ai-r: {exc}", file=sys.stderr)
        return 1


def _parse_simple_flags(argv: Iterable[str]) -> dict:
    """Parse ``--key value`` / ``--flag`` pairs into a dict.

    Unknown short options or positional arguments are ignored — the
    caller treats them as "not understood → fall back to legacy".
    """
    out: dict = {}
    items = list(argv)
    i = 0
    while i < len(items):
        token = items[i]
        if not token.startswith("--"):
            i += 1
            continue
        key = token
        # Heuristic: if the next token exists and doesn't start with
        # ``--``, treat it as the value.  This is the same convention
        # the legacy argparse definitions use (``--agent FOO``).
        if i + 1 < len(items) and not items[i + 1].startswith("--"):
            out[key] = items[i + 1]
            i += 2
        else:
            out[key] = True
            i += 1
    return out


def run_legacy_get_latest_context() -> Optional[int]:
    """Translate ``get_latest_context.py`` flags to ``ai-r``.

    Returns the ``ai-r`` exit code if handled, or ``None`` if the
    caller should fall through to the legacy implementation.  Never
    raises.

    Legacy CLI surface (from ``get_latest_context.py`` argparse):

    * ``--agent {ROO,ANTIGRAVITY,ANTIGRAVITY_CLI,ANTIGRAVITY_IDE,
      GEMINI,CODEX,CLAUDE,OPENCODE}``
    * ``--all-agents`` (show all, override ``CURRENT_AGENT`` env var)
    * ``--limit N``
    * ``--id UUID``
    * ``--fuzzy`` (partial UUID match — ai-r has no equivalent)
    """
    if not is_ai_r_available():
        return None

    flags = _parse_simple_flags(sys.argv[1:])

    # Unrecognised / unsupported flags → legacy fallback.  This is
    # the cardinal rule: never lose information.
    unsupported = {"--fuzzy"}
    if unsupported & set(flags):
        return None

    session_id = flags.get("--id")
    if session_id is not None:
        if not isinstance(session_id, str):
            return None
        agent = flags.get("--agent")
        if not agent:
            # Legacy iterates ALL sources when ``--agent`` is absent;
            # ai-r has no equivalent → fall back.
            return None
        mapped = _LEGACY_AGENT_TO_AI_R.get(str(agent).upper())
        if not mapped:
            return None
        return _run_ai_r(["read", "--agent", mapped, str(session_id)])

    # List mode.
    if flags.get("--all-agents"):
        # ai-r ``list`` with no ``--agent`` shows all agents.
        args: List[str] = ["list"]
        if isinstance(flags.get("--limit"), str):
            try:
                n = int(flags["--limit"])
                # ai-r has no --limit; default behaviour already
                # shows all matches, so we just don't pass it.  We
                # also fall back when the user asked for a limit,
                # because the AI output wouldn't honour it.  That
                # keeps the legacy semantics intact.
                if n > 0:
                    return None
            except ValueError:
                return None
        return _run_ai_r(args)

    agent = flags.get("--agent")
    if agent:
        mapped = _LEGACY_AGENT_TO_AI_R.get(str(agent).upper())
        if not mapped:
            return None
        args = ["list", "--agent", mapped]
        if isinstance(flags.get("--limit"), str):
            try:
                n = int(flags["--limit"])
                if n > 0:
                    # ai-r has no --limit; fall back to legacy
                    # to honour the requested truncation.
                    return None
            except ValueError:
                return None
        return _run_ai_r(args)

    # No filter, no --all-agents, no --id → legacy shows
    # CURRENT_AGENT-filtered list, ai-r shows all.  Different
    # semantics, fall back.
    return None


def run_legacy_agent_audit() -> Optional[int]:
    """Translate ``agent-audit.py`` flags to ``ai-r``.

    Returns the ``ai-r`` exit code if handled, or ``None`` if the
    caller should fall through to the legacy implementation.  Never
    raises.

    Legacy CLI surface is ~25 flags; the vast majority have no
    equivalent in ai-r (``--search`` is content search, not
    title; ``--stats`` / ``--export`` / ``--timeline`` / etc. are
    legacy-only).  We translate only the minimal set needed for the
    smoke tests, and fall back for everything else.
    """
    if not is_ai_r_available():
        return None

    flags = _parse_simple_flags(sys.argv[1:])

    # All flags that the legacy script supports but ai-r does
    # not.  If any are present, the request cannot be served by the
    # new CLI and we fall back.
    legacy_only = {
        "--days", "--from-date", "--to-date", "--stats", "--export",
        "--full", "--index", "--all", "--search", "--msg-type",
        "--author", "--intent", "--list", "--no-list",
        "--title-search", "--last", "--after-phrase", "--timeline",
        "--parallel", "--title-only", "--file-creator",
    }
    if legacy_only & set(flags):
        return None

    session_id = flags.get("--id")
    if session_id is not None:
        if not isinstance(session_id, str):
            return None
        agent = flags.get("--agent")
        if not agent:
            if flags.get("--fuzzy"):
                return _run_ai_r(["read", str(session_id)])
            return None
        mapped = _LEGACY_AGENT_TO_AI_R.get(str(agent).upper())
        if not mapped:
            return None
        return _run_ai_r(["read", "--agent", mapped, str(session_id)])

    agent = flags.get("--agent")
    if agent:
        mapped = _LEGACY_AGENT_TO_AI_R.get(str(agent).upper())
        if not mapped:
            return None
        if isinstance(flags.get("--limit"), str):
            # ai-r has no --limit; fall back to legacy to keep
            # the user's truncation request.
            return None
        return _run_ai_r(["list", "--agent", mapped])

    if isinstance(flags.get("--limit"), str):
        # Same rationale as above.
        return None

    # No filter → list everything.  This is the one case where the
    # legacy default and the ai-r default coincide closely
    # enough to redirect.
    return _run_ai_r(["list"])
