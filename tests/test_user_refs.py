"""Q1 ``user_ref`` extraction + surface tests.

A *user reference* is an external source the USER attached in their own turn
(a link, a file path, an @-mention, an image/attachment, or an IDE-injected
``<ide_*>`` context tag).  ``ai_r.user_refs`` extracts them from prose; the
parsers surface structured attachments on :attr:`Message.user_refs`; the event
layer folds both into ``user_turn`` refs (``{"user_ref": {...}}``) and hoists
them onto query rows (``user_refs`` / ``user_ref_kinds``); ``query`` exposes a
``user_ref`` facet and ``aggregate`` buckets by ``user_ref_kinds``.

Hermetic: the pure ``extract_*``/``dedup_*`` helpers need no data; every
end-to-end case writes a synthetic session under the per-test ``AI_R_HOME``
(auto-set by the conftest hermetic-env fixture) and reads it back through the
public ``query`` / ``aggregate`` surface — nothing touches the real vault.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_r.user_refs import (
    dedup_user_refs,
    extract_user_refs_from_text,
    make_user_ref,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# extract_user_refs_from_text — prose-embedded references
# ---------------------------------------------------------------------------


def test_extract_ide_opened_file_tag() -> None:
    text = (
        "<ide_opened_file>The user opened the file /repo/src/app.py in the "
        "IDE. This may or may not be related.</ide_opened_file>"
    )
    refs = extract_user_refs_from_text(text)
    assert refs == [make_user_ref("ide_context", "/repo/src/app.py", "text")]


def test_extract_doc_path_tag() -> None:
    text = '<doc path="/repo/notes/design.md">…rendered contents…</doc>'
    refs = extract_user_refs_from_text(text)
    assert refs == [make_user_ref("file", "/repo/notes/design.md", "text")]


def test_extract_bare_url() -> None:
    refs = extract_user_refs_from_text("please look at https://example.com/x here")
    assert refs == [make_user_ref("url", "https://example.com/x", "text")]


def test_extract_at_mention_path_shaped() -> None:
    refs = extract_user_refs_from_text("open @src/widget.py and fix it")
    assert refs == [make_user_ref("file", "src/widget.py", "text")]


def test_extract_bare_at_handle_is_not_a_ref() -> None:
    """A bare ``@handle`` with no path shape must NOT masquerade as a file."""
    assert extract_user_refs_from_text("ping @someone about this") == []


def test_extract_url_inside_fenced_code_is_ignored() -> None:
    """A URL inside a ``` fence is a code sample, not an attached source."""
    text = "example config:\n```\nendpoint = https://secret.internal/api\n```\n"
    assert extract_user_refs_from_text(text) == []


def test_extract_url_outside_fence_still_found_alongside_fence() -> None:
    """The fence is blanked, but a URL in the surrounding prose survives."""
    text = (
        "see https://example.com/doc\n"
        "```\nhttps://inside.fence/ignored\n```\n"
    )
    refs = extract_user_refs_from_text(text)
    targets = [r["target"] for r in refs]
    assert targets == ["https://example.com/doc"]


def test_extract_empty_and_non_string() -> None:
    assert extract_user_refs_from_text("") == []
    assert extract_user_refs_from_text(None) == []


# ---------------------------------------------------------------------------
# dedup_user_refs — structured wins over text for the same target
# ---------------------------------------------------------------------------


def test_dedup_prefers_structured_over_text() -> None:
    text_ref = make_user_ref("file", "src/app.py", "text")
    struct_ref = make_user_ref("file", "src/app.py", "structured")
    # text seen first, structured second → the survivor is the structured one.
    out = dedup_user_refs([text_ref, struct_ref])
    assert out == [make_user_ref("file", "src/app.py", "structured")]


def test_dedup_keeps_targetless_refs() -> None:
    """Two unnamed inline images cannot be de-duplicated — both survive."""
    img = make_user_ref("image", None, "structured")
    out = dedup_user_refs([img, dict(img)])
    assert len(out) == 2


