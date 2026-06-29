"""Interactive question→answer extraction across agents.

Verifies that the user's reply to an interactive agent question
(Claude ``AskUserQuestion``, Codex ``request_user_input``, OpenCode
``question``) is recovered as a *pair* — the question text paired with
the chosen answer — on :attr:`ai_r.parsers.models.Message.qa`, and that
the pair survives the MCP ``read_session`` projection.

Agents whose session format carries no structured interactive-question
record (Pi, Antigravity) are asserted to never emit ``qa`` (explicit
"format does not support this" coverage), so a future regression that
silently starts/stops emitting is caught.
"""
from __future__ import annotations

from pathlib import Path

from ai_r.parsers import claude, codex, opencode, antigravity, pi
from ai_r.mcp_server import _project_messages


# ---------------------------------------------------------------------------
# Claude — AskUserQuestion (answer lives only in the tool_result string)
# ---------------------------------------------------------------------------


def test_claude_qa_pair(fake_claude_session_with_ask: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    msgs = claude.read_messages("claude-ask-1", base_dir=base)
    qa_msgs = [m for m in msgs if m.qa]
    assert len(qa_msgs) == 1
    entry = qa_msgs[0].qa[0]
    assert entry["question"] == "Which approach?"
    assert entry["answer"] == "Option B"
    # Options are recovered from the AskUserQuestion call and joined back
    # to the answer parsed from the result string.
    assert entry["options"] == ("Option A", "Option B")


def test_claude_qa_strips_internal_keys(
    fake_claude_session_with_ask: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    msgs = claude.read_messages("claude-ask-1", base_dir=base)
    for m in msgs:
        for tu in m.tool_use:
            assert not any(k.startswith("_") for k in tu)
        for tr in m.tool_result:
            assert not any(k.startswith("_") for k in tr)


def test_claude_no_ask_no_qa(
    fake_claude_session_with_tools: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    msgs = claude.read_messages("claude-tools-1", base_dir=base)
    assert all(m.qa == () for m in msgs)


# ---------------------------------------------------------------------------
# Codex — request_user_input (answers keyed by question id in the output)
# ---------------------------------------------------------------------------


def test_codex_qa_pair(fake_codex_session_with_ask: Path, tmp_sessions_dir: Path) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    msgs = codex.read_messages("codex-ask-1", base_dir=base)
    qa_msgs = [m for m in msgs if m.qa]
    assert len(qa_msgs) == 1
    entry = qa_msgs[0].qa[0]
    assert entry["question"] == "Which mode?"
    assert entry["answer"] == "Safe"
    assert entry["options"] == ("Fast", "Safe")


def test_codex_no_ask_no_qa(
    fake_codex_session_with_tools: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    msgs = codex.read_messages("codex-tools-1", base_dir=base)
    assert all(m.qa == () for m in msgs)


# ---------------------------------------------------------------------------
# OpenCode — question tool (answers parallel to questions in metadata)
# ---------------------------------------------------------------------------


def test_opencode_qa_pair(fake_opencode_db_with_ask: Path) -> None:
    msgs = opencode.read_messages("oc-ask-1", override=str(fake_opencode_db_with_ask))
    qa_msgs = [m for m in msgs if m.qa]
    assert len(qa_msgs) == 1
    qa = qa_msgs[0].qa
    assert len(qa) == 2
    assert qa[0]["question"] == "Scope?"
    assert qa[0]["answer"] == "Small"
    assert qa[0]["options"] == ("Small", "Big")
    # Multi-select: both chosen labels are joined.
    assert qa[1]["question"] == "Extras?"
    assert qa[1]["answer"] == "Tests | Docs"


def test_opencode_no_ask_no_qa(fake_opencode_db_with_tools: Path) -> None:
    msgs = opencode.read_messages(
        "oc-tools-1", override=str(fake_opencode_db_with_tools)
    )
    assert all(m.qa == () for m in msgs)


# ---------------------------------------------------------------------------
# Pi / Antigravity — format does not carry structured interactive questions
# ---------------------------------------------------------------------------


def test_pi_format_has_no_qa(
    fake_pi_session_with_tools: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".pi" / "agent" / "sessions")
    msgs = pi.read_messages("pi-tools-1", base_dir=base)
    assert all(m.qa == () for m in msgs)


def test_antigravity_format_has_no_qa(
    fake_antigravity_brain_with_transcript: Path, tmp_sessions_dir: Path
) -> None:
    brain = fake_antigravity_brain_with_transcript
    msgs = antigravity.read_messages("ag-tools-1", base_dir=str(brain.parent))
    assert all(m.qa == () for m in msgs)


# ---------------------------------------------------------------------------
# MCP projection — the pair survives read_session's {role, content} output
# ---------------------------------------------------------------------------


def test_mcp_projection_surfaces_claude_qa(
    fake_claude_session_with_ask: Path, tmp_sessions_dir: Path
) -> None:
    base = str(tmp_sessions_dir / ".claude" / "projects")
    projected = _project_messages(claude.read_messages("claude-ask-1", base_dir=base))
    qa_dicts = [p for p in projected if "qa" in p]
    assert len(qa_dicts) == 1
    # Structured pair attached to the message dict.
    assert qa_dicts[0]["qa"][0] == {
        "question": "Which approach?",
        "options": ["Option A", "Option B"],
        "answer": "Option B",
    }
    # And the readable rendering carries BOTH question and answer in content,
    # so a bare answer is never surfaced alone.
    assert "Which approach?" in qa_dicts[0]["content"]
    assert "Option B" in qa_dicts[0]["content"]


def test_mcp_projection_surfaces_codex_tool_role_qa(
    fake_codex_session_with_ask: Path, tmp_sessions_dir: Path
) -> None:
    """Codex records the answer on a tool-role record; it must still surface."""
    base = str(tmp_sessions_dir / ".codex" / "sessions")
    projected = _project_messages(codex.read_messages("codex-ask-1", base_dir=base))
    qa_dicts = [p for p in projected if "qa" in p]
    assert len(qa_dicts) == 1
    # The tool-role answer record is relabelled "user" (it is the user's reply).
    assert qa_dicts[0]["role"] == "user"
    assert qa_dicts[0]["qa"][0]["question"] == "Which mode?"
    assert qa_dicts[0]["qa"][0]["answer"] == "Safe"


def test_mcp_projection_surfaces_opencode_qa(fake_opencode_db_with_ask: Path) -> None:
    projected = _project_messages(
        opencode.read_messages("oc-ask-1", override=str(fake_opencode_db_with_ask))
    )
    qa_dicts = [p for p in projected if "qa" in p]
    assert len(qa_dicts) == 1
    answers = {e["question"]: e["answer"] for e in qa_dicts[0]["qa"]}
    assert answers == {"Scope?": "Small", "Extras?": "Tests | Docs"}
