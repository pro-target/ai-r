"""Ready-to-run resume command for a session (F2.2).

Every session summary carries a ``resume_command`` field: the exact shell
one-liner that reopens the conversation in its agent's CLI — or ``None``
when no such command exists (absence is honest, never fabricated; nothing
is ever executed by ai-r, the field is text only).

Per-agent reality (verified against the installed CLIs' own ``--help``):

* **Claude** — ``claude --resume <uuid>``. The CLI resolves ``--resume``
  against the project store of the *current working directory*, so the
  command must run from the session's project dir: when ``project_dir``
  is known the command is emitted as ``cd <project_dir> && claude
  --resume <uuid>``; when unknown, the bare command is emitted and only
  works if the caller is already inside the original project dir.
  A reference-only Desktop session (transcript deleted, ``path`` points
  at the metadata JSON, F1.3) has nothing to resume → ``None``.
* **Codex** — ``codex resume <uuid>`` (the session store is global and
  id-addressable; the cwd filter only affects the interactive picker).
  The ``cd`` prefix is still emitted when ``project_dir`` is known so
  the continued session runs in its original directory.
* **OpenCode** — ``opencode --session <id>`` (main-command flag
  ``-s, --session  session id to continue``); project-scoped TUI →
  ``cd`` prefix when the directory is known.
* **Pi** — ``pi --session <path>`` (``--session <path|id>``: the id
  lookup is scoped to the current project's session dir, while the
  recorded session-file path is unambiguous from anywhere → the path
  form is emitted).
* **Antigravity** — always ``None``: sessions are IDE brain directories
  with no CLI resume verb (the ``gemini`` CLI's ``--resume`` addresses
  its *own* store by index/"latest", not brain-dir ids).

Cross-agent rules:

* **Subagent (sidechain) sessions are not resumable** — ``kind ==
  "subagent"`` (or a set ``parent_uuid``) → ``None``: the CLIs resume
  top-level interactive conversations, not spawned tool threads.
* When ``project_dir`` is known the command is prefixed with
  ``cd <project_dir> && `` so it works from any shell.
* All interpolated values (uuid / path / dir) are shell-quoted.
"""

from __future__ import annotations

import shlex
from typing import Optional

from ai_r.parsers.models import AgentName, Session

__all__ = ["resume_command"]


def _with_project_dir(command: str, project_dir: Optional[str]) -> str:
    """Prefix ``command`` with ``cd <project_dir> && `` when the dir is known."""
    if project_dir:
        return f"cd {shlex.quote(project_dir)} && {command}"
    return command


def resume_command(session: Session) -> Optional[str]:
    """The exact shell command that resumes ``session``, or ``None``.

    Text only — never executed by ai-r. ``None`` = no real resume
    command exists for this session (see module docstring for the
    per-agent rationale).
    """
    # Sidechain/subagent sessions are spawned tool threads, not
    # top-level interactive conversations — no CLI resumes them.
    if session.kind == "subagent" or session.parent_uuid:
        return None

    if session.agent is AgentName.CLAUDE:
        # Reference-only Desktop session (F1.3): the transcript is gone,
        # ``path`` points at the metadata JSON — nothing to resume.
        if not session.path.endswith(".jsonl"):
            return None
        return _with_project_dir(
            f"claude --resume {shlex.quote(session.uuid)}", session.project_dir
        )

    if session.agent is AgentName.CODEX:
        return _with_project_dir(
            f"codex resume {shlex.quote(session.uuid)}", session.project_dir
        )

    if session.agent is AgentName.OPENCODE:
        return _with_project_dir(
            f"opencode --session {shlex.quote(session.uuid)}", session.project_dir
        )

    if session.agent is AgentName.PI:
        return _with_project_dir(
            f"pi --session {shlex.quote(session.path)}", session.project_dir
        )

    # Antigravity (and any future agent without a known resume verb):
    # no CLI resume command exists — absence is honest.
    return None
