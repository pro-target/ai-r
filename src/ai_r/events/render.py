"""Edit-hunk normalisation + rendering — the single source of truth (core).

These helpers were historically defined in :mod:`ai_r.session_diff` and lazily
imported *into* the ``diff`` verb, which made the event core depend on a
preset (a dependency inversion).  They are lifted here so BOTH the event core
(:mod:`ai_r.events.diff`) and the ``session_diff`` preset import them from the
core; the dependency now flows one way only (``session_diff`` -> events core).

No logic change: ``_hunk_from_tool`` / ``_render_hunk`` and the two caveat
constants are moved verbatim.
"""

from __future__ import annotations

from typing import Any, List

# Reused verbatim from
# ``ai_r.find_file_edits._shell_redirect_targets`` / ``docs/parsers.md``.
_RISK3_CAVEAT: str = (
    "Inherits the find_file_edits shell-redirect blind spot: writes via "
    "tee / sed -i / cp / mv are NOT detected, so "
    "session_diff silently skips them too. Redirect-head writes such as "
    "`printf '...' > path` and `cat > path <<EOF` ARE detected."
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