def test_make_user_ref_rejects_bad_vocabulary() -> None:
    with pytest.raises(ValueError):
        make_user_ref("bogus", "/x", "text")
    with pytest.raises(ValueError):
        make_user_ref("file", "/x", "bogus")


# ---------------------------------------------------------------------------
# Parsers — structured (origin="structured") attachments
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_image_session(tmp_sessions_dir: Path) -> str:
    """Claude session: a user turn with an image part + an assistant image.

    The user's image part (role user) becomes a structured ``image``
    user_ref with no target; the assistant's image part must NOT.
    """
    sid = "uref-claude-img"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-u" / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "image",
                         "source": {"type": "base64", "data": "aQ=="}},
                        {"type": "text", "text": "what is in this picture?"},
                    ],
                },
                "timestamp": "2026-06-14T10:00:00Z", "sessionId": sid,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "image",
                         "source": {"type": "base64", "data": "Zg=="}},
                        {"type": "text", "text": "a widget"},
                    ],
                },
                "timestamp": "2026-06-14T10:00:05Z", "sessionId": sid,
            },
        ],
    )
    return sid


def test_claude_user_image_becomes_structured_ref(
    claude_image_session: str,
) -> None:
    from ai_r.events import query

    rows = query(session=claude_image_session)
    user = next(r for r in rows if r["type"] == "user_turn")
    assert user["user_refs"] == [make_user_ref("image", None, "structured")]
    assert user["user_ref_kinds"] == ["image"]
    # The assistant turn carries no user_ref at all.
    asst = next(r for r in rows if r["type"] == "assistant_turn")
    assert "user_refs" not in asst


@pytest.fixture
def codex_image_session(tmp_sessions_dir: Path) -> str:
    """Codex rollout whose user message carries an ``input_image`` part."""
    uuid = "uref-codex-img"
    jsonl = (
        tmp_sessions_dir / ".codex" / "sessions" / "2026" / "06" / "14"
        / f"rollout-2026-06-14T10-00-00-{uuid}.jsonl"
    )
    _write_jsonl(
        jsonl,
        [
            {"timestamp": "2026-06-14T10:00:00Z", "type": "session_meta",
             "payload": {"id": uuid, "cwd": "/tmp/work",
                         "timestamp": "2026-06-14T10:00:00Z"}},
            {"timestamp": "2026-06-14T10:00:02Z", "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_image",
                  "image_url": "data:image/png;base64,iVBOR"},
                 {"type": "text", "text": "describe this"}]}},
        ],
    )
    return uuid


def test_codex_user_image_becomes_structured_ref(
    codex_image_session: str,
) -> None:
    from ai_r.events import query

    rows = query(session=codex_image_session)
    user = next(r for r in rows if r["type"] == "user_turn")
    assert user["user_refs"] == [make_user_ref("image", None, "structured")]


