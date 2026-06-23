"""``ai-r detect-agent`` subcommand handler."""

from __future__ import annotations

import argparse

from ai_r.agents import _detect_agent_with_source
from ai_r.cli.shared import _exit_with_error


def _run_detect_agent(args: argparse.Namespace) -> int:
    agent, source = _detect_agent_with_source()
    if agent is None:
        return _exit_with_error(
            "could not detect current agent; set AGENT_NAME, AI_AGENT, "
            "CODING_AGENT, CODEX_HOME, CLAUDECODE or OPENCODE",
        )
    if args.quiet:
        print(agent.value.lower())
    else:
        print(f"agent:    {agent.value}")
        print(f"source:   {source}")
    return 0
