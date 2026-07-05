"""Codex session parser.

Source layout (recursive)::

    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
    ~/.codex/archived_sessions/YYYY/MM/DD/rollout-*.jsonl

Each line is a JSON object with one of the following ``type`` values:

* ``"session_meta"``   — payload has the session ``id``, ``cwd``
  (surfaced as ``Session.project_dir``) and ``originator`` (the launch
  surface, e.g. ``"codex_vscode"`` / ``"Codex Desktop"``, surfaced
  verbatim as ``Session.launch_surface``); this is the canonical UUID.
* ``"response_item"``  — payload is a message (``type: "message"``,
  ``role: "user"``/``"assistant"``, ``content: [...]``).
* ``"event_msg"``      — extracted when ``payload.type == "user_message"``
  (the raw user prompt text lives here, not in response_item). System-noise
  prefixes (``<permissions``, ``<system-reminder>``, ``<command-message>``,
  ``## Apps``) are filtered out before projection.
* ``"custom_tool_call"`` and friends — non-message noise, skipped.

User-text dedup across ``response_item`` and ``event_msg`` uses the first
``$AI_R_DEDUP_KEY_LEN`` chars (default 256) as the seen-set key.
Bump the env var if your prompts collide in the first 64 chars but
diverge later.

The first ``session_meta`` record is authoritative; later ones (rare)
are ignored.  The first user message text becomes the title, with a
fallback to ``payload.cwd`` if no user message is found.

The base directory can be overridden by ``base_dir`` or by setting
``$AI_R_HOME/.codex/sessions`` (the sibling ``archived_sessions``
directory is scanned automatically).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from ._common import (
    _is_valid_uuid,
    _parse_iso_timestamp,
    _qa_from_codex,
    iter_jsonl_records,
)
from .models import AgentName, Message, Session


_TITLE_MAX_LEN = 100
_DEDUP_KEY_LEN_DEFAULT = 256


def get_dedup_key_len() -> int:
    """Re-read ``$AI_R_DEDUP_KEY_LEN`` on every call.

    Cheap (single ``os.environ`` dict lookup); the alternative — module-level
    capture at import time — silently ignores runtime changes (e.g. operator
    restarts a long-running service after exporting a new value, or a test
    that mutates the env post-import). Returns the default if unset, empty,
    non-integer, or non-positive.
    """
    raw = os.environ.get("AI_R_DEDUP_KEY_LEN", str(_DEDUP_KEY_LEN_DEFAULT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEDUP_KEY_LEN_DEFAULT
    if value <= 0:
        return _DEDUP_KEY_LEN_DEFAULT
    return value


def _dedup_key(text: str) -> str:
    """Stable dedup key for user-text seen-set. Length controlled by
    ``$AI_R_DEDUP_KEY_LEN`` (default 256). Longer = stricter dedup,
    cost = more memory per session. 256 covers the first ~4 paragraphs
    of any realistic user prompt, which is the practical collision zone
    when the same prompt appears in both ``response_item`` and
    ``event_msg.user_message``."""
    return text[:get_dedup_key_len()]


def _resolve_base_dir(base_dir: Optional[str]) -> List[Path]:
    """Resolve Codex session roots: ``sessions/`` and the sibling ``archived_sessions/``."""
    if base_dir:
        primary = Path(base_dir).expanduser()
    else:
        env_home = os.environ.get("AI_R_HOME")
        if env_home:
            primary = Path(env_home).expanduser() / ".codex" / "sessions"
        else:
            primary = Path("~/.codex/sessions").expanduser()
    return [primary, primary.parent / "archived_sessions"]


def source_roots(base_dir: Optional[str] = None) -> List[str]:
    """Candidate source root(s) for Codex sessions (may not exist).

    Used by :mod:`ai_r.diagnostics` to explain empty results.
    """
    return [str(p) for p in _resolve_base_dir(base_dir)]


def _extract_text_from_parts(parts: object) -> str:
    """Concatenate ``input_text``/``output_text``/``text`` parts."""
    if not isinstance(parts, list):
        return ""
    chunks: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if part_type in ("input_text", "output_text", "text", ""):
            text = part.get("text", "")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks)


def _is_system_noise(text: str) -> bool:
    stripped = text.lstrip()
    return (
        stripped.startswith("<permissions")
        or stripped.startswith("## Apps")
        or stripped.startswith("<command-message>")
        or stripped.startswith("<system-reminder>")
    )


def _scan_file(jsonl_path: Path) -> Optional[Session]:
    """Parse a Codex rollout file into a :class:`Session`."""
    uuid: Optional[str] = None
    cwd: Optional[str] = None
    originator: Optional[str] = None
    timestamp: Optional[datetime] = None
    title: Optional[str] = None
    parent_uuid: Optional[str] = None
    is_subagent = False
    message_count = 0

    for record in iter_jsonl_records(jsonl_path):
        line_ts = _parse_iso_timestamp(record.get("timestamp", ""))
        if line_ts is not None:
            timestamp = line_ts

        rec_type = record.get("type")
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if rec_type == "session_meta" and uuid is None:
            uuid = payload.get("id") if isinstance(payload.get("id"), str) else None
            if not uuid:
                # session_meta without a usable id is unusable
                return None
            cwd_val = payload.get("cwd")
            if isinstance(cwd_val, str):
                cwd = cwd_val
            # ``originator`` names the surface that spawned the session
            # (observed values: "codex_vscode", "Codex Desktop", CLI
            # builds).  Passed through verbatim as ``launch_surface`` —
            # no invented taxonomy on top of the raw signal.
            originator_val = payload.get("originator")
            if isinstance(originator_val, str) and originator_val.strip():
                originator = originator_val.strip()
            meta_ts = _parse_iso_timestamp(payload.get("timestamp", ""))
            if meta_ts is not None:
                timestamp = meta_ts
            # Subagent signal: ``thread_source ∈ {"user", "subagent"}`` plus a
            # flat ``parent_thread_id``; older/newer layouts may carry the
            # parent only in the nested ``source.subagent.thread_spawn`` blob,
            # so read both (flat wins when present).
            if payload.get("thread_source") == "subagent":
                is_subagent = True
            parent_val = payload.get("parent_thread_id")
            if not isinstance(parent_val, str) or not parent_val:
                source = payload.get("source")
                spawn = (
                    source.get("subagent", {}).get("thread_spawn", {})
                    if isinstance(source, dict)
                    and isinstance(source.get("subagent"), dict)
                    else {}
                )
                parent_val = (
                    spawn.get("parent_thread_id")
                    if isinstance(spawn, dict) else None
                )
            if isinstance(parent_val, str) and parent_val:
                parent_uuid = parent_val
                is_subagent = True
            continue

        if (
            rec_type == "response_item"
            and payload.get("type") == "message"
        ):
            role = payload.get("role", "")
            text = _extract_text_from_parts(payload.get("content", []))
            if not text or _is_system_noise(text):
                continue
            message_count += 1
            if title is None and role == "user":
                candidate = text.strip()
                if candidate and not candidate.startswith("<") \
                        and not candidate.startswith("#"):
                    first_line = candidate.splitlines()[0].strip()
                    if first_line:
                        title = first_line

    if uuid is None:
        return None

    if title:
        final_title = title.replace("\n", " ").replace("\r", " ").strip()[
            :_TITLE_MAX_LEN
        ]
    elif cwd:
        final_title = cwd[:_TITLE_MAX_LEN]
    else:
        final_title = "Untitled"

    if timestamp is None:
        try:
            timestamp = datetime.fromtimestamp(
                jsonl_path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return None

    return Session(
        uuid=uuid,
        agent=AgentName.CODEX,
        title=final_title,
        date=timestamp,
        path=str(jsonl_path),
        message_count=message_count,
        parent_uuid=parent_uuid,
        kind="subagent" if is_subagent else "agent",
        project_dir=cwd,
        launch_surface=originator,
        extra={"cwd": cwd} if cwd else {},
    )


def _discover_files(roots: List[Path]) -> List[Path]:
    """Return Codex rollout files under any of ``roots``, deduped and sorted by mtime desc."""
    seen: set[Path] = set()
    files: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.glob("**/rollout-*.jsonl"):
            if not p.is_file() or p in seen:
                continue
            seen.add(p)
            files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def list_sessions(base_dir: Optional[str] = None) -> List[Session]:
    """Return every Codex session visible under ``base_dir``."""
    roots = _resolve_base_dir(base_dir)
    sessions: List[Session] = []
    seen_uuids: set[str] = set()
    for path in _discover_files(roots):
        session = _scan_file(path)
        if session is None:
            continue
        if session.uuid in seen_uuids:
            # Two files claiming the same session id — keep the first
            # (newest mtime) and ignore the rest.
            continue
        seen_uuids.add(session.uuid)
        sessions.append(session)
    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _find_session_file(
    uuid: str, base_dir: Optional[str]
) -> Tuple[Path, Session]:
    if not _is_valid_uuid(uuid):
        raise ValueError(f"Invalid Codex session uuid: {uuid!r}")
    roots = _resolve_base_dir(base_dir)
    for path in _discover_files(roots):
        for record in iter_jsonl_records(path):
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if payload.get("id") == uuid:
                return path, _scan_file(path)  # type: ignore[return-value]
    raise FileNotFoundError(f"Codex session {uuid!r} not found under {roots}")


def read_session(uuid: str, base_dir: Optional[str] = None) -> Session:
    """Read a single Codex session by ``uuid``.

    Raises:
        FileNotFoundError: no file with this id exists.
        ValueError: ``uuid`` is malformed.
    """
    _, session = _find_session_file(uuid, base_dir)
    return session



def _codex_message_text(payload: dict) -> str:
    """Concatenate the text parts of a Codex message payload."""
    return _extract_text_from_parts(payload.get("content", []))


def _safe_json(blob: object) -> object:
    """Best-effort ``json.loads`` returning ``None`` on any failure."""
    if not isinstance(blob, str) or not blob.strip():
        return None
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _extract_messages_from_rollout(path: Path) -> List[Message]:
    """Read a Codex rollout JSONL into structured :class:`Message` objects.

    Codex rollouts store ``response_item`` records.  ``message`` payloads
    become user/assistant :class:`Message` objects.  ``function_call``
    payloads (and the ``local_shell_call`` family) become assistant
    ``tool_use`` entries; ``function_call_output`` payloads become
    ``tool`` messages with a ``tool_result`` entry.  ``reasoning``
    payloads with a non-empty plaintext ``summary`` become assistant
    messages carrying :attr:`Message.thinking`.  Codex carries no
    per-result error flag, so ``tool_result.is_error`` defaults ``False``
    (best-effort).  Other record types are skipped.

    Lines that are not valid JSON are silently skipped; an
    :class:`OSError` returns whatever was collected so far.
    """
    messages: List[Message] = []
    seen_user_texts: set[str] = set()
    # Pending request_user_input calls: call_id -> structured questions.
    # The chosen answers arrive later in the matching function_call_output,
    # so we buffer the questions and pair them by call_id when the output
    # lands (the answer text is keyed by question id, not positional).
    pending_questions: dict[str, list] = {}
    for record in iter_jsonl_records(path):
        rec_type = record.get("type")
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if rec_type == "response_item":
            ptype = payload.get("type")
            env_ts = _parse_iso_timestamp(record.get("timestamp", ""))
            if ptype == "message":
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _codex_message_text(payload)
                if role == "user" and text:
                    key = _dedup_key(text)
                    if key in seen_user_texts:
                        continue
                    seen_user_texts.add(key)
                messages.append(Message(role=role, text=text, timestamp=env_ts))
            elif ptype in ("function_call", "local_shell_call"):
                name = payload.get("name") or ptype
                arguments = payload.get("arguments", "")
                if isinstance(arguments, str):
                    input_str = arguments
                else:
                    try:
                        input_str = json.dumps(arguments, ensure_ascii=False)
                    except (TypeError, ValueError):
                        input_str = str(arguments)
                # request_user_input is Codex's interactive-question
                # tool: buffer its questions so the later output can
                # be paired into a question->answer ``qa``.
                if name == "request_user_input":
                    call_id = payload.get("call_id")
                    parsed_args = (
                        arguments
                        if isinstance(arguments, dict)
                        else _safe_json(input_str)
                    )
                    if isinstance(call_id, str) and isinstance(parsed_args, dict):
                        questions = parsed_args.get("questions")
                        if isinstance(questions, list):
                            pending_questions[call_id] = questions
                messages.append(
                    Message(
                        role="assistant",
                        text="",
                        tool_use=({"name": name, "input": input_str},),
                        timestamp=env_ts,
                    )
                )
            elif ptype == "reasoning":
                # Codex stores model reasoning as ``reasoning`` response
                # items: the plaintext lives in ``summary[].text`` while
                # ``encrypted_content`` is opaque ciphertext (no plaintext
                # → skipped, absence is honest).  Verified against real
                # ~/.codex/sessions rollouts (2026-07 snapshot): ``summary``
                # is the only plaintext carrier and is often empty ([]),
                # so summary-less items are skipped rather than emitted as
                # blank assistant messages.  No per-message usage exists in
                # the format (cumulative ``token_count`` only) → tokens
                # stays None.
                summary = payload.get("summary")
                thinking_chunks: List[str] = []
                if isinstance(summary, list):
                    for entry in summary:
                        if not isinstance(entry, dict):
                            continue
                        text = entry.get("text", "")
                        if isinstance(text, str) and text:
                            thinking_chunks.append(text)
                if thinking_chunks:
                    messages.append(
                        Message(
                            role="assistant",
                            text="",
                            thinking="\n".join(thinking_chunks),
                            timestamp=env_ts,
                        )
                    )
            elif ptype == "web_search_call":
                # Codex's native web access is not a function_call: the
                # rollout stores a ``web_search_call`` response item whose
                # ``action`` object carries the target (``search`` →
                # ``query``/``queries``, ``open_page``/``find_in_page`` →
                # ``url``).  Surface it as a ``web_search`` tool_use so the
                # F3.1 classifier marks it ``tool_kind="web"`` and the F4.3
                # network audit sees Codex egress like everyone else's.
                # No result record exists → no tool_result (is_error stays
                # unknown — honest).
                action = payload.get("action")
                if isinstance(action, dict):
                    try:
                        input_str = json.dumps(action, ensure_ascii=False)
                    except (TypeError, ValueError):  # pragma: no cover
                        input_str = str(action)
                else:
                    input_str = ""
                messages.append(
                    Message(
                        role="assistant",
                        text="",
                        tool_use=({"name": "web_search", "input": input_str},),
                        timestamp=env_ts,
                    )
                )
            elif ptype in ("function_call_output", "local_shell_call_output"):
                output = payload.get("output", "")
                if not isinstance(output, str):
                    try:
                        output = json.dumps(output, ensure_ascii=False)
                    except (TypeError, ValueError):
                        output = str(output)
                call_id = payload.get("call_id")
                qa: tuple = ()
                if isinstance(call_id, str) and call_id in pending_questions:
                    questions = pending_questions.pop(call_id)
                    answers_obj = _safe_json(output)
                    qa = tuple(_qa_from_codex(questions, answers_obj))
                messages.append(
                    Message(
                        role="tool",
                        text="",
                        # Codex ``function_call_output`` records carry no
                        # explicit error flag (the output is a plain string),
                        # so ``is_error`` is best-effort and defaults False.
                        tool_result=({"content": output, "is_error": False},),
                        timestamp=env_ts,
                        qa=qa,
                    )
                )
        # verified against Codex CLI 2026-05 snapshot; recheck on schema change
        elif rec_type == "event_msg" and payload.get("type") == "user_message":
            msg = payload.get("message")
            if not isinstance(msg, str) or len(msg) <= 10:
                continue
            if _is_system_noise(msg):
                continue
            key = _dedup_key(msg)
            if key in seen_user_texts:
                continue
            seen_user_texts.add(key)
            env_ts = _parse_iso_timestamp(record.get("timestamp", ""))
            messages.append(Message(role="user", text=msg, timestamp=env_ts))
    return messages


def read_messages(
    uuid: str, base_dir: Optional[str] = None
) -> List[Message]:
    """Return the full message list for a Codex session.

    Reuses :func:`read_session` for path resolution.  Function/shell
    calls and their outputs are preserved on the returned
    :class:`Message` objects.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir)
    return _extract_messages_from_rollout(Path(session.path))