@pytest.fixture
def opencode_file_attach_db(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """OpenCode DB: a user msg with a ``file`` part (image + document) and an
    assistant msg with a ``patch``/``file`` part.

    The USER file parts must route to ``user_refs`` (image/* → image,
    otherwise file, target = filename); the ASSISTANT patch/file must stay
    as agent ``tool_use`` (the regression guard for the mis-filed-upload bug).
    """
    db = tmp_sessions_dir / "opencode-uref.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        """
    )
    conn.execute("INSERT INTO session VALUES ('oc-uref', NULL, 'attach', 1, 9)")
    conn.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        [
            ("um", "oc-uref", 2, 2, json.dumps({"role": "user"})),
            ("am", "oc-uref", 5, 5, json.dumps({"role": "assistant"})),
        ],
    )
    conn.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        [
            # user: text + an image upload + a document upload
            ("um-p0", "um", "oc-uref", 2, 2,
             json.dumps({"type": "text", "text": "review these"})),
            ("um-p1", "um", "oc-uref", 3, 3,
             json.dumps({"type": "file", "mime": "image/png",
                         "filename": "diagram.png",
                         "url": "data:image/png;base64,iVBOR"})),
            ("um-p2", "um", "oc-uref", 4, 4,
             json.dumps({"type": "file", "mime": "application/pdf",
                         "filename": "spec.pdf",
                         "url": "data:application/pdf;base64,JVBER"})),
            # assistant: an applied patch + a produced file (agent artifacts)
            ("am-p0", "am", "oc-uref", 6, 6,
             json.dumps({"type": "patch", "hash": "abc",
                         "files": [{"path": "src/x.py", "added": 1,
                                    "removed": 0}]})),
            ("am-p1", "am", "oc-uref", 7, 7,
             json.dumps({"type": "file", "mime": "image/png",
                         "filename": "out.png",
                         "url": "data:image/png;base64,iVBOR"})),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    return "oc-uref"


def test_opencode_user_file_parts_become_refs_assistant_stays_tool(
    opencode_file_attach_db: str,
) -> None:
    from ai_r.events import query

    rows = query(session=opencode_file_attach_db)
    user = next(r for r in rows if r["type"] == "user_turn")
    # image/* → image, other mime → file; target = filename.
    assert make_user_ref("image", "diagram.png", "structured") in user["user_refs"]
    assert make_user_ref("file", "spec.pdf", "structured") in user["user_refs"]
    assert sorted(user["user_ref_kinds"]) == ["file", "image"]

    # The assistant's patch/file are agent artifacts → tool_call events, and
    # they must NOT surface as user_refs anywhere.
    assert not any("user_refs" in r for r in rows if r["type"] != "user_turn")
    tool_names = [
        r["text"] for r in rows if r["type"].startswith("tool_call")
    ]
    assert "patch" in tool_names


# ---------------------------------------------------------------------------
# query — the ``user_ref`` facet (any / kind / substring / empty)
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_mixed_refs_session(tmp_sessions_dir: Path) -> str:
    """Claude session with three user turns: a URL turn, a file @-mention
    turn, and a plain turn with no reference.  Enough for every facet mode.
    """
    sid = "uref-claude-mixed"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-u" / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "user",
             "message": {"role": "user",
                         "content": "check https://example.com/report please"},
             "timestamp": "2026-06-14T10:00:00Z", "sessionId": sid},
            {"type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "looking"}]},
             "timestamp": "2026-06-14T10:00:02Z", "sessionId": sid},
            {"type": "user",
             "message": {"role": "user", "content": "now edit @src/main.py"},
             "timestamp": "2026-06-14T10:00:04Z", "sessionId": sid},
            {"type": "user",
             "message": {"role": "user", "content": "thanks, that works"},
             "timestamp": "2026-06-14T10:00:06Z", "sessionId": sid},
        ],
    )
    return sid


def test_query_user_ref_any(claude_mixed_refs_session: str) -> None:
    from ai_r.events import query

    hits = query(session=claude_mixed_refs_session, user_ref="any")
    # The URL turn and the @-mention turn qualify; the plain turn does not.
    texts = sorted(r["text"] for r in hits)
    assert len(hits) == 2
    assert any("https://example.com/report" in t for t in texts)
    assert any("@src/main.py" in t for t in texts)


def test_query_user_ref_by_kind(claude_mixed_refs_session: str) -> None:
    from ai_r.events import query

    urls = query(session=claude_mixed_refs_session, user_ref="url")
    assert len(urls) == 1
    assert "https://example.com/report" in urls[0]["text"]

    files = query(session=claude_mixed_refs_session, user_ref="file")
    assert len(files) == 1
    assert "@src/main.py" in files[0]["text"]


def test_query_user_ref_by_target_substring(
    claude_mixed_refs_session: str,
) -> None:
    from ai_r.events import query

    hits = query(session=claude_mixed_refs_session, user_ref="main.py")
    assert len(hits) == 1
    assert "@src/main.py" in hits[0]["text"]
    # A substring nobody references → honest empty.
    assert query(session=claude_mixed_refs_session, user_ref="nowhere") == []


def test_query_user_ref_empty_string_fails_loud() -> None:
    from ai_r.events import query

    with pytest.raises(ValueError, match="user_ref"):
        query(user_ref="")


