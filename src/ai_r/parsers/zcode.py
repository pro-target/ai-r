"""ZCode session parser.

ZCode (the ZCode CLI harness) keeps its session store under ``~/.zcode/cli``:

* **SQLite store** — ``~/.zcode/cli/db/db.sqlite`` — the canonical registry.
  Every session lives here, including main interactive ones (the rollout
  JSONL below only persists *some* subagent model traffic on the installs
  we observed), with real titles, ``parent_id`` links and the project
  ``directory``.  Queried read-only (WAL-aware ``mode=ro`` URI).
* **Rollout model-io JSONL** — ``~/.zcode/cli/rollout/model-io-sess_*.jsonl``
  — one line per model call: ``{sessionId, querySource, startedAt,
  completedAt, model: {modelId, providerId}, request: {messages,
  messagesKind: full|delta|tail, messageOffset, messageCount, body},
  response: {text, reasoningText, toolCalls, usage}}``.  ``request.messages``
  is the cumulative input of that call; the file is periodically compacted
  (rewritten), so records are replayed snapshot-style.  Used as a FALLBACK
  for sessions the DB does not know (the DB stays the source of truth).
* **Subagent metadata** — ``~/.zcode/cli/agents/sess_<parent>/agent_<uuid>/
  metadata.json`` — carries ``childSessionId`` → ``parentSessionId`` for
  rollout-only sessions (the rollout filename holds only the child uuid).

SQLite schema (relevant columns; mirrors OpenCode's layout closely)::

    session(id, parent_id, title, directory, task_type, time_created,
            time_updated, ...)
    message(id, session_id, time_created, time_updated, data, sequence)
    part(id, message_id, session_id, time_created, time_updated, data,
         sequence)

``message.data`` carries metadata (``role``, ``modelId``, ``providerId``,
``tokens``, ``time``); the bodies live in ``part`` rows linked by
``message_id``.  Observed ``part.data`` shapes:

* ``text``       — ``{"type":"text","text":"..."}``
* ``reasoning``  — ``{"type":"reasoning","text":"..."}``
* ``tool``       — combined call+result: ``{"type":"tool","tool":"<name>",
  "callID":"...","state":{"status":"completed|error|running",
  "input":{...}, "output":"...", "error":"..."}}``.  An
  ``AskUserQuestion`` part follows Claude's shape — questions in
  ``state.input.questions``, the user's choice serialized in the
  ``state.output`` result string — and is additionally surfaced as
  :attr:`Message.qa` on the same (assistant) message.
* ``step-start`` / ``step-finish`` / ``timeline`` — boundaries, skipped.
  ``file`` / ``patch`` parts are NOT observed in the store (as of
  2026-09) — there is no user-attachment signal to map, so they stay
  skipped rather than guessed.

Multi-part messages share one ``time_created`` millisecond; ``sequence``
is the per-message part ordinal and the load-bearing tie-breaker.

The base directory can be overridden by ``base_dir`` (a directory
containing ``db.sqlite``), the ``override`` argument (a direct DB path),
``$ZCODE_DB``, or ``$AI_R_HOME`` (treated as ``$HOME`` for the call —
the standard parser testing hook).
"""

from __future__ import annotations

import dataclasses
import glob
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from ._common import (
    _is_valid_uuid,
    _normalise_title,
    _parse_iso_timestamp,
    _qa_entry,
    _qa_options_from_question,
    _qa_pairs_from_result_text,
    fold_orphan_thinking,
    iter_jsonl_records,
)
from .models import AgentName, Message, Session

__all__ = [
    "list_sessions",
    "read_session",
    "read_messages",
    "read_token_usage",
    "search",
    "session_exists",
    "source_roots",
]


_DEFAULT_DB = "~/.zcode/cli/db/db.sqlite"
_DEFAULT_ROLLOUT = "~/.zcode/cli/rollout"
_DEFAULT_AGENTS = "~/.zcode/cli/agents"

_TITLE_MAX_LEN = 100

# Lock retry backoffs (seconds) for the read-only open.  The ZCode app
# writes the DB through WAL; ``mode=ro`` coexists with that, but a busy
# moment can still throw — one retry each, then give up honestly.
_OPEN_BACKOFFS = (0.0, 0.25, 0.5)

