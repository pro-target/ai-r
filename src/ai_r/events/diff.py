"""Phase-3a verb: ``diff`` — stitch edit rows into a per-file unified diff.

``diff`` reproduces the synthesis of ``session_diff``: given the edit events
for a session (``query(type="tool_call(edit)", session=…)`` — plus write /
shell-redirect events), it groups them per file in chronological order and
renders a stitched, readable hunk-by-hunk diff.  Bodies (``old_string`` /
``new_string`` / ``content`` / shell ``cmd``) are NOT inlined on the Event —
``diff`` fetches them on demand via :func:`get_body`, so this verb pays for
the payload only when it stitches.

The per-hunk rendering + caveats live in :mod:`ai_r.events.render`
(``_hunk_from_tool`` / ``_render_hunk`` / ``_GIT_CAVEAT`` / ``_RISK3_CAVEAT``),
which is imported by BOTH this verb and the ``session_diff`` preset — the
single source of truth for what an edit hunk looks like, and the reason the
event core no longer depends on ``session_diff``.

Moved verbatim from the former ``ai_r/events.py`` monolith — no logic change
(only the render-helper import target moved from ``session_diff`` to
``events.render``).
"""

from __future__ import annotations

from collections import OrderedDict as _OrderedDict
from typing import (
    Any,
    List,
    OrderedDict as OrderedDictType,
    Sequence,
    Tuple,
)

from ai_r.parsers import PARSERS, target_agents

from ai_r.events._common import (
    _coerce_tool_input,
    _path_from_payload,
    _plan_ref_value,
)
from ai_r.events.model import iter_events
from ai_r.events.render import (
    _GIT_CAVEAT,
    _RISK3_CAVEAT,
    _hunk_from_tool,
    _render_hunk,
)


def _edit_input_from_event(event_id: str) -> Tuple[str, dict[str, Any]]:
    """Re-resolve ``(tool_name, parsed_input_obj)`` for one edit event id.

    ``diff`` gets its edit rows from ``query`` whose Events carry only the raw
    tool NAME + refs (no body).  To stitch a real hunk we re-read the owning
    session, find the tool_use at the event's ``message_index`` matching the
    referenced file, and shape its input exactly like ``session_diff`` does
    (parse JSON, recover codex shell-redirect ``{cmd, edit}``).  Returns
    ``("", {})`` when the event/tool cannot be resolved.
    """
    from ai_r.find_file_edits import (
        _SHELL_EXEC_TOOLS,
        _extract_shell_command,
        _shell_redirect_targets,
    )

    if ":" not in event_id:
        return "", {}
    session_id = event_id.rsplit(":", 1)[0]
    stream = list(iter_events(session=session_id))
    event = next((e for e in stream if e.id == event_id), None)
    if event is None:
        return "", {}
    target_file = _plan_ref_value(event.refs, "file")
    tool_name = _plan_ref_value(event.refs, "tool") or event.text or ""

    for agent_name in target_agents(event.agent):
        parser = PARSERS[agent_name]
        for sess in parser.list_sessions():
            if sess.uuid != session_id:
                continue
            try:
                messages = parser.read_messages(sess.uuid)
            except (FileNotFoundError, ValueError, OSError):
                return tool_name, {}
            if not (0 <= event.message_index < len(messages)):
                return tool_name, {}
            msg = messages[event.message_index]
            for tool in getattr(msg, "tool_use", ()) or ():
                if not isinstance(tool, dict):
                    continue
                if tool.get("name", "") != tool_name:
                    continue
                if tool.get("name", "") in _SHELL_EXEC_TOOLS:
                    cmd = _extract_shell_command(tool.get("input", ""))
                    for fpath, append in _shell_redirect_targets(cmd):
                        if target_file is None or fpath == target_file:
                            return tool_name, {
                                "cmd": cmd,
                                "edit": "append" if append else "write",
                            }
                    continue
                payload = _coerce_tool_input(tool.get("input", ""))
                if isinstance(payload, dict):
                    # For the plain edit tools the whole parsed input carries
                    # the hunk shape (old_string/new_string/content/edits).
                    if target_file is None or _path_from_payload(payload) == target_file:
                        return tool_name, payload
            return tool_name, {}
    return tool_name, {}


