"""Property-based fuzzing of the five session parsers (hypothesis).

The parsers are ai-r's only ingestion point for **untrusted input**: the
transcripts are written by five foreign agents, may be truncated mid-write
(the agent is still running), half-corrupt (a killed process, a full disk),
schema-drifted (an upstream release) — or simply hostile.  A crash there is
not a cosmetic defect: one bad line in one file poisons ``list_sessions``
and takes the reader down for *every* session.

The contract the parsers promise (``docs/parsers.md`` → "Rules of thumb")
is fail-soft: a record that cannot be understood is **skipped**, never
raised.  The only documented exception is :class:`FileNotFoundError` for an
unknown uuid (plus :class:`ValueError` for a *malformed* uuid — out of play
here: every fuzz call passes a structurally valid one).

Invariant asserted below, for every entry point of every parser:

    hostile bytes in  ->  a value of the declared type out, or
                          FileNotFoundError — never anything else.

Generated hostility: truncated JSON, raw NUL bytes, invalid UTF-8, lone
surrogates, missing required fields, extra unknown fields, deeply nested
blobs, gigantic strings, epoch-sized integers, wrong types in every slot.

The deterministic regressions at the bottom pin the crashes this module
found on its first run (fixed in ``_common``/``claude``/``opencode``).
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ai_r.parsers import AgentName, Message, Session, antigravity, claude, codex, opencode, pi

# ---------------------------------------------------------------------------
# Strategies: what a corrupt / hostile transcript line looks like
# ---------------------------------------------------------------------------

# Field names the parsers actually branch on, plus junk ones — so a generated
# record hits the typed code paths (``type``/``message``/``payload`` …) with
# the WRONG type as often as with a plausible one.
_FIELD_NAMES = st.sampled_from(
    [
        "type", "message", "payload", "role", "content", "text", "timestamp",
        "usage", "tool_use", "tool_result", "input", "name", "id", "sessionId",
        "isSidechain", "cwd", "model", "thinking", "customTitle", "aiTitle",
        "requestId", "toolUseResult", "parentUuid", "state", "tokens", "parts",
        "source", "summary", "arguments", "questions", "answers", "options",
        "modelID", "callID", "tool", "output", "status", "info",
        "",  # empty key
        "__proto__",  # prototype-pollution style key
    ]
)

# Leaves: every scalar a JSON document can carry, including the ones that blow
# up naive arithmetic/formatting — epoch-sized ints, NaN/inf, a lone surrogate,
# an embedded NUL, an oversized string.
_JSON_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40),
    st.sampled_from(
        [
            "",
            "\x00",
            "a\x00b",
            "\ud800",  # lone surrogate (survives JSON escaping)
            "\U0010ffff",
            "x" * 4096,
            "9999-99-99T99:99:99Z",
            "-0001-01-01T00:00:00",
            "<markup>text the title heuristics must not choke on</markup>",
        ]
    ),
)

_JSON_VALUES = st.recursive(
    _JSON_LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_FIELD_NAMES, children, max_size=4),
    ),
    max_leaves=12,
)

# A record shaped like a transcript record: a ``type`` the parsers know (or
# junk), plus any mix of the other fields, with any types.
_RECORDS = st.fixed_dictionaries(
    {
        "type": st.one_of(
            st.sampled_from(
                [
                    "user", "assistant", "session", "session_meta", "message",
                    "response_item", "event_msg", "turn_context", "custom-title",
                    "ai-title", "session_info", "model_change", "USER_INPUT",
                ]
            ),
            _JSON_VALUES,
        )
    },
    optional={
        key: _JSON_VALUES
        for key in (
            "message", "payload", "role", "content", "timestamp", "usage",
            "id", "sessionId", "isSidechain", "cwd", "toolUseResult", "info",
        )
    },
)


def _encode_record(record: dict) -> bytes:
    """Serialise a generated record the way an agent would write it."""
    return json.dumps(record, ensure_ascii=True, allow_nan=True).encode("utf-8")


_TRUNCATED = st.builds(
    lambda raw, cut: raw[: max(1, len(raw) - cut)],
    st.builds(_encode_record, _RECORDS),
    st.integers(min_value=1, max_value=60),
)

_LINES = st.one_of(
    st.builds(_encode_record, _RECORDS),
    _TRUNCATED,
    st.binary(max_size=48),  # invalid UTF-8, NUL bytes, arbitrary garbage
    st.sampled_from(
        [
            b"",
            b"   ",
            b"null",
            b"[1, 2, 3]",  # valid JSON, but not an object
            b'{"type": "user"',  # truncated object
            b'{"type": "user", "message": {"role": "user", "content": "\x00"}}',
            b"\xff\xfe\x00\x00",  # invalid UTF-8 / NUL bytes
            b"{" * 200,
        ]
    ),
)

# A whole transcript file: any mix of the above, joined by newlines.
_TRANSCRIPTS = st.lists(_LINES, max_size=12).map(lambda lines: b"\n".join(lines))

_FUZZ = settings(
    max_examples=150,
    deadline=None,  # filesystem work per example; a wall-clock deadline flakes
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

# The ONLY exception a parser entry point may raise on the fuzz corpus.  The
# other documented raise (ValueError for a malformed uuid) cannot occur here:
# every call below passes a structurally valid uuid — so a ValueError escaping
# is a bug, not the contract, and must NOT be swallowed.
_CONTRACT_EXC = FileNotFoundError


def _assert_session(session: object) -> None:
    assert isinstance(session, Session)
    assert isinstance(session.uuid, str)
    assert isinstance(session.title, str)
    assert isinstance(session.path, str)
    assert isinstance(session.message_count, int)
    assert isinstance(session.agent, AgentName)
    assert session.kind in ("agent", "subagent")
    assert isinstance(session.date, datetime)
    # A naive date would poison the cross-agent ``sort(key=lambda s: s.date)``
    # with a TypeError one layer up — parsers pin every timestamp to UTC.
    assert session.date.tzinfo is not None
    assert isinstance(session.models, tuple)


def _assert_sessions(sessions: object) -> None:
    assert isinstance(sessions, list)
    for session in sessions:
        _assert_session(session)


def _assert_messages(messages: object) -> None:
    assert isinstance(messages, list)
    for message in messages:
        assert isinstance(message, Message)
        assert message.role in ("user", "assistant", "tool")
        assert isinstance(message.text, str)
        assert isinstance(message.thinking, str)
        for entry in message.tool_use:
            assert isinstance(entry.get("input", ""), str)
        for entry in message.tool_result:
            assert isinstance(entry.get("content", ""), str)


def _assert_usage(usage: object) -> None:
    if usage is None:
        return
    assert isinstance(usage, dict)
    for key in ("input", "output", "reasoning", "cache_read", "cache_write", "total"):
        assert key in usage


def _assert_bool(value: object) -> None:
    assert isinstance(value, bool)


def _assert_roots(roots: object) -> None:
    assert isinstance(roots, list)
    assert all(isinstance(root, str) for root in roots)


def _exercise(parser: Any, uuid: str, **kwargs: Any) -> None:
    """Call every public entry point of ``parser``; nothing unexpected may escape.

    ``kwargs`` carries the parser's path hook (``base_dir=`` for the JSONL
    parsers, ``override=`` for OpenCode's SQLite store).
    """
    # ``source_roots`` is diagnostics-only and takes ``base_dir`` alone (there
    # is no per-DB override): drop OpenCode's ``override=`` hook for it.
    root_kwargs = {k: v for k, v in kwargs.items() if k == "base_dir"}
    calls: List[Tuple[str, Callable[[], object], Callable[[object], None]]] = [
        ("list_sessions", lambda: parser.list_sessions(**kwargs), _assert_sessions),
        ("search", lambda: parser.search("a", **kwargs), _assert_sessions),
        ("session_exists", lambda: parser.session_exists(uuid, **kwargs), _assert_bool),
        ("source_roots", lambda: parser.source_roots(**root_kwargs), _assert_roots),
        ("read_session", lambda: parser.read_session(uuid, **kwargs), _assert_session),
        ("read_messages", lambda: parser.read_messages(uuid, **kwargs), _assert_messages),
        ("read_token_usage", lambda: parser.read_token_usage(uuid, **kwargs), _assert_usage),
    ]
    for name, call, check in calls:
        try:
            result = call()
        except _CONTRACT_EXC:
            continue
        except Exception as exc:  # the whole point of a fuzz: nothing else escapes
            raise AssertionError(
                f"{parser.__name__}.{name}() raised {type(exc).__name__}: {exc}"
            ) from exc
        check(result)


# ---------------------------------------------------------------------------
# Per-agent fuzz: hostile JSONL under each agent's on-disk layout
# ---------------------------------------------------------------------------


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


@given(body=_TRANSCRIPTS)
@_FUZZ
def test_fuzz_claude(tmp_sessions_dir: Path, body: bytes) -> None:
    """Claude: ``<projects>/<slug>/<uuid>.jsonl`` — the uuid is the file stem."""
    base = tmp_sessions_dir / ".claude" / "projects"
    _write(base / "proj-a" / "fuzz-claude.jsonl", body)
    _exercise(claude, "fuzz-claude", base_dir=str(base))


@given(body=_TRANSCRIPTS)
@_FUZZ
def test_fuzz_codex(tmp_sessions_dir: Path, body: bytes) -> None:
    """Codex: rollout files; the uuid lives in the ``session_meta`` header."""
    base = tmp_sessions_dir / ".codex" / "sessions"
    header = b'{"type": "session_meta", "payload": {"id": "fuzz-codex"}}\n'
    _write(
        base / "2026" / "06" / "14" / "rollout-2026-06-14T10-00-00-fuzz-codex.jsonl",
        header + body,
    )
    _exercise(codex, "fuzz-codex", base_dir=str(base))


@given(body=_TRANSCRIPTS)
@_FUZZ
def test_fuzz_pi(tmp_sessions_dir: Path, body: bytes) -> None:
    """Pi: ``<sessions>/<encoded-cwd>/*.jsonl`` — the uuid comes from the header."""
    base = tmp_sessions_dir / ".pi" / "agent" / "sessions"
    header = b'{"type": "session", "id": "fuzz-pi", "timestamp": "2026-06-14T10:00:00.000Z"}\n'
    _write(base / "--tmp-work--" / "2026-06-14T10-00-00-000Z_fuzz-pi.jsonl", header + body)
    _exercise(pi, "fuzz-pi", base_dir=str(base))


@given(body=_TRANSCRIPTS)
@_FUZZ
def test_fuzz_antigravity(tmp_sessions_dir: Path, body: bytes) -> None:
    """Antigravity: a brain directory whose ``overview.txt`` is JSONL."""
    base = tmp_sessions_dir / ".gemini" / "antigravity" / "brain"
    _write(base / "fuzz-ag" / ".system_generated" / "logs" / "overview.txt", body)
    _exercise(antigravity, "fuzz-ag", base_dir=str(base))


# --- OpenCode: the untrusted payload is JSON inside a SQLite store ----------

_DB_COUNTER = itertools.count()

# SQLite is dynamically typed: an INTEGER-affinity column happily stores a
# non-numeric string or a BLOB, so a corrupt store can hand the parser any of
# these where it expects an epoch / a title.
_DB_CELLS = st.one_of(
    st.none(),
    st.integers(min_value=-(2**63) + 1, max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=20),
    st.binary(max_size=8),
)

_BLOBS = st.one_of(
    st.builds(lambda value: json.dumps(value, ensure_ascii=True), _JSON_VALUES),
    st.text(max_size=30),  # not JSON at all
    st.none(),
)


def _make_db(root: Path, session_row: tuple, message_blob: object, part_blob: object) -> Path:
    """Write a one-session OpenCode DB whose cells / JSON blobs are the fuzz input.

    A fresh file per example on purpose: the parser pools connections by path,
    so reusing one path would serve a stale connection to a rewritten DB.
    """
    db_path = root / f"opencode-{next(_DB_COUNTER)}.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER, directory TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)", session_row)
    conn.execute("INSERT INTO message VALUES ('m-0', 'fuzz-oc', 1, 1, ?)", (message_blob,))
    conn.execute(
        "INSERT INTO part VALUES ('p-0', 'm-0', 'fuzz-oc', 1, 1, ?)",
        (part_blob if part_blob is not None else "",),
    )
    conn.commit()
    conn.close()
    return db_path


@given(
    title=_DB_CELLS,
    created=_DB_CELLS,
    updated=_DB_CELLS,
    parent=_DB_CELLS,
    directory=_DB_CELLS,
    message_blob=_BLOBS,
    part_blob=_BLOBS,
)
@_FUZZ
def test_fuzz_opencode(
    tmp_path: Path,
    title: object,
    created: object,
    updated: object,
    parent: object,
    directory: object,
    message_blob: object,
    part_blob: object,
) -> None:
    """OpenCode: corrupt cells (epochs, title) + corrupt JSON in message/part."""
    db_path = _make_db(
        tmp_path,
        ("fuzz-oc", parent, title, created, updated, directory),
        message_blob,
        part_blob,
    )
    _exercise(opencode, "fuzz-oc", override=str(db_path))


# ---------------------------------------------------------------------------
# Deterministic regressions — the crashes this module found on its first run
# ---------------------------------------------------------------------------

# Deep enough to trip BOTH recursion guards: the interpreter's recursion limit
# (CPython ≤ 3.13 raises RecursionError out of ``json.loads`` at ~1 000 levels)
# and the C-stack guard of the 3.14 scanner (which only trips in the tens of
# thousands).  One constant therefore reproduces the crash on every supported
# Python.
_NESTING = 60_000
_DEEP_JSON = "[" * _NESTING + "]" * _NESTING


def test_deep_nested_line_skipped_claude(tmp_sessions_dir: Path) -> None:
    """A 60k-deep JSON blob escaped as RecursionError out of ``json.loads``."""
    base = tmp_sessions_dir / ".claude" / "projects"
    line = '{"type": "user", "message": {"role": "user", "content": ' + _DEEP_JSON + "}}"
    _write(base / "proj-a" / "deep.jsonl", line.encode("utf-8"))
    _exercise(claude, "deep", base_dir=str(base))


def test_deep_nested_line_skipped_codex(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".codex" / "sessions"
    line = (
        '{"type": "response_item", "payload": {"type": "message", "content": '
        + _DEEP_JSON
        + "}}"
    )
    body = '{"type": "session_meta", "payload": {"id": "deep"}}\n' + line
    _write(
        base / "2026" / "06" / "14" / "rollout-2026-06-14T10-00-00-deep.jsonl",
        body.encode("utf-8"),
    )
    _exercise(codex, "deep", base_dir=str(base))


def test_deep_nested_line_skipped_pi(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".pi" / "agent" / "sessions"
    header = '{"type": "session", "id": "deep", "timestamp": "2026-06-14T10:00:00.000Z"}\n'
    line = '{"type": "message", "message": {"role": "user", "content": ' + _DEEP_JSON + "}}"
    _write(base / "--tmp--" / "2026-06-14T10-00-00-000Z_deep.jsonl", (header + line).encode())
    _exercise(pi, "deep", base_dir=str(base))


def test_deep_nested_line_skipped_antigravity(tmp_sessions_dir: Path) -> None:
    base = tmp_sessions_dir / ".gemini" / "antigravity" / "brain"
    good = '{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}\n'
    line = '{"type": "MODEL_OUTPUT", "content": ' + _DEEP_JSON + "}"
    _write(
        base / "deep" / ".system_generated" / "logs" / "overview.txt",
        (good + line).encode("utf-8"),
    )
    _exercise(antigravity, "deep", base_dir=str(base))


def test_deep_nested_claude_token_usage_line_skipped(tmp_sessions_dir: Path) -> None:
    """``read_token_usage`` carried its own ``json.loads`` loop — same crash."""
    base = tmp_sessions_dir / ".claude" / "projects"
    deep = '{"type": "assistant", "message": {"role": "assistant", "usage": ' + _DEEP_JSON + "}}"
    good = (
        '{"type": "assistant", "message": {"id": "m1", "role": "assistant", '
        '"usage": {"input_tokens": 10, "output_tokens": 5}}}'
    )
    _write(base / "proj-a" / "deepusage.jsonl", (deep + "\n" + good + "\n").encode("utf-8"))
    usage = claude.read_token_usage("deepusage", base_dir=str(base))
    # The deep line is skipped whole; the healthy record after it still counts.
    assert usage is not None
    assert usage["total"] == 15


def test_claude_deep_subagent_meta(tmp_sessions_dir: Path) -> None:
    """The sibling ``.meta.json`` of a subagent transcript is untrusted too."""
    base = tmp_sessions_dir / ".claude" / "projects"
    sub = base / "proj-a" / "parent-1" / "subagents"
    _write(
        sub / "agent-x.jsonl",
        b'{"type": "user", "message": {"role": "user", "content": "hi"}}\n',
    )
    _write(sub / "agent-x.meta.json", ('{"toolUseId": ' + _DEEP_JSON + "}").encode("utf-8"))
    _exercise(claude, "agent-x", base_dir=str(base))


def _oc_db(tmp_path: Path, session_row: tuple) -> Path:
    return _make_db(tmp_path, session_row, '{"role": "user"}', '{"type": "text", "text": "x"}')


def test_opencode_out_of_range_epoch(tmp_path: Path) -> None:
    """int64-max in ``time_updated`` → ValueError("year must be in 1..9999")."""
    db = _oc_db(tmp_path, ("fuzz-oc", None, "t", 1, 2**63 - 1, None))
    _exercise(opencode, "fuzz-oc", override=str(db))
    sessions = opencode.list_sessions(override=str(db))
    assert sessions and sessions[0].date.tzinfo is not None


def test_opencode_negative_epoch(tmp_path: Path) -> None:
    db = _oc_db(tmp_path, ("fuzz-oc", None, "t", 1, -(2**63) + 1, None))
    _exercise(opencode, "fuzz-oc", override=str(db))


def test_opencode_non_numeric_epoch(tmp_path: Path) -> None:
    """SQLite type affinity lets TEXT sit in an INTEGER column → ``str / float``."""
    db = _oc_db(tmp_path, ("fuzz-oc", None, "t", "nope", "nah", None))
    _exercise(opencode, "fuzz-oc", override=str(db))


def test_opencode_blob_title(tmp_path: Path) -> None:
    """A BLOB title leaked bytes into ``Session.title`` → ``search()`` crashed."""
    db = _oc_db(tmp_path, ("fuzz-oc", None, b"\xff\xfe", 1, 1, None))
    _exercise(opencode, "fuzz-oc", override=str(db))
    sessions = opencode.list_sessions(override=str(db))
    assert sessions and isinstance(sessions[0].title, str)


def test_opencode_deep_file_part(tmp_path: Path) -> None:
    """A deeply nested ``file`` part recursed through ``_compact_part_metadata``."""
    deep_part = '{"type": "file", "x": ' + "[" * 1500 + "]" * 1500 + "}"
    db = _make_db(tmp_path, ("fuzz-oc", None, "t", 1, 1, None), '{"role": "assistant"}', deep_part)
    _exercise(opencode, "fuzz-oc", override=str(db))


def test_opencode_deep_message_blob(tmp_path: Path) -> None:
    """A 60k-deep ``message.data`` blob → RecursionError out of ``_json_or_none``."""
    db = _make_db(
        tmp_path,
        ("fuzz-oc", None, "t", 1, 1, None),
        '{"role": "assistant", "tokens": ' + _DEEP_JSON + "}",
        '{"type": "text", "text": "x"}',
    )
    _exercise(opencode, "fuzz-oc", override=str(db))


_EMPTY_FILE_LAYOUTS = {
    "claude": (claude, (".claude", "projects"), "proj-a/nope.jsonl"),
    "codex": (
        codex,
        (".codex", "sessions"),
        "2026/06/14/rollout-2026-06-14T10-00-00-nope.jsonl",
    ),
    "pi": (pi, (".pi", "agent", "sessions"), "--tmp--/2026-06-14T10-00-00-000Z_nope.jsonl"),
    "antigravity": (
        antigravity,
        (".gemini", "antigravity", "brain"),
        "nope/.system_generated/logs/overview.txt",
    ),
}


@pytest.mark.parametrize("agent", sorted(_EMPTY_FILE_LAYOUTS))
def test_empty_file_is_skipped(tmp_sessions_dir: Path, agent: str) -> None:
    """A 0-byte transcript — the agent died before its first write."""
    parser, root_parts, rel = _EMPTY_FILE_LAYOUTS[agent]
    base = tmp_sessions_dir.joinpath(*root_parts)
    _write(base / rel, b"")
    _exercise(parser, "nope", base_dir=str(base))