def test_query_user_ref_on_non_user_type_is_empty(
    claude_mixed_refs_session: str,
) -> None:
    """Non-user events never carry a user_ref → combining with a
    non-``user_turn`` type is an honest empty result."""
    from ai_r.events import query

    assert query(
        session=claude_mixed_refs_session,
        type="assistant_turn",
        user_ref="any",
    ) == []


# ---------------------------------------------------------------------------
# aggregate — group_by="user_ref_kinds"
# ---------------------------------------------------------------------------


def test_aggregate_group_by_user_ref_kinds(
    claude_mixed_refs_session: str,
) -> None:
    from ai_r.events import aggregate, query

    rows = query(session=claude_mixed_refs_session)
    result = aggregate(rows, group_by="user_ref_kinds", metrics=("count",))
    counts = {g["group"]: g["count"] for g in result["groups"]}
    # ``user_ref_kinds`` is a list field, so aggregate EXPLODES it: a turn's
    # row lands in the bucket of EVERY kind it carries.  Here the fixture has
    # one url turn and one file turn (each a single kind), so each kind bucket
    # holds exactly that one turn; the no-signal rows (assistant turn, plain
    # user turn) carry no ``user_ref_kinds`` and fold into "(unknown)".
    assert counts["url"] == 1
    assert counts["file"] == 1
    assert counts["(unknown)"] >= 1
    # No combined ``['file', 'url']`` bucket exists — the list is exploded.
    assert str(["url"]) not in counts and str(["file"]) not in counts
    # ``totals`` fold the UNduplicated row set (never the exploded sum).
    assert result["totals"]["count"] == len(rows)


def test_aggregate_group_by_user_ref_kinds_multi_kind_explodes(
    tmp_sessions_dir: Path,
) -> None:
    """A single turn referencing BOTH a url and a file is counted under each
    kind — the explode, not one combined ``['file', 'url']`` bucket."""
    from ai_r.events import aggregate, query

    sid = "uref-claude-multikind"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-mk" / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            # one turn, two kinds: a url AND a file @-mention in the same message
            {"type": "user",
             "message": {"role": "user",
                         "content": "compare https://ex.com/a with @src/b.py"},
             "timestamp": "2026-06-14T11:00:00Z", "sessionId": sid},
            # a single-file turn, to show the multi-kind turn ADDS to ``file``
            {"type": "user",
             "message": {"role": "user", "content": "then edit @src/c.py"},
             "timestamp": "2026-06-14T11:00:02Z", "sessionId": sid},
            {"type": "user",
             "message": {"role": "user", "content": "no ref here"},
             "timestamp": "2026-06-14T11:00:04Z", "sessionId": sid},
        ],
    )
    rows = query(session=sid)
    # sanity: the first turn really did resolve to two kinds
    multi = next(r for r in rows if r.get("user_ref_kinds") == ["file", "url"])
    assert multi is not None
    result = aggregate(rows, group_by="user_ref_kinds", metrics=("count",))
    counts = {g["group"]: g["count"] for g in result["groups"]}
    # The multi-kind turn lands in BOTH ``file`` and ``url``; the single-file
    # turn adds to ``file`` → file=2, url=1; the plain turn → "(unknown)".
    assert counts["file"] == 2
    assert counts["url"] == 1
    assert counts["(unknown)"] == 1
    # Explode overcounts vs rows on purpose — the sum exceeds the row count.
    assert sum(counts.values()) == 4
    assert sum(counts.values()) > len(rows)
    # ``totals`` stay honest: the unduplicated row set, never the exploded 4.
    assert result["totals"]["count"] == len(rows)


# ---------------------------------------------------------------------------
# dedup end-to-end: same target via structured part + prose @-mention
# ---------------------------------------------------------------------------