def diff(
    rows: Sequence[dict[str, Any]],
    *,
    per_file: bool = True,
    format: str = "unified",
) -> dict[str, Any]:
    """Stitch edit rows into a per-file chronological diff — the diff verb.

    Reproduces the synthesis of :func:`ai_r.session_diff.session_diff`: given
    the edit events of a session (``query(type="tool_call(edit)",
    session=…)`` — plus ``write`` / shell-redirect events), group them per
    file in chronological order and render a stitched, readable diff.  Bodies
    are fetched on demand via :func:`get_body` (through the event's stored
    ``message_index``), never inlined on the row.

    Args:
        rows: Edit-event dicts (``query`` output).  Each must carry an ``id``
            (``"{session}:{seq}"``) and a ``refs`` list with a ``file`` entry;
            rows without a resolvable file are skipped.
        per_file: Group by file (the only mode today; ``False`` still groups
            per file but is reserved for a future flat mode).
        format: ``"unified"`` (the only rendering today).  Any other value
            raises :class:`ValueError`.

    Returns:
        ``{"files": [{"file", "edits", "diff", "hunks"}], "count": N,
        "caveats": [...]}`` — the same shape + caveats as
        :func:`session_diff`, with an added flat ``hunks`` list per file.

    Raises:
        ValueError: on an unsupported ``format``.
    """
    if format != "unified":
        raise ValueError(f"format must be 'unified', got {format!r}")

    # Build ordered (file, edit) events from the rows, mirroring the shaping
    # ``session_diff._scan_session`` produces.
    events: List[dict[str, Any]] = []
    for row in rows:
        event_id = row.get("id")
        if not isinstance(event_id, str) or ":" not in event_id:
            continue
        refs = row.get("refs", ()) or ()
        fpath = _plan_ref_value(refs, "file")
        ts = row.get("ts")
        try:
            seq = int(event_id.rsplit(":", 1)[-1])
        except ValueError:
            seq = -1
        tool_name, input_obj = _edit_input_from_event(event_id)
        # For a shell-redirect event the resolved file lives on the {cmd,edit}
        # shape; fall back to the ref file (or the redirect target).
        if fpath is None and isinstance(input_obj, dict) and "cmd" in input_obj:
            fpath = _plan_ref_value(refs, "file")
        if not fpath:
            continue
        events.append({
            "file": fpath,
            "timestamp": ts,
            "seq": seq,
            "intent": row.get("intent"),
            "tool": tool_name or (row.get("text") or ""),
            "hunks": _hunk_from_tool(tool_name, input_obj),
        })

    # Group per file, preserving chronological order within each file.
    by_file: "OrderedDictType[str, List[dict[str, Any]]]" = _OrderedDict()
    for ev in events:
        by_file.setdefault(ev["file"], []).append(ev)

    files: List[dict[str, Any]] = []
    for fpath, evs in by_file.items():
        ordered = sorted(
            evs,
            key=lambda e: (e["timestamp"] is None, e["timestamp"] or "", e["seq"]),
        )
        diff_blocks: List[str] = []
        edits_out: List[dict[str, Any]] = []
        all_hunks: List[dict[str, Any]] = []
        for ev in ordered:
            edits_out.append({
                "timestamp": ev["timestamp"],
                "intent": ev["intent"],
                "tool": ev["tool"],
                "hunks": ev["hunks"],
            })
            all_hunks.extend(ev["hunks"])
            header = f"@@ {ev['timestamp'] or '(no ts)'} {ev['tool']} @@"
            rendered = "\n".join(_render_hunk(h) for h in ev["hunks"])
            diff_blocks.append(f"{header}\n{rendered}")
        files.append({
            "file": fpath,
            "edits": edits_out,
            "diff": "\n".join(diff_blocks),
            "hunks": all_hunks,
        })

    return {
        "files": files,
        "count": len(files),
        "caveats": [_GIT_CAVEAT, _RISK3_CAVEAT],
    }