def read_token_usage(
    uuid: str, base_dir: Optional[str] = None
) -> Optional[dict]:
    """Return the session's recorded token usage, or ``None`` without signal.

    Codex rollouts interleave ``event_msg`` records whose payload
    ``type == "token_count"`` carries ``info.total_token_usage`` — a
    **cumulative** counter for the whole session, so the LAST valid one
    wins (no summing).

    Normalized fields (format-native semantics): ``input`` =
    ``input_tokens`` (which Codex counts **including** the cached part),
    ``output`` = ``output_tokens`` (including reasoning), ``reasoning`` =
    ``reasoning_output_tokens``, ``cache_read`` = ``cached_input_tokens``;
    Codex has no cache-creation counter → ``cache_write`` is ``None``.
    ``total`` is the recorded ``total_tokens`` (fallback: input + output).
    Returns ``None`` when the rollout has no ``token_count`` event or the
    total is zero — absence is honest.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    path, _ = _find_session_file(uuid, base_dir)
    last_usage: Optional[dict] = None
    for record in iter_jsonl_records(path):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        usage = info.get("total_token_usage")
        if isinstance(usage, dict):
            last_usage = usage
    if last_usage is None:
        return None

    def _count(key: str) -> Optional[int]:
        val = last_usage.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
        return None

    input_tokens = _count("input_tokens")
    output_tokens = _count("output_tokens")
    total = _count("total_tokens")
    if total is None:
        total = (input_tokens or 0) + (output_tokens or 0)
    if total <= 0:
        return None
    return {
        "input": input_tokens,
        "output": output_tokens,
        "reasoning": _count("reasoning_output_tokens"),
        "cache_read": _count("cached_input_tokens"),
        "cache_write": None,
        "total": total,
    }


def search(query: str, base_dir: Optional[str] = None) -> List[Session]:
    """Case-insensitive substring search across Codex session titles."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [
        session
        for session in list_sessions(base_dir)
        if needle in session.title.lower()
    ]


def session_exists(uuid: str, base_dir: Optional[str] = None) -> bool:
    if not _is_valid_uuid(uuid):
        return False
    try:
        _find_session_file(uuid, base_dir)
    except (FileNotFoundError, ValueError):
        return False
    return True
