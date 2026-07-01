"""Reconstruct *what the agent changed* in a single session — without git.

The data is already in the session transcript: ``Edit`` / ``MultiEdit``
tool calls carry ``old_string`` → ``new_string``, ``Write`` carries the
full ``content``, and codex routes writes through a shell-exec tool whose
redirection target + mode (``write`` / ``append``) is recovered by
:mod:`ai_r.find_file_edits`. Stitching those edits together per file, in
chronological order, yields a precise picture of the agent's editing
actions for that session.

This module **reuses** the extraction stream of
:mod:`ai_r.find_file_edits` (file path, timestamp, tool input, intent)
rather than re-parsing transcripts: the per-session scan below calls the
exact same exported helpers (``edit_path_from_input``,
``_extract_shell_command``, ``_shell_redirect_targets``,
``previous_user_intent``, ``to_utc_aware``, ``iso``) so there is a single
source of truth for *which* tool calls count as edits.

TWO honest blind spots — surfaced in the tool output as ``caveats``:

1. This is a diff of the **agent's actions**, not the git outcome. Manual
   edits, partial commits, merges, or reverts that happen outside the
   session are invisible here. (git is deliberately out of scope — the
   user's decision.)
2. RISK-3 — it inherits the shell-redirect blind spot of
   :func:`ai_r.find_file_edits._shell_redirect_targets`: writes via
   ``tee`` / ``sed -i`` / ``cp`` / ``mv`` / heredoc-only are NOT detected,
   so ``session_diff`` silently skips them too. (The disclaimer text is
   reused verbatim from that function's docstring / ``docs/parsers.md``.)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional

from ai_r.find_file_edits import (
    EDIT_TOOLS,
    edit_path_from_input,
    iso,
    previous_user_intent,
    to_utc_aware,
)
from ai_r.find_file_edits import (
    _SHELL_EXEC_TOOLS,
    _extract_shell_command,
    _shell_redirect_targets,
)
from ai_r.parsers import PARSERS, coerce_agent

__all__ = ["session_diff"]


# Reused verbatim from
# ``ai_r.find_file_edits._shell_redirect_targets`` / ``docs/parsers.md``.
_RISK3_CAVEAT: str = (
    "Inherits the find_file_edits shell-redirect blind spot: writes via "
    "tee / sed -i / cp / mv / heredoc-only are NOT detected, so "
    "session_diff silently skips them too. The common codex pattern "
    "`printf '...' > path` IS detected."
)

_GIT_CAVEAT: str = (
    "This is a diff of the agent's ACTIONS as recorded in the session, "
    "not the git outcome. Manual edits, partial commits, merges or reverts "
    "made outside the session are invisible here (git is out of scope by "
    "design)."
)


def _hunk_from_tool(
    tool_name: str, input_obj: dict[str, Any]
) -> List[dict[str, Any]]:
    """Normalise one edit tool input into a list of hunks.

    Three shapes are handled:

    * ``Edit`` / ``str_replace`` and friends → a single ``replace`` hunk
      ``{kind, old, new}``.
    * ``MultiEdit`` (``edits=[{old_string,new_string}, ...]``) → one
      ``replace`` hunk per entry, in order.
    * ``Write`` / ``create_file`` (``content``) → one ``write`` hunk with
      the full file body (new file or full overwrite).
    * codex shell-exec → one ``shell`` hunk carrying the command and the
      ``write`` / ``append`` mode recovered by ``find_file_edits``.

    Unrecognised shapes yield a single ``unknown`` hunk so the call is
    never silently dropped from the timeline.

    ``tool_name`` is accepted for API symmetry / future per-tool dispatch;
    normalisation is driven by the input *shape*, which is unambiguous.
    """
    # codex shell-exec: find_file_edits already shaped this as
    # {"cmd": ..., "edit": "write"|"append"}.
    if "cmd" in input_obj and "edit" in input_obj:
        return [
            {
                "kind": "shell",
                "mode": str(input_obj.get("edit") or "write"),
                "cmd": str(input_obj.get("cmd") or ""),
            }
        ]

    # MultiEdit: a list of old→new replacements applied in order.
    edits = input_obj.get("edits")
    if isinstance(edits, list) and edits:
        hunks: List[dict[str, Any]] = []
        for entry in edits:
            if not isinstance(entry, dict):
                continue
            hunks.append(
                {
                    "kind": "replace",
                    "old": str(entry.get("old_string", "")),
                    "new": str(entry.get("new_string", "")),
                }
            )
        if hunks:
            return hunks

    # Write / create_file / write_file: full content.
    if "content" in input_obj and "old_string" not in input_obj:
        return [
            {
                "kind": "write",
                "content": str(input_obj.get("content") or ""),
            }
        ]

    # Edit / str_replace / single old→new replacement.
    if "old_string" in input_obj or "new_string" in input_obj:
        return [
            {
                "kind": "replace",
                "old": str(input_obj.get("old_string", "")),
                "new": str(input_obj.get("new_string", "")),
            }
        ]

    # Unknown edit-tool shape — keep it in the timeline, but mark it.
    return [{"kind": "unknown", "raw": input_obj}]


def _render_hunk(hunk: dict[str, Any]) -> str:
    """Render one hunk as a readable unified-ish diff block."""
    kind = hunk.get("kind")
    if kind == "replace":
        old_lines = [f"- {ln}" for ln in str(hunk.get("old", "")).splitlines()]
        new_lines = [f"+ {ln}" for ln in str(hunk.get("new", "")).splitlines()]
        body = "\n".join(old_lines + new_lines)
        return body or "(empty replace)"
    if kind == "write":
        new_lines = [f"+ {ln}" for ln in str(hunk.get("content", "")).splitlines()]
        body = "\n".join(new_lines)
        return body or "(empty write)"
    if kind == "shell":
        mode = hunk.get("mode", "write")
        return f"$ ({mode}) {hunk.get('cmd', '')}"
    return f"(unrecognised edit: {hunk.get('raw')!r})"


def _scan_session(
    agent_name: Any,
    session_uuid: str,
    path_filter: Optional[str],
) -> List[dict[str, Any]]:
    """Yield ordered edit events for ONE session.

    Mirrors the inner loop of :func:`ai_r.find_file_edits.find_file_edits`
    (reusing its exported extraction helpers) but scoped to a single
    session so we never scan the whole vault. Each event is
    ``{file, timestamp, intent, tool, hunks}``.
    """
    parser = PARSERS[agent_name]
    try:
        messages = parser.read_messages(session_uuid)
    except (FileNotFoundError, ValueError, OSError):
        return []

    events: List[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        if msg.role != "assistant" or not msg.tool_use:
            continue
        msg_ts: Optional[datetime] = to_utc_aware(getattr(msg, "timestamp", None))
        intent = previous_user_intent(messages, idx)
        for tool in msg.tool_use:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "")
            is_shell = name in _SHELL_EXEC_TOOLS
            if name not in EDIT_TOOLS and not is_shell:
                continue
            tool_ts = to_utc_aware(tool.get("timestamp"))
            edit_ts = tool_ts if tool_ts is not None else msg_ts

            # Build (file, input) candidates — identical shaping to
            # find_file_edits so codex shell-exec multi-writes split too.
            if is_shell:
                cmd = _extract_shell_command(tool.get("input", ""))
                candidates: List[tuple[str, dict[str, Any]]] = [
                    (
                        fpath,
                        {"cmd": cmd, "edit": "append" if append else "write"},
                    )
                    for fpath, append in _shell_redirect_targets(cmd)
                    if path_filter is None or path_filter in fpath
                ]
            else:
                raw_input = tool.get("input", "")
                payload: object = raw_input
                if isinstance(raw_input, str) and raw_input.strip():
                    try:
                        payload = json.loads(raw_input)
                    except (ValueError, TypeError):
                        payload = raw_input
                file_path = edit_path_from_input(payload)
                if file_path is None or (
                    path_filter is not None and path_filter not in file_path
                ):
                    candidates = []
                else:
                    candidates = [
                        (
                            file_path,
                            payload if isinstance(payload, dict) else {},
                        )
                    ]

            for file_path, input_obj in candidates:
                events.append(
                    {
                        "file": file_path,
                        "timestamp": iso(edit_ts) if edit_ts is not None else None,
                        "message_index": idx,
                        "intent": intent,
                        "tool": name,
                        "hunks": _hunk_from_tool(name, input_obj),
                    }
                )
    return events


def _diff_via_verb(
    session_uuid: str, agent: str, path_filter: Optional[str]
) -> dict[str, Any]:
    """Reconstruct a session's per-file diff by delegating to the ``diff`` verb.

    Builds the session's edit events (``tool_call(edit)`` / ``tool_call(write)``
    with a ``file`` ref) via ``query(with_intent=True)`` — a single,
    chronological stream so file grouping matches ``_scan_session``'s
    first-appearance order — folds them with ``diff``, and projects the result
    onto the exact legacy shape (dropping ``diff``'s extra file-level
    ``hunks`` key, which is additive and not part of the ``session_diff``
    contract).  Byte-identical to the legacy scan for structured-edit agents.
    """
    from ai_r.events import diff as _diff, query as _query

    rows: List[dict[str, Any]] = []
    for ev in _query(
        type="tool_call", session=session_uuid, agent=agent, with_intent=True
    ):
        # Only real edits: ``Edit``/``Write``/… normalize to edit|write; a
        # ``Read``/``View`` carries a file ref too but is NOT an edit, so it
        # must be excluded to match the legacy EDIT_TOOLS filter.
        if ev.get("type") not in ("tool_call(edit)", "tool_call(write)"):
            continue
        files = [r.get("file", "") for r in ev.get("refs", ()) if "file" in r]
        if not files:
            continue
        if path_filter is not None and not any(path_filter in f for f in files):
            continue
        rows.append(ev)

    folded = _diff(rows)
    # Project onto the legacy shape: keep only file/edits/diff per file (drop
    # the additive per-file ``hunks``), preserving order and caveats.
    files = [
        {"file": f["file"], "edits": f["edits"], "diff": f["diff"]}
        for f in folded["files"]
    ]
    return {
        "files": files,
        "count": folded["count"],
        "caveats": folded["caveats"],
    }


def session_diff(
    session_uuid: str,
    agent: str,
    path: Optional[str] = None,
) -> dict[str, Any]:
    """Reconstruct the agent's per-file edits for one session (no git).

    Args:
        session_uuid: The session to reconstruct.
        agent: Which agent owns the session — one of ``"claude"``,
            ``"codex"``, ``"opencode"``, ``"antigravity"``, ``"pi"``.
        path: Optional substring filter on the edited file path
            (case-sensitive). ``None`` = every edited file in the session.

    Returns:
        ``{"files": [...], "count": N, "caveats": [...]}`` where each
        entry in ``files`` is::

            {
                "file": str,
                "edits": [           # chronological
                    {"timestamp", "intent", "tool", "hunks"}, ...
                ],
                "diff": str,         # stitched, readable hunk-by-hunk diff
            }

        ``caveats`` always carries the two honest blind spots (git-outcome
        divergence and the RISK-3 shell-redirect gap).

    Raises:
        ValueError: ``session_uuid`` empty, or ``agent`` unknown.
    """
    if not isinstance(session_uuid, str) or not session_uuid.strip():
        raise ValueError("session_uuid must be a non-empty string")
    if path is not None and not isinstance(path, str):
        raise ValueError("path must be a string or None")
    # ``coerce_agent`` raises ``ValueError`` on an unknown agent, which is
    # exactly the contract the MCP/CLI wrappers expect — let it propagate.
    agent_name = coerce_agent(agent)

    # Structured-edit agents (claude / opencode / antigravity / pi) route their
    # edits through real ``Edit`` / ``Write`` tool_use entries, which the
    # unified event stream normalizes to ``tool_call(edit)`` / ``(write)``
    # events carrying a ``file`` ref.  For those we DELEGATE to the ``diff``
    # verb over ``query(with_intent=True)`` — byte-identical on real data
    # (proven across the host vault).  Codex is the one exception: it writes
    # files through a shell-exec tool whose redirect targets are recovered by a
    # command-string scan the event stream does NOT run, so a codex session's
    # shell-redirect edits would vanish from a ``query`` fold.  Codex therefore
    # keeps the legacy ``_scan_session`` path, preserving byte-parity for every
    # agent.
    agent_lc = agent_name.value.lower()
    if agent_lc != "codex":
        return _diff_via_verb(session_uuid.strip(), agent_lc, path if path else None)

    path_filter = path if path else None
    events = _scan_session(agent_name, session_uuid.strip(), path_filter)

    # Group events by file, preserving chronological order within each file.
    by_file: dict[str, List[dict[str, Any]]] = {}
    order: List[str] = []
    for ev in events:
        fpath = ev["file"]
        if fpath not in by_file:
            by_file[fpath] = []
            order.append(fpath)
        by_file[fpath].append(ev)

    files: List[dict[str, Any]] = []
    for fpath in order:
        evs = sorted(
            by_file[fpath],
            key=lambda e: (
                e["timestamp"] is None,
                e["timestamp"] or "",
                e["message_index"],
            ),
        )
        diff_blocks: List[str] = []
        edits_out: List[dict[str, Any]] = []
        for ev in evs:
            edits_out.append(
                {
                    "timestamp": ev["timestamp"],
                    "intent": ev["intent"],
                    "tool": ev["tool"],
                    "hunks": ev["hunks"],
                }
            )
            header = f"@@ {ev['timestamp'] or '(no ts)'} {ev['tool']} @@"
            rendered = "\n".join(_render_hunk(h) for h in ev["hunks"])
            diff_blocks.append(f"{header}\n{rendered}")
        files.append(
            {
                "file": fpath,
                "edits": edits_out,
                "diff": "\n".join(diff_blocks),
            }
        )

    return {
        "files": files,
        "count": len(files),
        "caveats": [_GIT_CAVEAT, _RISK3_CAVEAT],
    }
