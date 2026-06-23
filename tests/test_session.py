"""Tests for runtime session_id detection (env + flag file cascade)."""
from __future__ import annotations

import contextlib
import dataclasses
import inspect
import io
import os
from pathlib import Path

import pytest

from ai_r import cli as cli_module
from ai_r.parsers.models import AgentName
from ai_r.session import (
    AmbiguousSessionError,
    SessionCandidate,
    _is_valid_session_id,
    detect_session_candidates,
    detect_session_id,
    detect_session_id_with_source,
)


_DETECT_VARS = (
    "AI_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "OPENCODE_SESSION_ID",
    "AGENT_NAME",
    "AI_AGENT",
    "CODING_AGENT",
    "CODEX_HOME",
    "CLAUDECODE",
    "OPENCODE",
)


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every detection var; leaves HOME untouched."""
    for var in _DETECT_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def identity_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Point ``AI_R_SESSION_IDENTITY_DIR`` at a fresh tmp directory."""
    base = tmp_path / "session-identity"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AI_R_SESSION_IDENTITY_DIR", str(base))
    return base


def _write_flag(identity_dir: Path, agent: str, value: str) -> Path:
    path = identity_dir / agent / "current"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def test_detect_session_id_env_ai(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_SESSION_ID", "ses_test_xxx")
    sid, source, agent = detect_session_id_with_source()
    assert sid == "ses_test_xxx"
    assert source == "AI_SESSION_ID"
    assert agent is None


def test_detect_session_id_env_per_agent_claude(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    uuid = "abcdef01-2345-6789-abcd-ef0123456789"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", uuid)
    sid, source, agent = detect_session_id_with_source()
    assert sid == uuid
    assert source == "CLAUDE_CODE_SESSION_ID"
    assert agent == AgentName.CLAUDE


def test_detect_session_id_env_marker_opencode(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE", "1")
    _write_flag(identity_dir, "opencode", "ses_markerabc12")
    sid, source, agent = detect_session_id_with_source()
    assert sid == "ses_markerabc12"
    assert source == "flag/opencode"
    assert agent == AgentName.OPENCODE


def test_detect_session_id_env_marker_claude_wins_over_other_flag(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uuid = "abcdef01-2345-6789-abcd-ef0123456789"
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_flag(identity_dir, "claude", uuid)
    _write_flag(identity_dir, "opencode", "ses_should_not_win")
    sid, source, agent = detect_session_id_with_source()
    assert sid == uuid
    assert source == "flag/claude"
    assert agent == AgentName.CLAUDE


def test_detect_session_id_fallback_first_flag(
    _clean_env: None, identity_dir: Path
) -> None:
    uuid_claude = "abcdef01-2345-6789-abcd-ef0123456789"
    uuid_codex = "fedcba98-7654-3210-fedc-ba9876543210"
    _write_flag(identity_dir, "claude", uuid_claude)
    _write_flag(identity_dir, "codex", uuid_codex)
    sid, source, agent = detect_session_id_with_source()
    assert sid == uuid_claude
    assert source == "flag/claude"
    assert agent == AgentName.CLAUDE


def test_detect_session_id_shape_mismatch_rejected(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE", "1")
    _write_flag(identity_dir, "opencode", "abcdef01-2345-6789-abcd-ef0123456789")
    sid, source, agent = detect_session_id_with_source()
    assert sid is None
    assert source is None
    assert agent is None


def test_detect_session_id_symlink_rejected(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE", "1")
    real = identity_dir / "opencode" / "real"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("ses_real_value_xx", encoding="utf-8")
    link = identity_dir / "opencode" / "current"
    link.symlink_to(real)
    sid, source, agent = detect_session_id_with_source()
    assert sid is None
    assert source is None
    assert agent is None


def test_detect_session_id_invalid_charset(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_SESSION_ID", "bad id with spaces")
    sid, source, agent = detect_session_id_with_source()
    assert sid is None
    assert source is None
    assert agent is None


def test_detect_session_id_with_source_returns_triple(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_SESSION_ID", "ses_triple_xxx")
    result = detect_session_id_with_source()
    assert isinstance(result, tuple)
    assert len(result) == 3
    sid, source, agent = result
    assert sid == "ses_triple_xxx"
    assert source == "AI_SESSION_ID"
    assert agent is None


def test_is_valid_session_id() -> None:
    assert _is_valid_session_id("ses_abc123") is True
    assert _is_valid_session_id("abcdef01-2345-6789-abcd-ef0123456789") is True
    assert _is_valid_session_id("abc") is False
    assert _is_valid_session_id("") is False
    assert _is_valid_session_id("has space") is False
    assert _is_valid_session_id("has/slash") is False
    assert _is_valid_session_id("has.dot") is False


def _run_cli_inproc(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli_module.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


def test_cli_detect_session_found(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_SESSION_ID", "ses_cli_found_xxx")
    rc, out, err = _run_cli_inproc(["detect-session"])
    assert rc == 0
    assert "id=ses_cli_found_xxx" in out
    assert "source=AI_SESSION_ID" in out


def test_cli_detect_session_not_found(
    _clean_env: None, identity_dir: Path
) -> None:
    rc, out, err = _run_cli_inproc(["detect-session"])
    assert rc == 1
    assert "could not detect" in err.lower()


# ---------------------------------------------------------------------------
# detect_session_candidates (multi-candidate output, sidecars, modes)
# ---------------------------------------------------------------------------


def _write_per_session(identity_dir: Path, agent: str, session_id: str) -> Path:
    path = identity_dir / agent / session_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id, encoding="utf-8")
    return path


def _write_self(identity_dir: Path, agent: str, session_id: str,
                opencode_pid: int, opencode_ppid: int) -> Path:
    path = identity_dir / agent / f"{session_id}.self"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"{agent}\t{session_id}\topencode\t{opencode_pid}\t{opencode_ppid}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _write_fingerprint(identity_dir: Path, agent: str, session_id: str,
                       hash_value: str) -> Path:
    path = identity_dir / agent / f"{session_id}.fingerprint"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"{agent}\t{session_id}\t{hash_value}\t2026-06-21T00:00:00Z\n"
    path.write_text(content, encoding="utf-8")
    return path


def _find_alive_opencode_pid() -> int | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError, FileNotFoundError):
            continue
        if comm.startswith("opencode"):
            return int(entry.name)
    return None


def test_detect_session_candidates_returns_list(
    _clean_env: None, identity_dir: Path
) -> None:
    uuid_claude = "abcdef01-2345-6789-abcd-ef0123456789"
    uuid_codex = "fedcba98-7654-3210-fedc-ba9876543210"
    _write_per_session(identity_dir, "claude", uuid_claude)
    _write_per_session(identity_dir, "codex", uuid_codex)
    candidates = detect_session_candidates()
    ids = {c.session_id for c in candidates}
    assert uuid_claude in ids
    assert uuid_codex in ids
    assert all(c.source.startswith("ts_file:") for c in candidates)
    assert all(c.verified is True for c in candidates)


def test_detect_session_candidates_current_deprecated(
    _clean_env: None, identity_dir: Path
) -> None:
    uuid = "ses_depcurrentxx"
    _write_flag(identity_dir, "opencode", uuid)
    _write_per_session(identity_dir, "opencode", "ses_deptsfxxxxxx")
    with pytest.warns(DeprecationWarning, match="current pointer is deprecated"):
        candidates = detect_session_candidates()
    sources = [c.source for c in candidates]
    flag_sources = [s for s in sources if s.startswith("flag/")]
    ts_sources = [s for s in sources if s.startswith("ts_file:")]
    assert any(s == "flag/opencode" for s in flag_sources)
    assert "ts_file:opencode" in ts_sources


def test_detect_session_candidates_fingerprint_parsed(
    _clean_env: None, identity_dir: Path
) -> None:
    sid = "ses_fpparsedxxxx"
    _write_per_session(identity_dir, "opencode", sid)
    _write_fingerprint(
        identity_dir, "opencode", sid, "deadbeef" + "0" * 56
    )
    candidates = detect_session_candidates()
    matches = [c for c in candidates if c.session_id == sid]
    assert len(matches) == 1
    assert matches[0].fingerprint == "deadbeef"


def test_detect_session_candidates_self_parsed_opencode(
    _clean_env: None, identity_dir: Path
) -> None:
    opencode_pid = _find_alive_opencode_pid()
    if opencode_pid is None:
        pytest.skip("no alive opencode process on this host")
    sid = "ses_selfalivexxx"
    _write_per_session(identity_dir, "opencode", sid)
    _write_self(identity_dir, "opencode", sid, opencode_pid, opencode_pid)
    candidates = detect_session_candidates()
    matches = [c for c in candidates if c.session_id == sid]
    assert len(matches) == 1
    assert matches[0].is_self is True


def test_detect_session_candidates_self_false_dead_pid(
    _clean_env: None, identity_dir: Path
) -> None:
    sid = "ses_selfdeadjjjj"
    _write_per_session(identity_dir, "opencode", sid)
    _write_self(identity_dir, "opencode", sid, 1, 1)
    candidates = detect_session_candidates()
    matches = [c for c in candidates if c.session_id == sid]
    assert len(matches) == 1
    assert matches[0].is_self is False


def test_detect_session_id_strict_raises_on_ambiguous(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_SESSION_OUTPUT", "strict")
    _write_per_session(
        identity_dir, "claude", "abcdef01-2345-6789-abcd-ef0123456789"
    )
    _write_per_session(
        identity_dir, "codex", "fedcba98-7654-3210-fedc-ba9876543210"
    )
    with pytest.raises(AmbiguousSessionError):
        detect_session_id()


def test_detect_session_id_self_mode(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opencode_pid = _find_alive_opencode_pid()
    if opencode_pid is None:
        pytest.skip("no alive opencode process on this host")
    monkeypatch.setenv("AI_SESSION_OUTPUT", "self")
    sid_self = "ses_pickselfxxxxxx"
    sid_other = "ses_otheragentxx"
    _write_per_session(identity_dir, "opencode", sid_self)
    _write_per_session(identity_dir, "codex", sid_other)
    _write_self(identity_dir, "opencode", sid_self, opencode_pid, opencode_pid)
    assert detect_session_id() == sid_self


def test_detect_session_id_fingerprint_mode(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_hash = "abcd1234"
    monkeypatch.setenv("AI_SESSION_OUTPUT", f"fingerprint:{target_hash}")
    sid_match = "ses_fpmatchxxxxxx"
    sid_other = "ses_fpotherxxxxxx"
    _write_per_session(identity_dir, "opencode", sid_match)
    _write_per_session(identity_dir, "opencode", sid_other)
    _write_fingerprint(identity_dir, "opencode", sid_match, target_hash + "0" * 56)
    candidates = detect_session_candidates()
    assert any(c.fingerprint == target_hash for c in candidates)
    assert detect_session_id() == sid_match


def test_session_candidate_dataclass_frozen() -> None:
    cand = SessionCandidate(
        session_id="ses_imm_xx",
        agent=AgentName.OPENCODE,
        source="ts_file:opencode",
        verified=True,
        is_self=False,
        fingerprint="deadbeef",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cand.session_id = "ses_other_xx"  # type: ignore[misc]


def test_cli_detect_session_json_output(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    monkeypatch.setenv("AI_SESSION_ID", "ses_cli_json_xxx")
    rc, out, err = _run_cli_inproc(["detect-session", "--json"])
    assert rc == 0
    parsed = _json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["id"] == "ses_cli_json_xxx"
    assert parsed[0]["source"] == "AI_SESSION_ID"
    assert parsed[0]["verified"] is True
    assert parsed[0]["self"] is False


def test_cli_detect_session_warns_on_multiple(
    _clean_env: None, identity_dir: Path
) -> None:
    _write_per_session(
        identity_dir, "claude", "abcdef01-2345-6789-abcd-ef0123456789"
    )
    _write_per_session(
        identity_dir, "codex", "fedcba98-7654-3210-fedc-ba9876543210"
    )
    rc, out, err = _run_cli_inproc(["detect-session"])
    assert rc == 0
    assert "WARN" in err
    assert "disambiguation" in err.lower()


# ---------------------------------------------------------------------------
# Regression lock + identity-resolution gaps (audit 2026-06-21)
#
# The authorization gate (src/ai_r/access/, guard.py) was removed
# intentionally — a public single-user tool reads its owner's own sessions.
# These tests lock that decision and cover previously-untested branches of
# the session identity cascade (AI_SESSION_OUTPUT modes, sidecar parsing,
# env-vs-flag shadowing, fingerprint prefix collision).
# ---------------------------------------------------------------------------


def test_no_authorization_gate_parent_reads_sessions(
    _clean_env: None, identity_dir: Path
) -> None:
    """Regression lock: no authorization gate; parent reads own sessions.

    Asserts (1) the deleted ``ai_r.access`` module stays gone, (2) a
    process with no sub-agent env markers reads its session without
    ``PermissionError``, and (3) no symbol in ``ai_r.session``
    enforces sub-agent authorization.  Locks the approved public-repo
    decision from commit ee72961.
    """
    import importlib

    # (1) The access module is deleted — do not silently reintroduce it.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ai_r.access")

    # (2) Parent (no subagent markers) reads its own session: no gate.
    sid = "abcdef01-2345-6789-abcd-ef0123456789"
    _write_per_session(identity_dir, "claude", sid)
    resolved = detect_session_id_with_source()
    assert resolved[0] == sid
    assert resolved[1].startswith("ts_file:")
    assert resolved[2] is AgentName.CLAUDE

    # (3) Static lock: session.py carries no authorization primitive.
    import ai_r.session as session_mod

    source = inspect.getsource(session_mod)
    assert "PermissionError" not in source
    assert "is_subagent" not in source


def test_detect_session_id_first_mode_no_warning(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AI_SESSION_OUTPUT=first`` returns the first candidate, no WARN."""
    monkeypatch.setenv("AI_SESSION_OUTPUT", "first")
    monkeypatch.setenv("AI_SESSION_ID", "ses_first_mode_xx")
    _write_per_session(
        identity_dir, "claude", "abcdef01-2345-6789-abcd-ef0123456789"
    )
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        sid = detect_session_id()
    assert sid == "ses_first_mode_xx"
    assert err.getvalue() == ""


def test_detect_session_id_unknown_mode_falls_back_to_list(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown ``AI_SESSION_OUTPUT`` warns and falls back to list mode."""
    monkeypatch.setenv("AI_SESSION_OUTPUT", "bogus")
    monkeypatch.setenv("AI_SESSION_ID", "ses_unknown_mode")
    _write_per_session(
        identity_dir, "claude", "abcdef01-2345-6789-abcd-ef0123456789"
    )
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        sid = detect_session_id()
    assert sid == "ses_unknown_mode"
    stderr = err.getvalue()
    assert "unknown AI_SESSION_OUTPUT" in stderr
    assert "WARN" in stderr


def test_detect_session_id_empty_output_var_defaults_to_list(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty ``AI_SESSION_OUTPUT`` does not crash; defaults to list mode."""
    monkeypatch.setenv("AI_SESSION_OUTPUT", "")
    monkeypatch.setenv("AI_SESSION_ID", "ses_empty_out")
    _write_per_session(
        identity_dir, "claude", "abcdef01-2345-6789-abcd-ef0123456789"
    )
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        sid = detect_session_id()
    assert sid == "ses_empty_out"
    assert "disambiguation" in err.getvalue().lower()


def test_detect_session_candidates_env_shadows_flag_file(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env-var and flag-file candidates for the same id both survive; the
    env candidate is emitted first, so callers taking ``[0]`` get the
    env-var provenance (env shadows via ordering, not cross-list dedup)."""
    uuid = "abcdef01-2345-6789-abcd-ef0123456789"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", uuid)
    _write_per_session(identity_dir, "claude", uuid)
    resolved = detect_session_id_with_source()
    assert resolved == (uuid, "CLAUDE_CODE_SESSION_ID", AgentName.CLAUDE)
    sources = [
        c.source for c in detect_session_candidates() if c.session_id == uuid
    ]
    assert "CLAUDE_CODE_SESSION_ID" in sources
    assert "ts_file:claude" in sources


def test_read_self_malformed_returns_none(tmp_path: Path) -> None:
    """``.self`` with too few fields / non-integer PID yields None."""
    from ai_r.session import _read_self

    flag_dir = tmp_path / "opencode"
    flag_dir.mkdir(parents=True, exist_ok=True)
    sid = "ses_selfbadxxxx"
    too_few = flag_dir / f"{sid}.self"
    too_few.write_text("opencode\tses_selfbadxxxx\tname\n", encoding="utf-8")
    assert _read_self(flag_dir, sid) is None
    bad_pid = flag_dir / f"{sid}.self"
    bad_pid.write_text(
        "opencode\tses_selfbadxxxx\tname\tnotapid\t10\n", encoding="utf-8"
    )
    assert _read_self(flag_dir, sid) is None


def test_read_self_symlink_rejected(tmp_path: Path) -> None:
    """A symlinked ``.self`` sidecar is rejected (symmetry with ``current``)."""
    from ai_r.session import _read_self

    flag_dir = tmp_path / "opencode"
    flag_dir.mkdir(parents=True, exist_ok=True)
    sid = "ses_selflnkxxxxx"
    real = flag_dir / "real"
    real.write_text("opencode\tsid\tname\t100\t10\n", encoding="utf-8")
    (flag_dir / f"{sid}.self").symlink_to(real)
    assert _read_self(flag_dir, sid) is None


def test_read_fingerprint_malformed_and_symlink(tmp_path: Path) -> None:
    """``.fingerprint`` with too few fields / empty hash / symlink → None."""
    from ai_r.session import _read_fingerprint

    flag_dir = tmp_path / "opencode"
    flag_dir.mkdir(parents=True, exist_ok=True)
    sid = "ses_fpbadxxxxxx"
    fp = flag_dir / f"{sid}.fingerprint"

    fp.write_text("opencode\tses_fpbadxxxxxx\n", encoding="utf-8")
    assert _read_fingerprint(flag_dir, sid) is None

    fp.write_text(
        "opencode\tses_fpbadxxxxxx\t\t2026-06-21T00:00:00Z\n", encoding="utf-8"
    )
    assert _read_fingerprint(flag_dir, sid) is None

    fp.unlink()
    real = flag_dir / "real"
    real.write_text(
        "opencode\tses_fpbadxxxxxx\tdeadbeef\t2026-06-21T00:00:00Z\n",
        encoding="utf-8",
    )
    fp.symlink_to(real)
    assert _read_fingerprint(flag_dir, sid) is None


def test_fingerprint_prefix_collision_documents_behaviour(
    _clean_env: None, identity_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions sharing the ``sha256[:8]`` prefix both match; mode
    ``fingerprint:<prefix>`` returns the first.  Documents collision
    behaviour (8-char prefix = 32 bits) as a lock-in until the prefix
    is widened."""
    prefix = "deadbeef"
    sid_a = "ses_collide01aa"
    sid_b = "ses_collide02bb"
    _write_per_session(identity_dir, "opencode", sid_a)
    _write_per_session(identity_dir, "opencode", sid_b)
    _write_fingerprint(identity_dir, "opencode", sid_a, prefix + "0" * 56)
    _write_fingerprint(identity_dir, "opencode", sid_b, prefix + "1" * 56)
    candidates = detect_session_candidates()
    matched = {c.session_id for c in candidates if c.fingerprint == prefix}
    assert matched == {sid_a, sid_b}
    monkeypatch.setenv("AI_SESSION_OUTPUT", f"fingerprint:{prefix}")
    assert detect_session_id() in {sid_a, sid_b}