@pytest.fixture
def opencode_dup_ref_db(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """OpenCode user msg attaching ``src/app.py`` as a structured file part
    AND @-mentioning the same path in prose → one deduped ref (structured)."""
    db = tmp_sessions_dir / "opencode-dup.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        """
    )
    conn.execute("INSERT INTO session VALUES ('oc-dup', NULL, 'dup', 1, 9)")
    conn.execute(
        "INSERT INTO message VALUES ('um', 'oc-dup', 2, 2, ?)",
        (json.dumps({"role": "user"}),),
    )
    conn.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("um-p0", "um", "oc-dup", 2, 2,
             json.dumps({"type": "text",
                         "text": "please refactor @src/app.py"})),
            ("um-p1", "um", "oc-dup", 3, 3,
             json.dumps({"type": "file", "mime": "text/x-python",
                         "filename": "src/app.py",
                         "url": "data:text/plain;base64,cA=="})),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    return "oc-dup"


def test_dedup_structured_and_text_same_target_end_to_end(
    opencode_dup_ref_db: str,
) -> None:
    from ai_r.events import query

    rows = query(session=opencode_dup_ref_db)
    user = next(r for r in rows if r["type"] == "user_turn")
    # The structured part and the prose @-mention name the SAME target →
    # exactly ONE ref survives, and it is the structured one.
    same = [u for u in user["user_refs"] if u.get("target") == "src/app.py"]
    assert len(same) == 1
    assert same[0]["origin"] == "structured"


# ---------------------------------------------------------------------------
# negative guard: an AGENT tool-call file ref is NOT a user_ref
# ---------------------------------------------------------------------------


def test_agent_tool_call_file_is_not_user_ref(tmp_sessions_dir: Path) -> None:
    """An assistant ``Edit`` carries a ``file`` ref on its tool_call event, but
    that must never be hoisted onto ``user_refs`` — user_refs are user-only."""
    from ai_r.events import query

    sid = "uref-agent-edit"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-u" / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "user",
             "message": {"role": "user", "content": "fix the bug"},
             "timestamp": "2026-06-14T10:00:00Z", "sessionId": sid},
            {"type": "assistant",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Edit", "input": {
                     "file_path": "/repo/src/thing.py",
                     "old_string": "a", "new_string": "b"}}]},
             "timestamp": "2026-06-14T10:00:05Z", "sessionId": sid},
        ],
    )
    rows = query(session=sid)
    edit = next(r for r in rows if r["type"].startswith("tool_call"))
    # The tool_call carries a file ref …
    assert any("file" in ref for ref in edit["refs"])
    # … but no user_ref hoist on ANY row (the only user turn had no reference).
    assert not any("user_refs" in r for r in rows)


# ---------------------------------------------------------------------------
# redaction: a secret inside a user_ref.target is masked on emission only
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_secret_url_ref(tmp_sessions_dir: Path) -> str:
    """Claude user turn whose attached URL embeds a token in its query string."""
    sid = "uref-secret-url"
    jsonl = tmp_sessions_dir / ".claude" / "projects" / "proj-u" / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "user",
             "message": {"role": "user", "content": (
                 "fetch https://api.example.com/d?token="
                 "ghp_0123456789abcdef0123456789abcdef0123")},
             "timestamp": "2026-06-14T10:00:00Z", "sessionId": sid},
        ],
    )
    return sid


def test_user_ref_target_redacted_on_emission(
    claude_secret_url_ref: str,
) -> None:
    from ai_r.events import query

    # redact=True (default): the token in the ref target is masked …
    redacted = query(session=claude_secret_url_ref, user_ref="any", redact=True)
    target = redacted[0]["user_refs"][0]["target"]
    assert "ghp_0123456789abcdef" not in target
    assert "REDACTED" in target

    # redact=False: the raw target is returned verbatim …
    raw = query(session=claude_secret_url_ref, user_ref="any", redact=False)
    assert "ghp_0123456789abcdef0123456789abcdef0123" in (
        raw[0]["user_refs"][0]["target"]
    )

    # … and the redacted read did NOT mutate the underlying Event: a fresh
    # raw read still sees the secret (emission-time masking, not in place).
    raw_again = query(session=claude_secret_url_ref, user_ref="any", redact=False)
    assert "ghp_0123456789abcdef0123456789abcdef0123" in (
        raw_again[0]["user_refs"][0]["target"]
    )
