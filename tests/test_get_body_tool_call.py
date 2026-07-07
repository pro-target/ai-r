"""``get_body`` on a ``tool_call`` id returns the full call body (FFE-3 fix).

Before this fix ``get_body(<tool_call id>)`` returned ``{id, type, text}`` where
``text`` was merely the tool NAME (``"Edit"`` / ``"Bash"``) — the non-plan
branch echoed ``event.text`` verbatim.  That left the reference-by-default
design (``find_file_edits`` emits ``input_sha256`` + ``input_chars``) with no
working on-demand route to the body.

The fix resolves the hosting message's raw ``tool_use`` and returns its full
``input`` under ``body`` (reusing the shared coerce), so:

* the returned body's JSON-canonical sha256/length match the
  ``input_sha256`` / ``input_chars`` reference ``find_file_edits`` emits;
* ``max_chars`` (+ ``body_truncated``) and ``redact`` behave like every other
  body;
* the ``user_turn`` / ``assistant_turn`` / ``plan_event`` branches are
  untouched.
"""

from __future__ import annotations

import hashlib
import json

from ai_r.events import get_body, query
from ai_r.find_file_edits import find_file_edits as _core


def _canonical(obj: object) -> str:
    """The JSON-canonical form ``find_file_edits._input_reference`` hashes."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _first_tool_call(session_id: str) -> dict:
    events = query(type="tool_call", session=session_id)
    assert events, f"no tool_call events for {session_id}"
    return events[0]


# ---------------------------------------------------------------------------
# 1. The body is the full call input (not the tool name).
# ---------------------------------------------------------------------------


def test_tool_call_body_returns_full_input(
    fake_claude_session_with_tools,
) -> None:
    """A ``tool_call`` id resolves to its full ``input``, not the tool name."""
    ev = _first_tool_call("claude-tools-1")
    body = get_body(ev["id"])

    assert body["id"] == ev["id"]
    assert body["type"].startswith("tool_call")
    assert body["tool"] == "Bash"
    # The full call body — NOT the bare tool name that the old branch echoed.
    assert body["body"] == {"command": "pytest"}
    assert body["body"] != "Bash"
    assert "error" not in body


# ---------------------------------------------------------------------------
# 2. sha256 + length parity with the find_file_edits reference.
# ---------------------------------------------------------------------------


def test_tool_call_body_matches_find_file_edits_reference(
    fake_claude_edit_session: str,
) -> None:
    """The returned body reproduces ``find_file_edits``' ``input_sha256``.

    ``find_file_edits(include_input=False)`` emits a light reference
    (``input_sha256`` + ``input_chars``); ``get_body`` on the SAME edit's
    ``tool_call`` event must return the body that hash was taken over.
    """
    ref = _core(path="widget.py", agent="claude", include_input=False)
    ref_records = [
        r for r in ref["records"] if r.get("file", "").endswith("widget.py")
    ]
    assert len(ref_records) == 1, ref
    ref_rec = ref_records[0]

    ev = _first_tool_call(fake_claude_edit_session)
    body = get_body(ev["id"], redact=False)

    canonical = _canonical(body["body"])
    got_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert got_sha == ref_rec["input_sha256"]
    assert len(canonical) == ref_rec["input_chars"]


# ---------------------------------------------------------------------------
# 3. max_chars caps the body and flags body_truncated.
# ---------------------------------------------------------------------------


def test_tool_call_dict_body_passes_max_chars_like_plans(
    fake_claude_session_with_tools,
) -> None:
    """A structured (dict) body is not sliced by ``max_chars`` — plan parity.

    ``_cap_body`` only bounds strings, so a dict tool input passes through
    whole regardless of ``max_chars`` (exactly like a plan ``body``); the
    over-1 MB coerce guard is what turns a pathological input into a string,
    and that string IS capped (asserted below).
    """
    ev = _first_tool_call("claude-tools-1")
    body = get_body(ev["id"], max_chars=1)
    assert body["body"] == {"command": "pytest"}
    assert "body_truncated" not in body


def test_tool_call_string_body_is_capped(fake_codex_session_with_tools) -> None:
    """A STRING tool input (codex ``shell``) is sliced + flags ``body_truncated``.

    Codex carries the call input as a raw string (``arguments: "pytest"``); a
    tiny ``max_chars`` exercises the string path of ``_cap_body`` end-to-end,
    proving the tool-call branch honours the cap contract.
    """
    ev = _first_tool_call("codex-tools-1")
    body = get_body(ev["id"], max_chars=3)
    assert isinstance(body["body"], str)
    assert body["body_truncated"] is True
    assert body["body"].endswith("…[truncated]")


# ---------------------------------------------------------------------------
# 4. redact masks secrets in the emitted body.
# ---------------------------------------------------------------------------


def test_tool_call_body_redacts_secrets(fake_claude_secret_edit: str) -> None:
    """A secret in the call input is masked on output by default."""
    ev = _first_tool_call(fake_claude_secret_edit)

    masked = get_body(ev["id"])  # redact=True default
    serialized = json.dumps(masked["body"], ensure_ascii=False)
    assert "REDACTED" in serialized
    assert "ghp_" not in serialized  # the raw token is gone
    assert masked.get("redactions")

    raw = get_body(ev["id"], redact=False)
    raw_serialized = json.dumps(raw["body"], ensure_ascii=False)
    assert "ghp_0123456789abcdef0123456789abcdef0123" in raw_serialized
    assert "redactions" not in raw


# ---------------------------------------------------------------------------
# 5. turn / plan branches are untouched (regression guard).
# ---------------------------------------------------------------------------


def test_turn_branch_shape_unchanged(fake_claude_session_with_tools) -> None:
    """A ``user_turn`` / ``assistant_turn`` id still returns ``{id,type,text}``.

    The tool-call branch must not have altered the turn path — same keys, same
    ``text`` payload, no ``body``/``tool`` leakage.
    """
    turns = query(type="user_turn", session="claude-tools-1")
    assert turns
    body = get_body(turns[0]["id"])
    assert set(body) == {"id", "type", "text"}
    assert body["type"] == "user_turn"
    assert body["text"] == "Run the tests"
    assert "body" not in body
    assert "tool" not in body


def test_plan_branch_shape_unchanged(fake_claude_plan_write: str) -> None:
    """A ``plan_event`` id still returns the plan-shaped body (no regression)."""
    plans = query(type="plan_event", session=fake_claude_plan_write)
    assert plans
    body = get_body(plans[0]["id"])
    assert body["type"] == "plan_event"
    assert "body" in body
    assert body.get("shallow") is False
    # The plan branch keys the body off the plan signal, not a tool_use.
    assert "tool" not in body