_EPOCH_ZERO = datetime.fromtimestamp(0, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _home_dir() -> Path:
    """Resolve ``$HOME`` honouring the ``AI_R_HOME`` testing hook."""
    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path("~").expanduser()


def _resolve_db_paths(
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[str]:
    """Return existing ZCode DB paths, in priority order.

    Priority: ``override`` → ``$ZCODE_DB`` → ``base_dir/db.sqlite`` →
    ``<home>/.zcode/cli/db/db.sqlite`` (where ``<home>`` honours
    ``$AI_R_HOME``).  Only files that exist are returned.
    """
    candidates: List[str] = []

    def _add(path: object) -> None:
        if not path:
            return
        expanded = str(Path(str(path)).expanduser())
        if os.path.isfile(expanded) and expanded not in candidates:
            candidates.append(expanded)

    if override:
        _add(override)
    env_override = os.environ.get("ZCODE_DB")
    if env_override:
        _add(env_override)
    if base_dir:
        _add(os.path.join(str(Path(base_dir).expanduser()), "db.sqlite"))
    _add(_home_dir() / ".zcode" / "cli" / "db" / "db.sqlite")
    return candidates


def _rollout_dir() -> Path:
    """The rollout model-io directory under the resolved home."""
    return _home_dir() / ".zcode" / "cli" / "rollout"


def _agents_dir() -> Path:
    """The per-session subagent metadata directory under the resolved home."""
    return _home_dir() / ".zcode" / "cli" / "agents"


def source_roots(base_dir: Optional[str] = None) -> List[str]:
    """Candidate ZCode source roots (DB + rollout dir), existing or default.

    Used by :mod:`ai_r.diagnostics` to explain empty results: unlike
    :func:`_resolve_db_paths` this also returns the *looked-at* locations
    when nothing exists.
    """
    roots: List[str] = []
    existing = _resolve_db_paths(base_dir)
    if existing:
        roots.extend(existing)
    else:
        env_override = os.environ.get("ZCODE_DB")
        if env_override:
            roots.append(str(Path(env_override).expanduser()))
        if base_dir:
            roots.append(
                os.path.join(str(Path(base_dir).expanduser()), "db.sqlite")
            )
        roots.append(
            str(_home_dir() / ".zcode" / "cli" / "db" / "db.sqlite")
        )
    rollout = _rollout_dir()
    if rollout.is_dir():
        roots.append(str(rollout))
    return roots


def _open_db(db_path: str) -> Optional[sqlite3.Connection]:
    """Open the ZCode DB read-only with a short lock-retry, or ``None``."""
    for backoff in _OPEN_BACKOFFS:
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            conn.execute("PRAGMA busy_timeout = 10000")
            return conn
        except sqlite3.Error:
            if backoff:
                time.sleep(backoff)
            continue
    return None


def _epoch_ms_to_datetime(ms: object) -> Optional[datetime]:
    """Epoch-millis → tz-aware datetime, or ``None`` for an unusable cell."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _json_or_none(blob: object) -> Optional[dict]:
    if not isinstance(blob, str) or not blob.strip():
        return None
    try:
        rec = json.loads(blob)
    except (TypeError, ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def _stringify(value: object) -> str:
    """Best-effort serialise a tool input/output value to a string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# SQLite layer (canonical)
# ---------------------------------------------------------------------------

_SELECT_ALL_SESSIONS = (
    "SELECT id, parent_id, title, directory, time_created, time_updated "
    "FROM session ORDER BY time_updated DESC"
)
_SELECT_SESSION = (
    "SELECT id, parent_id, title, directory, time_created, time_updated "
    "FROM session WHERE id = ?"
)
_SELECT_MESSAGE_COUNT = "SELECT COUNT(*) FROM message WHERE session_id = ?"
_SELECT_MESSAGE_DATA = (
    "SELECT data FROM message WHERE session_id = ? "
    "ORDER BY time_created, id"
)
# Order: message rows by time (``sequence`` correlates with it and old rows
# may NULL it), then parts by time with the per-message ``sequence`` ordinal
# as the tie-breaker — multi-part messages share one millisecond.
_SELECT_MESSAGES_WITH_PARTS = (
    "SELECT m.id AS mid, m.time_created AS mtime, m.data AS mdata, "
    "p.id AS pid, p.data AS pdata, p.time_created AS ptime "
    "FROM message m "
    "LEFT JOIN part p ON p.message_id = m.id "
    "WHERE m.session_id = ? "
    "ORDER BY m.time_created, m.id, p.time_created, "
    "COALESCE(p.sequence, 2147483647), p.id"
)


def _row_to_session(
    row: sqlite3.Row, db_path: str, subagent_meta: Optional[dict] = None
) -> Session:
    """Map one ``session`` row to a :class:`Session`.

    ``subagent_meta`` (from :func:`_subagent_meta_map`, keyed by session
    id) enriches a child row's ``extra`` with the spawn facts the agents
    directory records — ``subagent_type`` (``profileId``) and
    ``spawn_tool_use_id`` (``parentToolUseId``) — so MCP
    ``include_subagents`` / ``subagent_cost_facts`` can join the child to
    its spawning call.  Absent keys stay absent (never fabricated).
    """
    sid = row["id"]
    title_raw = row["title"]
    directory = row["directory"]
    parent_id = row["parent_id"]
    date = (
        _epoch_ms_to_datetime(row["time_updated"] or row["time_created"] or 0)
        or _EPOCH_ZERO
    )
    clean_title = (
        title_raw.strip() if isinstance(title_raw, str) else ""
    ) or "Untitled"
    directory = (
        directory if isinstance(directory, str) and directory.strip() else None
    )
    parent = parent_id if isinstance(parent_id, str) and parent_id else None
    extra: dict = {
        "time_created": row["time_created"],
        "time_updated": row["time_updated"],
    }
    meta = subagent_meta.get(sid) if (parent and subagent_meta) else None
    if isinstance(meta, dict):
        for key in ("subagent_type", "spawn_tool_use_id"):
            val = meta.get(key)
            if isinstance(val, str) and val:
                extra[key] = val
    return Session(
        uuid=sid if isinstance(sid, str) else "",
        agent=AgentName.ZCODE,
        title=clean_title[:_TITLE_MAX_LEN],
        date=date,
        path=db_path,
        message_count=0,  # filled by the caller
        parent_uuid=parent,
        kind="subagent" if parent else "agent",
        project_dir=directory,
        launch_surface=None,
        extra=extra,
    )


def _message_model(message_data: Optional[dict]) -> Optional[str]:
    """The assistant row's ``modelId``, or ``None`` without signal."""
    if not isinstance(message_data, dict):
        return None
    model = message_data.get("modelId")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _session_models(cursor: sqlite3.Cursor, sid: str) -> Tuple[str, ...]:
    """Unique assistant ``modelId`` values for one session, in order."""
    models: List[str] = []
    for (blob,) in cursor.execute(_SELECT_MESSAGE_DATA, (sid,)):
        data = _json_or_none(blob)
        if data is None or data.get("role") != "assistant":
            continue
        model = _message_model(data)
        if model is not None and model not in models:
            models.append(model)
    return tuple(models)


def _normalize_tokens(tokens: object) -> Optional[dict]:
    """Normalize a zcode ``message.data.tokens`` block (or ``None``).

    ZCode records per-model-call usage: ``{"total", "input", "output",
    "reasoning", "cache": {"read", "write"}}`` (``input`` includes the
    cache-read part; the recorded ``total`` is ``input + output``).
    ``total`` prefers the RECORDED value (never re-sums what the format
    already summed) and falls back to ``input + output``.  Returns
    ``None`` for a non-dict block or an all-zero placeholder (the
    incomplete-message case) — absence is honest.
    """
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}

    def _count(key: str, src: dict) -> Optional[int]:
        val = src.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return val
        return None

    block = {
        "input": _count("input", tokens),
        "output": _count("output", tokens),
        "reasoning": _count("reasoning", tokens),
        "cache_read": _count("read", cache),
        "cache_write": _count("write", cache),
    }
    total = _count("total", tokens)
    if total is None:
        total = (block["input"] or 0) + (block["output"] or 0)
    if total <= 0:
        return None
    return {**block, "total": total}


def _role_from_message_data(message_data: Optional[dict]) -> Optional[str]:
    if not isinstance(message_data, dict):
        return None
    raw = message_data.get("role")
    if not isinstance(raw, str):
        return None
    role = raw.lower()
    if role in ("user", "assistant", "tool"):
        return role
    return None


def _qa_from_ask_part(state: dict) -> List[dict]:
    """Build ``qa`` entries from one ``AskUserQuestion`` tool part's state.

    ZCode records an interactive question exactly like Claude: the offered
    questions live in ``state.input.questions`` (the shared structured
    shape) and the user's choice ONLY in the ``state.output`` result string
    (``User has answered your questions: "q"="a", ...``) — there is no
    separate answers structure (OpenCode's ``state.metadata.answers``) and
    no following user-role record.  Each parsed answer pair is enriched
    with the options offered for that question; a question whose answer
    did not parse (dismissed / truncated output) is not invented.
    """
    inp = state.get("input")
    questions = inp.get("questions") if isinstance(inp, dict) else None
    if not isinstance(questions, list):
        return []
    output = state.get("output")
    pairs = _qa_pairs_from_result_text(
        output if isinstance(output, str) else ""
    )
    if not pairs:
        return []
    opts_by_q: dict = {}
    for q in questions:
        if isinstance(q, dict):
            qtext = q.get("question")
            if isinstance(qtext, str):
                opts_by_q[qtext.strip()] = _qa_options_from_question(q)
    return [
        _qa_entry(q_text, opts_by_q.get(q_text, ()), answer)
        for q_text, answer in pairs
    ]


def _build_message(
    message_data: Optional[dict],
    parts: List[Tuple[dict, Optional[int]]],
    timestamp: Optional[datetime] = None,
) -> Optional[Message]:
    """Assemble a :class:`Message` from metadata + ordered part rows.

    ``parts`` carries ``(part_data, part_time_created_ms)`` pairs.  Mirrors
    the OpenCode part vocabulary (the stores share a layout): ``text`` →
    text, ``reasoning`` → thinking, ``tool`` → a ``tool_use`` entry plus a
    ``tool_result`` entry (``is_error`` from ``state.status == "error"``;
    the ``state.error`` string is surfaced as the result content when the
    errored call produced no output).  Each ``tool_use`` entry carries the
    originating part's own ``time_created`` as ``timestamp`` (``None`` when
    the row lacked a part time) — the per-call instant, not the message's.
    ``step-*`` / ``timeline`` boundary parts are skipped.
    """
    role = _role_from_message_data(message_data)
    if role is None:
        return None

    text_chunks: List[str] = []
    thinking_chunks: List[str] = []
    tool_use: List[dict] = []
    tool_result: List[dict] = []
    qa: List[dict] = []

    for part, ptime in parts:
        ts_for_entry: Optional[datetime] = (
            _epoch_ms_to_datetime(ptime) if isinstance(ptime, int) else None
        )
        ptype = part.get("type", "")
        if ptype in ("text", "reasoning"):
            t = part.get("text", "")
            if isinstance(t, str) and t:
                (
                    thinking_chunks if ptype == "reasoning" else text_chunks
                ).append(t)
        elif ptype == "tool":
            name = part.get("tool") or part.get("toolName") or ""
            state_raw = part.get("state")
            state = state_raw if isinstance(state_raw, dict) else {}
            inp = state.get("input")
            call_id = part.get("callID") or part.get("callId")
            tu_entry: dict = {
                "name": name,
                "input": _stringify(inp),
                "timestamp": ts_for_entry,
            }
            if isinstance(call_id, str) and call_id:
                tu_entry["tool_use_id"] = call_id
            tool_use.append(tu_entry)
            output = state.get("output")
            is_error = state.get("status") == "error"
            if output is None and is_error:
                # An errored call frequently carries no ``output``; the
                # ``state.error`` string IS the result — surface it.
                err = state.get("error")
                output = err if isinstance(err, str) and err else None
            if output is not None or is_error:
                tr_entry: dict = {
                    "content": _stringify(output) if output is not None else "",
                    "is_error": is_error,
                }
                if isinstance(call_id, str) and call_id:
                    tr_entry["tool_use_id"] = call_id
                tool_result.append(tr_entry)
            # ZCode's ``AskUserQuestion`` is its interactive-question
            # surface (Claude's tool, combined call+result in ONE part):
            # questions in ``state.input``, the chosen answers serialized
            # into the ``state.output`` result string → pair them into
            # ``qa`` on this same assistant message.
            if name == "AskUserQuestion":
                qa.extend(_qa_from_ask_part(state))
        # step-start / step-finish / timeline / unknown → skip
        # (``file``/``patch`` parts are NOT observed in the ZCode store
        # as of 2026-09 — no user-ref signal exists to map; honest skip.)

    tokens = (
        _normalize_tokens(message_data.get("tokens"))
        if isinstance(message_data, dict)
        else None
    )

    return Message(
        role=role,
        text="\n".join(text_chunks),
        tool_use=tuple(tool_use),
        tool_result=tuple(tool_result),
        timestamp=timestamp,
        qa=tuple(qa),
        thinking="\n".join(thinking_chunks),
        tokens=tokens,
        model=(
            _message_model(message_data)
            if role == "assistant"
            else None
        ),
    )


def _extract_messages_from_db(
    conn: sqlite3.Connection, sid: str
) -> List[Message]:
    """Read all messages for ``sid`` from an open ZCode DB connection."""
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        rows = cursor.execute(_SELECT_MESSAGES_WITH_PARTS, (sid,)).fetchall()
    except sqlite3.Error:
        return []

    messages: List[Message] = []
    current_mid: Optional[str] = None
    current_data: Optional[dict] = None
    current_parts: List[Tuple[dict, Optional[int]]] = []
    current_mtime: Optional[int] = None

    def flush() -> None:
        nonlocal current_mid, current_data, current_parts, current_mtime
        if current_mid is None:
            return
        ts = _epoch_ms_to_datetime(current_mtime)
        # Prefer the message's own recorded time when present.
        if isinstance(current_data, dict):
            mtime_obj = (current_data.get("time") or {}).get("created") \
                if isinstance(current_data.get("time"), dict) else None
            ts = _epoch_ms_to_datetime(mtime_obj) or ts
        msg = _build_message(current_data, current_parts, timestamp=ts)
        if msg is not None:
            messages.append(msg)
        current_mid = None
        current_data = None
        current_parts = []
        current_mtime = None

    for row in rows:
        mid = row["mid"]
        if mid != current_mid:
            flush()
            current_mid = mid
            current_data = _json_or_none(row["mdata"])
            current_parts = []
            current_mtime = row["mtime"]
        part = _json_or_none(row["pdata"])
        if part is not None:
            current_parts.append((part, row["ptime"]))
    flush()
    return messages


# ---------------------------------------------------------------------------
# Rollout model-io JSONL layer (fallback for DB-unknown sessions)
# ---------------------------------------------------------------------------

_ROLLOUT_GLOB = "model-io-sess_*.jsonl"
_SUBAGENT_ID_RE_SUFFIX = "subagent_agent_"


def _subagent_meta_map() -> dict:
    """Map ``childSessionId`` → spawn metadata from the agents directory.

    ``~/.zcode/cli/agents/sess_<parent>/agent_<child>/metadata.json`` is
    the only place a rollout-only subagent's parent link lives (the
    rollout filename carries just the child uuid), and — for every
    subagent, DB-registered or rollout-only — the only place the spawn
    facts live: ``parentSessionId``, ``parentToolUseId`` (the id of the
    ``Agent`` tool call that spawned the child; the join key
    ``extra.spawn_tool_use_id`` consumers correlate on) and ``profileId``
    (the persona the child ran as, e.g. ``general-purpose`` / ``Explore``;
    surfaced as ``extra.subagent_type``).  No ``model`` pin exists in the
    metadata — never fabricated.  Best-effort: unreadable or malformed
    files contribute nothing.
    """
    mapping: dict = {}
    root = _agents_dir()
    if not root.is_dir():
        return mapping
    for meta_path in sorted(
        glob.glob(str(root / "sess_*" / "agent_*" / "metadata.json"))
    ):
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        child = meta.get("childSessionId")
        parent = meta.get("parentSessionId")
        if not (isinstance(child, str) and child and isinstance(parent, str)):
            continue
        entry: dict = {"parent": parent}
        profile = meta.get("profileId")
        if isinstance(profile, str) and profile:
            entry["subagent_type"] = profile
        spawn_id = meta.get("parentToolUseId")
        if isinstance(spawn_id, str) and spawn_id:
            entry["spawn_tool_use_id"] = spawn_id
        mapping[child] = entry
    return mapping


def _rollout_first_user_text(records: List[dict]) -> Optional[str]:
    """The first user-message text in the earliest full snapshot, if any."""
    for record in records:
        req = record.get("request")
        if not isinstance(req, dict):
            continue
        if req.get("messagesKind") not in (None, "full"):
            continue
        for msg in req.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            text = _rollout_message_text(msg)
            candidate = text.strip() if text else ""
            if candidate and not candidate.startswith("<"):
                first_line = candidate.splitlines()[0].strip()
                if first_line:
                    return first_line
        break
    return None


def _rollout_message_text(msg: dict) -> str:
    """Concatenate the plain-text content of one rollout input message."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("text", ""):
            t = block.get("text", "")
            if isinstance(t, str) and t:
                chunks.append(t)
    return "\n".join(chunks)


def _scan_rollout_file(
    path: Path, subagent_meta: dict
) -> Optional[Tuple[str, Session]]:
    """Parse one rollout file into ``(sessionId, Session)``.

    ``subagent_meta`` (from :func:`_subagent_meta_map`) supplies the
    parent link plus the spawn facts (``subagent_type`` /
    ``spawn_tool_use_id``).  Returns ``None`` when the file holds no
    usable record.
    """
    records: List[dict] = []
    session_id: Optional[str] = None
    last_ts: Optional[datetime] = None
    models: List[str] = []
    max_message_count = 0
    query_source: Optional[str] = None

    for record in iter_jsonl_records(path):
        records.append(record)
        sid = record.get("sessionId")
        if isinstance(sid, str) and sid and session_id is None:
            session_id = sid
        ts = _parse_iso_timestamp(record.get("completedAt", ""))
        if ts is not None:
            last_ts = ts
        model_obj = record.get("model")
        if isinstance(model_obj, dict):
            model = model_obj.get("modelId")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
        req = record.get("request")
        if isinstance(req, dict):
            mc = req.get("messageCount")
            if isinstance(mc, int) and not isinstance(mc, bool):
                max_message_count = max(max_message_count, mc)
        qs = record.get("querySource")
        if isinstance(qs, str) and qs and query_source is None:
            query_source = qs

    if session_id is None:
        # Fall back to the filename: model-io-sess_<sessionId>.jsonl
        stem = path.name
        if stem.startswith("model-io-") and stem.endswith(".jsonl"):
            session_id = stem[len("model-io-"):-len(".jsonl")]
    if not session_id:
        return None

    # Parent link: agents metadata first (authoritative), else the
    # subagent naming convention alone (parent uuid stays unknown).
    meta = subagent_meta.get(session_id)
    meta = meta if isinstance(meta, dict) else {}
    parent = meta.get("parent")
    is_subagent = parent is not None or _SUBAGENT_ID_RE_SUFFIX in session_id

    title_text = _rollout_first_user_text(records)
    title = _normalise_title(title_text) if title_text else "Untitled"

    if last_ts is None:
        try:
            last_ts = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return None

    extra: dict = {"source": "rollout", "querySource": query_source}
    for key in ("subagent_type", "spawn_tool_use_id"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            extra[key] = val

    return session_id, Session(
        uuid=session_id,
        agent=AgentName.ZCODE,
        title=title,
        date=last_ts,
        path=str(path),
        # +1: the last record's response is not part of any input snapshot.
        message_count=max_message_count + 1 if max_message_count else 0,
        parent_uuid=parent,
        kind="subagent" if is_subagent else "agent",
        project_dir=None,
        # The rollout record's own ``querySource`` (observed values:
        # ``"main_turn"`` / ``"subagent"``), passed through verbatim —
        # the Codex-originator precedent, no invented taxonomy.  DB
        # sessions carry no equivalent signal → ``None`` there.
        launch_surface=query_source,
        models=tuple(models),
        extra=extra,
    )


def _rollout_sessions(known_uuids: set) -> List[Session]:
    """Rollout-file sessions whose uuid the DB does not already cover."""
    root = _rollout_dir()
    if not root.is_dir():
        return []
    subagent_meta = _subagent_meta_map()
    sessions: List[Session] = []
    for path in sorted(root.glob(_ROLLOUT_GLOB)):
        if not path.is_file():
            continue
        scanned = _scan_rollout_file(path, subagent_meta)
        if scanned is None:
            continue
        sid, session = scanned
        if sid in known_uuids:
            continue
        known_uuids.add(sid)
        sessions.append(session)
    return sessions


def _find_rollout_file(uuid: str) -> Optional[Path]:
    """Locate the rollout file for an exact session id, if it exists."""
    path = _rollout_dir() / f"model-io-{uuid}.jsonl"
    if path.is_file():
        return path
    return None


def _rollout_response_to_message(record: dict) -> Optional[Message]:
    """Map one rollout record's ``response`` to an assistant :class:`Message`."""
    resp = record.get("response")
    if not isinstance(resp, dict):
        return None
    text = resp.get("text")
    text = text if isinstance(text, str) else ""
    reasoning = resp.get("reasoningText")
    thinking = reasoning if isinstance(reasoning, str) else ""
    tool_calls = resp.get("toolCalls") or []
    tool_use: List[dict] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        entry: dict = {
            "name": call.get("name") or "",
            "input": _stringify(call.get("input")),
        }
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            entry["tool_use_id"] = call_id
        tool_use.append(entry)
    if not text and not thinking and not tool_use:
        return None
    model_obj = record.get("model")
    model = None
    if isinstance(model_obj, dict):
        model_id = model_obj.get("modelId")
        model = model_id if isinstance(model_id, str) and model_id else None
    ts = _parse_iso_timestamp(record.get("completedAt", ""))
    return Message(
        role="assistant",
        text=text,
        tool_use=tuple(tool_use),
        timestamp=ts,
        thinking=thinking,
        model=model,
    )


def _rollout_input_message_to_message(msg: dict) -> Optional[Message]:
    """Map one cumulative-snapshot input message to a :class:`Message`."""
    role = msg.get("role")
    if role == "system":
        return None  # system prompts are noise for the audit surface
    if role == "user":
        return Message(
            role="user",
            text=_rollout_message_text(msg),
            timestamp=None,
        )
    if role == "assistant":
        content = msg.get("content")
        text_chunks: List[str] = []
        thinking_chunks: List[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                t = block.get("text", "")
                if btype in ("text", "") and isinstance(t, str) and t:
                    text_chunks.append(t)
                elif btype == "reasoning" and isinstance(t, str) and t:
                    thinking_chunks.append(t)
        tool_use: List[dict] = []
        for call in msg.get("toolCalls") or []:
            if not isinstance(call, dict):
                continue
            entry: dict = {
                "name": call.get("name") or "",
                "input": _stringify(call.get("input")),
            }
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                entry["tool_use_id"] = call_id
            tool_use.append(entry)
        model = msg.get("modelId")
        return Message(
            role="assistant",
            text="\n".join(text_chunks),
            tool_use=tuple(tool_use),
            thinking="\n".join(thinking_chunks),
            model=model if isinstance(model, str) else None,
        )
    if role == "tool":
        content = msg.get("content")
        tr_entry: dict = {
            "content": content if isinstance(content, str) else _stringify(content),
            "is_error": bool(msg.get("isError")),
        }
        call_id = msg.get("toolCallId")
        if isinstance(call_id, str) and call_id:
            tr_entry["tool_use_id"] = call_id
        return Message(
            role="tool",
            text="",
            tool_result=(tr_entry,),
        )
    return None


def _extract_messages_from_rollout(path: Path) -> List[Message]:
    """Replay a rollout model-io JSONL into :class:`Message` objects.

    Each record's ``request.messages`` is that call's input; a ``full``
    snapshot resets the conversation, a ``delta`` appends at
    ``messageOffset`` (a ``tail`` is lossy — used only until the first
    ``full``).  The LAST record's ``response`` (never part of any input
    snapshot) is appended as the final assistant message.
    """
    conversation: List[dict] = []
    last_record: Optional[dict] = None
    for record in iter_jsonl_records(path):
        req = record.get("request")
        if not isinstance(req, dict):
            continue
        last_record = record
        kind = req.get("messagesKind")
        msgs = req.get("messages")
        if not isinstance(msgs, list):
            continue
        if kind == "delta":
            offset = req.get("messageOffset")
            if isinstance(offset, int) and 0 <= offset <= len(conversation):
                # Replace the known tail position onward with the delta.
                conversation[offset:] = list(msgs)
            else:
                conversation.extend(msgs)
        elif kind == "tail":
            if not conversation:
                # No anchor yet — best-effort start of the conversation.
                conversation = list(msgs)
        else:  # "full" (or absent) — reset to the authoritative snapshot
            conversation = list(msgs)

    messages: List[Message] = []
    for msg in conversation:
        if not isinstance(msg, dict):
            continue
        mapped = _rollout_input_message_to_message(msg)
        if mapped is not None:
            messages.append(mapped)
    if last_record is not None:
        response_msg = _rollout_response_to_message(last_record)
        if response_msg is not None:
            messages.append(response_msg)
    return messages


def _rollout_token_usage(path: Path) -> Optional[dict]:
    """Sum the per-call ``response.usage`` blocks of a rollout file."""
    totals: dict = {
        "input": 0,
        "output": 0,
        "reasoning": None,
        "cache_read": 0,
        "cache_write": 0,
    }
    found = False
    for record in iter_jsonl_records(path):
        resp = record.get("response")
        if not isinstance(resp, dict):
            continue
        usage = resp.get("usage")
        if not isinstance(usage, dict):
            continue
        found = True

        def _add(key: str, field: str) -> None:
            val = usage.get(field)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                totals[key] += val

        _add("input", "inputTokens")
        _add("output", "outputTokens")
        _add("cache_read", "cacheReadTokens")
        _add("cache_write", "cacheWriteTokens")
        # ZCode records no per-call reasoning counter — stays None
        # (honest absence) even when calls exist.
    if not found:
        return None
    counted = [
        v for v in (totals["input"], totals["output"],
                    totals["cache_read"], totals["cache_write"])
        if isinstance(v, int)
    ]
    total = sum(counted)
    if total <= 0:
        return None
    return {**totals, "total": total}


# ---------------------------------------------------------------------------
# Public parser interface
# ---------------------------------------------------------------------------


def list_sessions(
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[Session]:
    """Return every ZCode session: DB registry first, rollout files second."""
    sessions: List[Session] = []
    seen: set = set()
    for db_path in _resolve_db_paths(base_dir, override):
        conn = _open_db(db_path)
        if conn is None:
            continue
        try:
            conn.row_factory = sqlite3.Row
            list_cursor = conn.cursor()
            count_cursor = conn.cursor()
            rows = list_cursor.execute(_SELECT_ALL_SESSIONS).fetchall()
            subagent_meta = _subagent_meta_map()
            for row in rows:
                sid = row["id"]
                if not isinstance(sid, str) or sid in seen:
                    continue
                seen.add(sid)
                count = count_cursor.execute(
                    _SELECT_MESSAGE_COUNT, (sid,)
                ).fetchone()[0]
                session = _row_to_session(row, db_path, subagent_meta)
                sessions.append(
                    dataclasses.replace(
                        session,
                        message_count=int(count or 0),
                        models=_session_models(count_cursor, sid),
                    )
                )
        except sqlite3.Error:
            continue
        finally:
            conn.close()

    sessions.extend(_rollout_sessions(seen))
    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _read_session_by_uuid(
    uuid: str,
    base_dir: Optional[str],
    override: Optional[str],
) -> Session:
    if not _is_valid_uuid(uuid):
        raise ValueError(f"Invalid ZCode session uuid: {uuid!r}")

    for db_path in _resolve_db_paths(base_dir, override):
        conn = _open_db(db_path)
        if conn is None:
            continue
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            row = cursor.execute(_SELECT_SESSION, (uuid,)).fetchone()
            if row is None:
                continue
            count = cursor.execute(
                _SELECT_MESSAGE_COUNT, (uuid,)
            ).fetchone()[0]
            session = _row_to_session(row, db_path, _subagent_meta_map())
            return dataclasses.replace(
                session,
                message_count=int(count or 0),
                models=_session_models(cursor, uuid),
            )
        except sqlite3.Error:
            continue
        finally:
            conn.close()

    rollout_path = _find_rollout_file(uuid)
    if rollout_path is not None:
        scanned = _scan_rollout_file(rollout_path, _subagent_meta_map())
        if scanned is not None and scanned[0] == uuid:
            return scanned[1]

    raise FileNotFoundError(f"ZCode session {uuid!r} not found")


def read_session(
    uuid: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> Session:
    """Read a single ZCode session by ``uuid``.

    Raises:
        FileNotFoundError: no DB or rollout file knows this id.
        ValueError: ``uuid`` is malformed.
    """
    return _read_session_by_uuid(uuid, base_dir, override)


def read_messages(
    uuid: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[Message]:
    """Return the full message list for a ZCode session.

    DB sessions read the ``message``/``part`` tables; DB-unknown sessions
    fall back to replaying the rollout model-io JSONL.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir, override)
    if session.extra.get("source") == "rollout":
        return fold_orphan_thinking(
            _extract_messages_from_rollout(Path(session.path))
        )
    conn = _open_db(session.path)
    if conn is None:
        return []
    try:
        return fold_orphan_thinking(
            _extract_messages_from_db(conn, session.uuid)
        )
    finally:
        conn.close()


def read_token_usage(
    uuid: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> Optional[dict]:
    """Return the session's recorded token usage, or ``None`` without signal.

    DB sessions sum the per-assistant-message ``tokens`` blocks (each is
    one model call's usage — the same cross-agent semantics OpenCode
    uses).  Rollout sessions sum the per-call ``response.usage`` blocks.
    A counter the format does not record is ``None`` (rollout reasoning).

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir, override)
    if session.extra.get("source") == "rollout":
        return _rollout_token_usage(Path(session.path))

    conn = _open_db(session.path)
    if conn is None:
        return None
    try:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT data FROM message WHERE session_id = ?", (session.uuid,)
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    totals = {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    found = False
    for row in rows:
        data = _json_or_none(row[0])
        if not isinstance(data, dict):
            continue
        block = _normalize_tokens(data.get("tokens"))
        if block is None:
            continue
        found = True
        for field in totals:
            val = block.get(field)
            if val is not None:
                totals[field] += val
    if not found:
        return None
    total = totals["input"] + totals["output"]
    if total <= 0:
        return None
    return {**totals, "total": total}


def search(
    query: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> List[Session]:
    """Case-insensitive substring search across ZCode session titles."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [
        session
        for session in list_sessions(base_dir, override)
        if needle in session.title.lower()
    ]


def session_exists(
    uuid: str,
    base_dir: Optional[str] = None,
    override: Optional[str] = None,
) -> bool:
    if not _is_valid_uuid(uuid):
        return False
    try:
        _read_session_by_uuid(uuid, base_dir, override)
    except (FileNotFoundError, ValueError):
        return False
    return True
