"""Hermetic tests for the *codex* branch of :func:`ai_r.session_diff.session_diff`.

Unlike the structured-edit agents (claude / opencode / antigravity / pi), which
route ``session_diff`` through the ``diff`` verb (``_diff_via_verb``), codex
writes files through a **shell-exec** tool (``local_shell_call`` /
``exec_command``).  The edit target lives *inside* the command string, so codex
keeps the legacy :func:`ai_r.session_diff._scan_session` path plus the
shell-aware rendering (``_render_hunk`` ``kind == "shell"``).

That codex path used to be exercised only by host-dependent tests that the
hermetic CI job (``pytest -m "not host"``) deselects — leaving the
shell-redirect reconstruction (RISK-3) green even under regression.  These
tests synthesize a codex rollout on disk under a temp ``AI_R_HOME`` (via the
autouse ``_isolate_ai_r_home`` fixture) and assert the reconstructed per-file
diff, with **no real host data, no git, no** ``@pytest.mark.host``.

Recovered redirect patterns (the ones ``_shell_redirect_targets`` DOES detect):

* ``printf '...' > path``   → a fresh write (``mode == "write"``).
* ``printf '...' >> path``  → an append (``mode == "append"``).
* ``a > f1 && b > f2``      → one shell call splitting into TWO file edits.

The documented RISK-3 blind spots (``tee`` / ``sed -i`` / ``cp`` / ``mv`` /
heredoc-only) are *silently skipped* — a fact asserted explicitly below so the
limitation is pinned by a hermetic test rather than living only in prose.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ai_r.mcp_server import session_diff as mcp_session_diff
from ai_r.session_diff import session_diff


def _codex_shell_call(cmd: str, ts: str) -> dict:
    """A codex ``response_item`` shell-exec call carrying a bare command string.

    ``name`` must be one of ``_SHELL_EXEC_TOOLS`` (``local_shell_call`` /
    ``exec_command``) for the scanner to treat it as an edit; the codex parser
    keeps ``arguments`` verbatim as the tool ``input`` string, which
    ``_extract_shell_command`` returns unchanged for a bare string.
    """
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "local_shell_call",
            "name": "local_shell_call",
            "arguments": cmd,
        },
    }


def _codex_user(text: str, ts: str) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _write_codex_shell_edit_session(uuid: str) -> None:
    """Write a codex rollout whose file writes go through shell redirects.

    Timeline (chronological):

      1. user: "create the config"
      2. shell: ``printf '...' > /repo/app.py``           (write)
      3. user: "append a helper"
      4. shell: ``printf '...' >> /repo/app.py``          (append, same file)
      5. shell: ``echo one > /repo/a.txt && echo two > /repo/b.txt``
                                                          (ONE call → TWO files)
      6. shell: ``sed -i 's/x/y/' /repo/app.py``          (RISK-3: NOT detected)
      7. shell: ``tee /repo/never.txt <<'EOF' ...``       (RISK-3: NOT detected)
    """
    home = Path(os.environ["AI_R_HOME"])
    jsonl = (
        home / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T10-00-00-{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-06-14T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": uuid, "cwd": "/repo", "timestamp": "2026-06-14T10:00:00Z"},
        },
        _codex_user("create the config", "2026-06-14T10:00:01Z"),
        _codex_shell_call(
            "printf 'name = \"app\"\\nversion = 1\\n' > /repo/app.py",
            "2026-06-14T10:00:05Z",
        ),
        _codex_user("append a helper", "2026-06-14T10:01:00Z"),
        _codex_shell_call(
            "printf '\\ndef helper():\\n    return 1\\n' >> /repo/app.py",
            "2026-06-14T10:01:05Z",
        ),
        # ONE shell call redirecting into TWO distinct files.
        _codex_shell_call(
            "echo one > /repo/a.txt && echo two > /repo/b.txt",
            "2026-06-14T10:02:00Z",
        ),
        # RISK-3 blind spots: recovered target is NOT detected → skipped.
        _codex_shell_call(
            "sed -i 's/app/APP/' /repo/app.py",
            "2026-06-14T10:03:00Z",
        ),
        _codex_shell_call(
            "tee /repo/never.txt",
            "2026-06-14T10:04:00Z",
        ),
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_codex_shell_redirects_reconstructed_per_file(tmp_path: Path) -> None:
    uuid = "codex-sd-shell-1"
    _write_codex_shell_edit_session(uuid)

    result = session_diff(uuid, "codex")

    # Three files recovered: app.py (write + append), a.txt, b.txt.
    # sed -i and tee targets are RISK-3 blind spots → NOT counted.
    assert result["count"] == 3
    files = {f["file"]: f for f in result["files"]}
    assert set(files) == {"/repo/app.py", "/repo/a.txt", "/repo/b.txt"}

    # File grouping preserves first-appearance order.
    assert [f["file"] for f in result["files"]] == [
        "/repo/app.py",
        "/repo/a.txt",
        "/repo/b.txt",
    ]

    # --- app.py: two shell edits, chronological, write then append ---------
    app = files["/repo/app.py"]
    assert len(app["edits"]) == 2
    assert [e["timestamp"] for e in app["edits"]] == [
        "2026-06-14T10:00:05+00:00",
        "2026-06-14T10:01:05+00:00",
    ]
    assert [e["tool"] for e in app["edits"]] == [
        "local_shell_call",
        "local_shell_call",
    ]

    # Each edit is a single ``shell`` hunk carrying cmd + recovered mode.
    first_hunk = app["edits"][0]["hunks"][0]
    assert first_hunk["kind"] == "shell"
    assert first_hunk["mode"] == "write"
    assert "printf" in first_hunk["cmd"] and "> /repo/app.py" in first_hunk["cmd"]

    second_hunk = app["edits"][1]["hunks"][0]
    assert second_hunk["kind"] == "shell"
    assert second_hunk["mode"] == "append"

    # Intent threaded from the preceding user turn (previous_user_intent walk).
    assert app["edits"][0]["intent"] == "create the config"
    assert app["edits"][1]["intent"] == "append a helper"

    # Rendered diff uses the shell-hunk marker: ``$ (mode) cmd``.
    assert "$ (write) printf" in app["diff"]
    assert "$ (append) printf" in app["diff"]
    # Header carries timestamp + tool name.
    assert "local_shell_call @@" in app["diff"]

    # --- a.txt / b.txt: one shell call, two files -------------------------
    for path_, mode_marker in (("/repo/a.txt", "echo one"), ("/repo/b.txt", "echo two")):
        f = files[path_]
        assert len(f["edits"]) == 1
        h = f["edits"][0]["hunks"][0]
        assert h["kind"] == "shell"
        assert h["mode"] == "write"
        assert mode_marker in h["cmd"]

    # Both honest caveats are always attached.
    assert len(result["caveats"]) == 2


def test_codex_risk3_blind_spots_are_silently_skipped(tmp_path: Path) -> None:
    """The RISK-3 patterns (``sed -i`` / ``tee``) leave NO reconstructed edit."""
    uuid = "codex-sd-shell-2"
    _write_codex_shell_edit_session(uuid)

    result = session_diff(uuid, "codex")

    all_files = {f["file"] for f in result["files"]}
    # tee target never surfaces.
    assert "/repo/never.txt" not in all_files

    # app.py has EXACTLY the two detected redirect edits — the trailing
    # ``sed -i 's/app/APP/' /repo/app.py`` did NOT add a third edit.
    app = next(f for f in result["files"] if f["file"] == "/repo/app.py")
    assert len(app["edits"]) == 2

    # RISK-3 caveat text is present and names the undetected mechanisms.
    caveats = " ".join(result["caveats"]).lower()
    assert "tee" in caveats and "sed -i" in caveats
    assert "git" in caveats


def test_codex_path_filter_scopes_to_one_file(tmp_path: Path) -> None:
    """The ``path`` substring filter narrows the codex scan to matching files."""
    uuid = "codex-sd-shell-3"
    _write_codex_shell_edit_session(uuid)

    only_app = session_diff(uuid, "codex", path="app.py")
    assert only_app["count"] == 1
    assert only_app["files"][0]["file"] == "/repo/app.py"
    # Still both redirect edits on the filtered file.
    assert len(only_app["files"][0]["edits"]) == 2

    # A substring matching nothing yields an empty (but well-formed) result.
    none = session_diff(uuid, "codex", path="does-not-exist")
    assert none["count"] == 0
    assert none["files"] == []
    assert len(none["caveats"]) == 2


def test_codex_missing_session_returns_empty(tmp_path: Path) -> None:
    """An unknown codex uuid yields an empty diff, not an error (scan swallows it)."""
    result = session_diff("codex-nope-1", "codex")
    assert result["count"] == 0
    assert result["files"] == []
    assert len(result["caveats"]) == 2


def test_mcp_codex_session_diff_happy_path(tmp_path: Path) -> None:
    """The MCP wrapper returns the codex reconstruction without an error dict."""
    uuid = "codex-sd-shell-4"
    _write_codex_shell_edit_session(uuid)
    result = mcp_session_diff(session_uuid=uuid, agent="codex")
    assert "error" not in result
    assert result["count"] == 3
