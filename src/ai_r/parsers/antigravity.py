"""Antigravity session parser.

Antigravity stores its "brain" (per-session scratchpad) and companion
binary metadata under four roots beneath ``~/.gemini/`` (or
``$AI_R_HOME/.gemini/``)::

    ~/.gemini/antigravity-cli/brain/<session-uuid>/...
    ~/.gemini/antigravity/brain/<session-uuid>/...
    ~/.gemini/antigravity-ide/brain/<session-uuid>/...
    ~/.gemini/antigravity-backup/brain/<session-uuid>/...

Each root may also carry companion stores alongside ``brain/``:

* ``conversations/<session-uuid>.db`` — SQLite database whose
  ``trajectory_metadata_blob`` row (``id='main'``) stores a protobuf
  message with workspace URI (``project_dir``), ``created_at``,
  ``parent_uuid``, ``root_uuid``, subagent descriptor (type/role/prompt),
  and ``spawn_depth``.
* ``conversations/<session-uuid>.pb`` — legacy raw protobuf fallback.
* ``conversation_summaries.db`` — per-root SQLite index fallback.

Within ``brain/<session-uuid>/``, transcripts are resolved in priority
order:

1. ``.system_generated/logs/transcript_full.jsonl`` (canonical full log)
2. ``.system_generated/logs/transcript.json``
3. ``.system_generated/logs/transcript.jsonl`` (UI-truncated copy)
4. ``.system_generated/logs/overview.txt`` (legacy meta-event log)

Companion markdown plans (``implementation_plan.md``, ``walkthrough.md``,
``task.md.resolved``, ``task.md``) and parent-brain subagent descriptors
(``<parent_brain>/.system_generated/subagents/<child_uuid>.json``) serve
as fallbacks when primary sources are absent.

The base directory can be overridden by ``base_dir`` (which then names a
single brain directory) or by setting ``$AI_R_HOME``.  All SQLite reads
are strictly read-only (``?mode=ro``) and every untrusted binary/JSONL
input degrades fail-soft.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import urllib.parse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ai_r.user_refs import make_user_ref

from ._common import (
    _is_valid_uuid,
    _normalise_title,
    _parse_iso_timestamp,
    iter_jsonl_records,
)
from .models import AgentName, Message, Session


_TITLE_MAX_LEN = 100
_USER_REQUEST_RE = re.compile(
    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL
)
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FRAC_SECS_RE = re.compile(r"(\.\d{6})\d+")
_MARKDOWN_CANDIDATES: tuple[str, ...] = (
    "implementation_plan.md",
    "walkthrough.md",
    "task.md.resolved",
    "task.md",
)
_TRANSCRIPT_CANDIDATES: tuple[str, ...] = (
    ".system_generated/logs/transcript_full.jsonl",
    ".system_generated/logs/transcript.json",
    ".system_generated/logs/transcript.jsonl",
    ".system_generated/logs/overview.txt",
)
_OVERVIEW = ".system_generated/logs/overview.txt"

_ROOT_NAMES: tuple[str, ...] = (
    "antigravity-cli",
    "antigravity",
    "antigravity-ide",
    "antigravity-backup",
)

_TOOL_PART_TYPES: frozenset[str] = frozenset({
    "MODEL_TOOL_CALL",
    "TOOL_CALL",
    "tool_call",
    "toolCall",
    "tool_use",
    "TOOL_USE",
})

_TOOL_RESULT_TYPES: frozenset[str] = frozenset({
    "GENERIC",
    "VIEW_FILE",
    "RUN_COMMAND",
    "GREP_SEARCH",
    "CODE_ACTION",
    "LIST_DIRECTORY",
    "SEARCH_WEB",
    "INVOKE_SUBAGENT",
})

# Safety caps for untrusted protobuf blobs and subagent JSON metadata.
_PB_MAX_BLOB_BYTES = 4 * 1024 * 1024
_PB_MAX_FIELDS = 2000
_SUBAGENT_JSON_MAX_BYTES = 1 * 1024 * 1024


@dataclass
class _BrainMeta:
    """Merged binary/index metadata for one Antigravity session."""

    project_dir: Optional[str] = None
    created_at: Optional[datetime] = None
    parent_uuid: Optional[str] = None
    root_uuid: Optional[str] = None
    subagent_type: Optional[str] = None
    title: Optional[str] = None
    spawn_depth: Optional[int] = None
    is_subagent: bool = False


def _merge_meta(base: _BrainMeta, fallback: _BrainMeta) -> _BrainMeta:
    """Fill missing fields of ``base`` from ``fallback`` without overwriting."""
    return _BrainMeta(
        project_dir=base.project_dir or fallback.project_dir,
        created_at=base.created_at or fallback.created_at,
        parent_uuid=base.parent_uuid or fallback.parent_uuid,
        root_uuid=base.root_uuid or fallback.root_uuid,
        subagent_type=base.subagent_type or fallback.subagent_type,
        title=base.title or fallback.title,
        spawn_depth=(
            base.spawn_depth
            if base.spawn_depth is not None
            else fallback.spawn_depth
        ),
        is_subagent=base.is_subagent or fallback.is_subagent,
    )


# ---------------------------------------------------------------------------
# Compact fail-soft stdlib protobuf wire decoder
# ---------------------------------------------------------------------------


def _pb_read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Decode one protobuf base-128 varint starting at ``pos``."""
    shift = 0
    val = 0
    length = len(buf)
    while True:
        if pos >= length:
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return val, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _pb_parse_fields(
    buf: bytes,
    *,
    max_fields: int = _PB_MAX_FIELDS,
) -> Optional[Dict[int, List[Tuple[int, Any]]]]:
    """Parse one protobuf message level into ``{field_num: [(wt, val), ...]}``.

    Returns ``None`` on any wire-format violation, truncation, or field-count
    overflow so corrupt/encrypted blobs degrade fail-soft.
    """
    if not isinstance(buf, (bytes, bytearray)) or len(buf) > _PB_MAX_BLOB_BYTES:
        return None
    fields: Dict[int, List[Tuple[int, Any]]] = {}
    pos = 0
    end = len(buf)
    count = 0
    while pos < end:
        count += 1
        if count > max_fields:
            return None
        try:
            tag, pos = _pb_read_varint(buf, pos)
        except ValueError:
            return None
        field_num = tag >> 3
        wire_type = tag & 7
        if field_num == 0:
            return None
        if wire_type == 0:
            try:
                val, pos = _pb_read_varint(buf, pos)
            except ValueError:
                return None
            fields.setdefault(field_num, []).append((0, val))
        elif wire_type == 1:
            if pos + 8 > end:
                return None
            fields.setdefault(field_num, []).append((1, bytes(buf[pos : pos + 8])))
            pos += 8
        elif wire_type == 2:
            try:
                chunk_len, pos = _pb_read_varint(buf, pos)
            except ValueError:
                return None
            if chunk_len < 0 or pos + chunk_len > end:
                return None
            fields.setdefault(field_num, []).append(
                (2, bytes(buf[pos : pos + chunk_len]))
            )
            pos += chunk_len
        elif wire_type == 5:
            if pos + 4 > end:
                return None
            fields.setdefault(field_num, []).append((5, bytes(buf[pos : pos + 4])))
            pos += 4
        else:
            # Wire types 3/4 (deprecated groups) or 6/7 (invalid) -> reject.
            return None
    return fields


def _pb_bytes_to_str(raw: bytes) -> Optional[str]:
    """Decode UTF-8 string bytes, rejecting NUL bytes and blank strings."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text or not text.strip():
        return None
    return text


def _file_uri_to_path(uri: Any) -> Optional[str]:
    """Convert a ``file:///...`` URI (or absolute path) to a decoded path."""
    if not isinstance(uri, str):
        return None
    s = uri.strip()
    if not s:
        return None
    if s.startswith("file://"):
        parsed = urllib.parse.urlparse(s)
        path = urllib.parse.unquote(parsed.path)
        return path or None
    if s.startswith("/"):
        return s
    return None


def _pb_timestamp_to_datetime(sec: Any, nanos: Any = 0) -> Optional[datetime]:
    """Convert protobuf Timestamp ``(seconds, nanos)`` to UTC ``datetime``."""
    if isinstance(sec, bool) or not isinstance(sec, int) or sec <= 0:
        return None
    if isinstance(nanos, bool) or not isinstance(nanos, int):
        nanos = 0
    if nanos < 0 or nanos >= 1_000_000_000:
        return None
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.replace(microsecond=nanos // 1000)
    except (OSError, OverflowError, ValueError):
        return None


def _decode_trajectory_meta_blob(blob: bytes) -> Optional[_BrainMeta]:
    """Decode ``trajectory_metadata_blob`` (``id='main'``) or ``.pb`` bytes."""
    fields = _pb_parse_fields(blob)
    if not fields:
        return None

    project_dir: Optional[str] = None
    # f1 (workspace/repo message) -> f1.1 (project_dir URI)
    for wt, val in fields.get(1, ()):
        if wt != 2:
            continue
        sub = _pb_parse_fields(val)
        if not sub:
            continue
        for swt, sval in sub.get(1, ()):
            if swt == 2:
                uri = _pb_bytes_to_str(sval)
                path = _file_uri_to_path(uri)
                if path:
                    project_dir = path
                    break
        if project_dir:
            break

    # f7 (repeated project_dir URI fallback)
    if not project_dir:
        for wt, val in fields.get(7, ()):
            if wt == 2:
                uri = _pb_bytes_to_str(val)
                path = _file_uri_to_path(uri)
                if path:
                    project_dir = path
                    break

    # f2 (Timestamp message: f2.1 = seconds, f2.2 = nanos)
    created_at: Optional[datetime] = None
    for wt, val in fields.get(2, ()):
        if wt != 2:
            continue
        sub = _pb_parse_fields(val)
        if not sub:
            continue
        sec_vals = [v for swt, v in sub.get(1, ()) if swt == 0]
        nano_vals = [v for swt, v in sub.get(2, ()) if swt == 0]
        if sec_vals:
            created_at = _pb_timestamp_to_datetime(
                sec_vals[0], nano_vals[0] if nano_vals else 0
            )
            if created_at is not None:
                break

    # f5 = parent_uuid, f6 = root_uuid
    parent_uuid: Optional[str] = None
    for wt, val in fields.get(5, ()):
        if wt == 2:
            s = _pb_bytes_to_str(val)
            if s:
                parent_uuid = s.strip()
                break

    root_uuid: Optional[str] = None
    for wt, val in fields.get(6, ()):
        if wt == 2:
            s = _pb_bytes_to_str(val)
            if s:
                root_uuid = s.strip()
                break

    # f8 (subagent descriptor: f8.1 = type, f8.2 = role/title, f8.3 = prompt)
    subagent_type: Optional[str] = None
    subagent_role: Optional[str] = None
    subagent_prompt: Optional[str] = None
    for wt, val in fields.get(8, ()):
        if wt != 2:
            continue
        sub = _pb_parse_fields(val)
        if not sub:
            continue
        for swt, sval in sub.get(1, ()):
            if swt == 2 and not subagent_type:
                s = _pb_bytes_to_str(sval)
                if s:
                    subagent_type = s.strip()
        for swt, sval in sub.get(2, ()):
            if swt == 2 and not subagent_role:
                s = _pb_bytes_to_str(sval)
                if s:
                    subagent_role = s
        for swt, sval in sub.get(3, ()):
            if swt == 2 and not subagent_prompt:
                s = _pb_bytes_to_str(sval)
                if s:
                    subagent_prompt = s

    # f17 = spawn_depth (varint)
    spawn_depth: Optional[int] = None
    for wt, val in fields.get(17, ()):
        if wt == 0 and isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            spawn_depth = val
            break

    return _BrainMeta(
        project_dir=project_dir,
        created_at=created_at,
        parent_uuid=parent_uuid,
        root_uuid=root_uuid,
        subagent_type=subagent_type,
        title=subagent_role or subagent_prompt,
        spawn_depth=spawn_depth,
        is_subagent=bool(parent_uuid),
    )


# ---------------------------------------------------------------------------
# Read-only SQLite + companion metadata fallbacks
# ---------------------------------------------------------------------------


def _connect_sqlite_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """Open ``db_path`` strictly read-only, failing soft on any error."""
    if not db_path.is_file():
        return None
    try:
        uri = f"file:{urllib.parse.quote(str(db_path), safe='/')}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except (sqlite3.Error, OSError):
        return None


def _parse_ag_timestamp(raw: Any) -> Optional[datetime]:
    """Parse ISO-8601 / space-separated timestamps, trimming nanosecond tails."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = _FRAC_SECS_RE.sub(r"\1", raw.strip())
    return _parse_iso_timestamp(cleaned)


def _read_conv_db_meta(db_path: Path) -> Optional[_BrainMeta]:
    """Read ``trajectory_metadata_blob`` (``id='main'``) from ``<uuid>.db``."""
    conn = _connect_sqlite_ro(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id = 'main' LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not isinstance(row[0], (bytes, bytearray)):
        return None
    return _decode_trajectory_meta_blob(bytes(row[0]))


def _read_conv_pb_meta(pb_path: Path) -> Optional[_BrainMeta]:
    """Read raw protobuf fallback from ``conversations/<uuid>.pb``."""
    if not pb_path.is_file():
        return None
    try:
        size = pb_path.stat().st_size
        if size <= 0 or size > _PB_MAX_BLOB_BYTES:
            return None
        blob = pb_path.read_bytes()
    except OSError:
        return None
    return _decode_trajectory_meta_blob(blob)


def _read_summaries_db_meta(sdb_path: Path, uuid: str) -> Optional[_BrainMeta]:
    """Read fallback metadata from ``<root>/conversation_summaries.db``."""
    conn = _connect_sqlite_ro(sdb_path)
    if conn is None:
        return None
    try:
        col_rows = conn.execute(
            "PRAGMA table_info(conversation_summaries)"
        ).fetchall()
        available = {r[1] for r in col_rows if len(r) > 1 and isinstance(r[1], str)}
        wanted = (
            "title",
            "last_modified_time",
            "workspace_uris",
            "agent_name",
            "parent_conversation_id",
            "nesting_depth",
        )
        selected = [c for c in wanted if c in available]
        row_map: Dict[str, Any] = {}
        if selected and "conversation_id" in available:
            sql = (
                f"SELECT {', '.join(selected)} FROM conversation_summaries "
                "WHERE conversation_id = ? LIMIT 1"
            )
            row = conn.execute(sql, (uuid,)).fetchone()
            if row is not None:
                row_map = dict(zip(selected, row))

        parent_from_table: Optional[str] = None
        try:
            prow = conn.execute(
                "SELECT parent_id FROM conversation_parents "
                "WHERE conversation_id = ? LIMIT 1",
                (uuid,),
            ).fetchone()
            if prow and isinstance(prow[0], str) and prow[0].strip():
                parent_from_table = prow[0].strip()
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    if not row_map and not parent_from_table:
        return None

    project_dir: Optional[str] = None
    raw_uris = row_map.get("workspace_uris")
    if isinstance(raw_uris, str) and raw_uris.strip():
        try:
            parsed_uris = json.loads(raw_uris)
        except (ValueError, RecursionError):
            parsed_uris = None
        if isinstance(parsed_uris, list):
            for uri in parsed_uris:
                path = _file_uri_to_path(uri)
                if path:
                    project_dir = path
                    break
        elif isinstance(parsed_uris, str):
            project_dir = _file_uri_to_path(parsed_uris)

    parent_raw = row_map.get("parent_conversation_id")
    parent_uuid = (
        parent_raw.strip()
        if isinstance(parent_raw, str) and parent_raw.strip()
        else parent_from_table
    )

    title_raw = row_map.get("title")
    title = (
        title_raw
        if isinstance(title_raw, str) and title_raw.strip()
        else None
    )

    agent_raw = row_map.get("agent_name")
    subagent_type = (
        agent_raw.strip()
        if isinstance(agent_raw, str) and agent_raw.strip()
        else None
    )

    depth_raw = row_map.get("nesting_depth")
    spawn_depth = (
        depth_raw
        if isinstance(depth_raw, int)
        and not isinstance(depth_raw, bool)
        and depth_raw > 0
        else None
    )

    created_at = _parse_ag_timestamp(row_map.get("last_modified_time"))

    return _BrainMeta(
        project_dir=project_dir,
        created_at=created_at,
        parent_uuid=parent_uuid,
        subagent_type=subagent_type,
        title=title,
        spawn_depth=spawn_depth,
        is_subagent=bool(parent_uuid) or (spawn_depth is not None and spawn_depth > 0),
    )


def _parse_subagent_json_file(path: Path, parent_uuid: str) -> Optional[_BrainMeta]:
    """Parse one ``<parent_brain>/.system_generated/subagents/<child>.json``."""
    try:
        if not path.is_file() or path.stat().st_size > _SUBAGENT_JSON_MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None

    desc = data.get("subagentDescriptor")
    desc_dict = desc if isinstance(desc, dict) else {}

    type_raw = desc_dict.get("typeName")
    subagent_type = (
        type_raw.strip()
        if isinstance(type_raw, str) and type_raw.strip()
        else None
    )

    role_raw = desc_dict.get("role")
    title = (
        role_raw
        if isinstance(role_raw, str) and role_raw.strip()
        else None
    )

    project_dir: Optional[str] = None
    uris = data.get("workspaceUris")
    if isinstance(uris, list):
        for uri in uris:
            path_str = _file_uri_to_path(uri)
            if path_str:
                project_dir = path_str
                break

    return _BrainMeta(
        project_dir=project_dir,
        parent_uuid=parent_uuid,
        subagent_type=subagent_type,
        title=title,
        is_subagent=True,
    )


def _build_subagent_json_index(brain_root: Path) -> Dict[str, _BrainMeta]:
    """Index ``<parent>/.system_generated/subagents/<child>.json`` in one pass."""
    index: Dict[str, _BrainMeta] = {}
    try:
        entries = list(brain_root.iterdir())
    except OSError:
        return index
    for parent_brain in entries:
        if not parent_brain.is_dir():
            continue
        sub_dir = parent_brain / ".system_generated" / "subagents"
        if not sub_dir.is_dir():
            continue
        try:
            files = list(sub_dir.iterdir())
        except OSError:
            continue
        for jf in files:
            if jf.suffix != ".json" or not jf.is_file():
                continue
            child_uuid = jf.stem
            if child_uuid in index:
                continue
            meta = _parse_subagent_json_file(jf, parent_brain.name)
            if meta is not None:
                index[child_uuid] = meta
    return index


def _find_subagent_json_for_child(brain: Path) -> Optional[_BrainMeta]:
    """Standalone lookup of ``<sibling>/.system_generated/subagents/<uuid>.json``."""
    child_uuid = brain.name
    parent_dir = brain.parent
    try:
        siblings = list(parent_dir.iterdir())
    except OSError:
        return None
    for sibling in siblings:
        if sibling == brain or not sibling.is_dir():
            continue
        candidate = sibling / ".system_generated" / "subagents" / f"{child_uuid}.json"
        if candidate.is_file():
            meta = _parse_subagent_json_file(candidate, sibling.name)
            if meta is not None:
                return meta
    return None


def _load_brain_meta(
    brain: Path,
    subagent_index: Optional[Dict[str, _BrainMeta]] = None,
) -> _BrainMeta:
    """Resolve binary/index metadata for ``brain`` with full fallback chain.

    Order:
    1. ``<root>/conversations/<uuid>.db`` (``trajectory_metadata_blob`` ``id='main'``)
    2. ``<root>/conversations/<uuid>.pb``
    3. ``<root>/conversation_summaries.db``
    4. ``<parent_brain>/.system_generated/subagents/<child_uuid>.json``
    """
    uuid = brain.name
    meta = _BrainMeta()
    has_primary = False

    if brain.parent.name == "brain":
        ag_root = brain.parent.parent
        conv_dir = ag_root / "conversations"

        db_meta = _read_conv_db_meta(conv_dir / f"{uuid}.db")
        if db_meta is not None:
            meta = _merge_meta(meta, db_meta)
            has_primary = True

        if not has_primary:
            pb_meta = _read_conv_pb_meta(conv_dir / f"{uuid}.pb")
            if pb_meta is not None:
                meta = _merge_meta(meta, pb_meta)
                has_primary = True

        if not has_primary or not meta.project_dir:
            sdb_meta = _read_summaries_db_meta(
                ag_root / "conversation_summaries.db", uuid
            )
            if sdb_meta is not None:
                meta = _merge_meta(meta, sdb_meta)

    # Fallback 3: parent-brain subagents/<child_uuid>.json (works even under
    # arbitrary base_dir layouts).
    if not meta.parent_uuid and not (has_primary and meta.root_uuid == uuid):
        sub_meta = (
            subagent_index.get(uuid)
            if subagent_index is not None
            else _find_subagent_json_for_child(brain)
        )
        if sub_meta is not None:
            meta = _merge_meta(meta, sub_meta)

    return meta


# ---------------------------------------------------------------------------
# Root discovery, launch surface, and cross-root deduplication
# ---------------------------------------------------------------------------


def _resolve_brain_roots(
    base_dir: Optional[str] = None,
) -> List[Path]:
    """Return the list of brain roots to scan, in priority order.

    When ``base_dir`` is supplied, it is treated as a single brain
    directory and returned as-is.  Otherwise we look at the four known
    installations under ``$AI_R_HOME/.gemini`` (if set) or ``~/.gemini``.
    """
    if base_dir:
        return [Path(base_dir).expanduser()]

    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        root = Path(env_home).expanduser() / ".gemini"
    else:
        root = Path("~/.gemini").expanduser()

    roots = [root / name / "brain" for name in _ROOT_NAMES]
    return [r for r in roots if r.is_dir()]


def source_roots(base_dir: Optional[str] = None) -> List[str]:
    """Candidate brain root(s) for Antigravity sessions (may not exist).

    Unlike :func:`_resolve_brain_roots` (which filters to existing dirs so
    the scanner never walks a missing tree), this returns the *candidate*
    locations unfiltered — :mod:`ai_r.diagnostics` needs the paths that
    were looked at even when none of them exist.
    """
    if base_dir:
        return [str(Path(base_dir).expanduser())]
    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        root = Path(env_home).expanduser() / ".gemini"
    else:
        root = Path("~/.gemini").expanduser()
    return [str(root / name / "brain") for name in _ROOT_NAMES]


def _launch_surface_for_brain(brain: Path) -> Optional[str]:
    """Launch surface from WHICH brain root holds the session.

    Returns ``"antigravity-cli"`` for ``antigravity-cli``,
    ``"antigravity-ide"`` for ``antigravity`` and ``antigravity-ide``,
    and ``None`` for ``antigravity-backup`` or arbitrary ``base_dir``
    layouts — the signal is derived, never fabricated.
    """
    parent = brain.parent
    if parent.name != "brain":
        return None
    root_name = parent.parent.name
    if root_name == "antigravity-cli":
        return "antigravity-cli"
    if root_name in ("antigravity", "antigravity-ide"):
        return "antigravity-ide"
    return None


def _brain_rank(brain: Path, root_idx: int) -> Tuple[int, int, int, float]:
    """Rank duplicate brain directories across roots (higher wins).

    Order of precedence:
    1. Transcript richness (``transcript_full.jsonl`` > ``transcript.json``
       > ``transcript.jsonl`` > ``overview.txt`` > none).
    2. Companion binary metadata richness (``.db`` > ``.pb`` > none).
    3. Root priority (``antigravity-cli`` > ``antigravity`` >
       ``antigravity-ide`` > ``antigravity-backup``).
    4. Directory/transcript mtime as final tiebreaker.
    """
    transcript_tier = 0
    mtime = 0.0
    total_candidates = len(_TRANSCRIPT_CANDIDATES)
    for idx, rel in enumerate(_TRANSCRIPT_CANDIDATES):
        tpath = brain / rel
        if tpath.is_file():
            transcript_tier = total_candidates - idx
            try:
                mtime = tpath.stat().st_mtime
            except OSError:
                mtime = 0.0
            break
    if mtime == 0.0:
        try:
            mtime = brain.stat().st_mtime
        except OSError:
            mtime = 0.0

    binary_tier = 0
    if brain.parent.name == "brain":
        conv_dir = brain.parent.parent / "conversations"
        if (conv_dir / f"{brain.name}.db").is_file():
            binary_tier = 2
        elif (conv_dir / f"{brain.name}.pb").is_file():
            binary_tier = 1

    return (transcript_tier, binary_tier, -root_idx, mtime)


# ---------------------------------------------------------------------------
# Record helpers (timestamps, sorting, tokens, media, titles)
# ---------------------------------------------------------------------------


def _record_timestamp_str(record: dict) -> Optional[str]:
    """Return the raw ISO timestamp string (``created_at`` preferred)."""
    ts = record.get("created_at")
    if isinstance(ts, str) and ts:
        return ts
    ts = record.get("timestamp")
    if isinstance(ts, str) and ts:
        return ts
    return None


def _parse_record_timestamp(record: dict) -> Optional[datetime]:
    raw = _record_timestamp_str(record)
    return _parse_ag_timestamp(raw) if raw else None


def _step_index_of(record: dict) -> Optional[int]:
    val = record.get("step_index")
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return None


def _sort_records_by_step_index(records: List[dict]) -> List[dict]:
    """Stably sort transcript records by ``step_index`` when present."""
    if not records:
        return records
    indexed = list(enumerate(records))
    indexed.sort(
        key=lambda item: (
            0 if _step_index_of(item[1]) is not None else 1,
            _step_index_of(item[1]) or 0,
            item[0],
        )
    )
    return [rec for _, rec in indexed]


def _safe_nonneg_int(val: Any) -> Optional[int]:
    """Accept only non-negative ``int`` (explicitly rejecting ``bool``)."""
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return val
    return None


def _extract_record_tokens(record: dict) -> Optional[dict]:
    """Extract normalized per-call token usage from a ``MODEL`` record."""
    inp = _safe_nonneg_int(record.get("input_tokens"))
    out = _safe_nonneg_int(record.get("output_tokens"))
    cache_read = _safe_nonneg_int(record.get("cache_read_tokens"))
    inp_v = inp or 0
    out_v = out or 0
    cr_v = cache_read or 0
    total = inp_v + out_v + cr_v
    if total <= 0:
        return None
    return {
        "input": inp_v,
        "output": out_v,
        "reasoning": 0,
        "cache_read": cr_v,
        "cache_write": 0,
        "total": total,
    }


def _extract_media_user_refs(record: dict) -> Tuple[dict, ...]:
    """Map ``record["media"]`` entries into ``origin="structured"`` user_refs."""
    media = record.get("media")
    if not isinstance(media, list):
        return ()
    refs: List[dict] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        mime = item.get("mime_type")
        mime_str = mime.strip().lower() if isinstance(mime, str) else ""
        uri = item.get("uri")
        uri_str = uri.strip() if isinstance(uri, str) else ""
        if not mime_str and not uri_str:
            continue

        if mime_str.startswith("image/"):
            kind = "image"
        elif uri_str.startswith(("http://", "https://")):
            kind = "url"
        elif uri_str.startswith("file://"):
            kind = "file"
        else:
            kind = "attachment"

        target: Optional[str]
        if uri_str.startswith("file://"):
            target = _file_uri_to_path(uri_str)
        else:
            target = uri_str or None

        refs.append(make_user_ref(kind, target, "structured"))
    return tuple(refs)


def _extract_title_from_overview(overview_path: Path) -> Tuple[str, int, Optional[str]]:
    """Return ``(title, message_count, latest_timestamp_iso)`` from overview/transcript."""
    title = ""
    count = 0
    latest_ts: Optional[str] = None
    for record in iter_jsonl_records(overview_path):
        count += 1
        ts = _record_timestamp_str(record)
        if ts:
            latest_ts = ts
        if (
            not title
            and record.get("source") == "USER_EXPLICIT"
            and record.get("type") == "USER_INPUT"
        ):
            raw_content = record.get("content", "")
            if isinstance(raw_content, str) and raw_content:
                match = _USER_REQUEST_RE.search(raw_content)
                if match:
                    title = match.group(1)
                else:
                    title = raw_content
    return title, count, latest_ts


def _extract_title_from_markdown(brain: Path) -> str:
    """Return the first ``# Heading`` found in any of the markdown plans."""
    for name in _MARKDOWN_CANDIDATES:
        path = brain / name
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                data = fh.read(_TITLE_MAX_LEN * 4)
        except OSError:
            continue
        match = _HEADING_RE.search(data)
        if match:
            return match.group(1)
    return ""


def _select_primary_transcript(brain: Path) -> Tuple[Optional[Path], List[dict]]:
    """Return ``(path, records)`` for the highest-priority non-empty transcript."""
    first_existing: Optional[Path] = None
    for rel in _TRANSCRIPT_CANDIDATES:
        tpath = brain / rel
        if not tpath.is_file():
            continue
        if first_existing is None:
            first_existing = tpath
        records = list(iter_jsonl_records(tpath))
        if records:
            return tpath, records
    return first_existing, []


# ---------------------------------------------------------------------------
# Session scanning & discovery
# ---------------------------------------------------------------------------


def _scan_brain(
    brain: Path,
    *,
    subagent_index: Optional[Dict[str, _BrainMeta]] = None,
) -> Optional[Session]:
    """Build a :class:`Session` from one brain directory + companion metadata."""
    if not brain.is_dir():
        return None

    _, raw_records = _select_primary_transcript(brain)
    records = _sort_records_by_step_index(raw_records)

    title = ""
    count = len(records)
    latest_ts: Optional[datetime] = None
    models: List[str] = []
    seen_models: set[str] = set()

    for record in records:
        ts = _parse_record_timestamp(record)
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_ts = ts

        if (
            not title
            and record.get("source") == "USER_EXPLICIT"
            and record.get("type") == "USER_INPUT"
        ):
            raw_content = record.get("content", "")
            if isinstance(raw_content, str) and raw_content:
                match = _USER_REQUEST_RE.search(raw_content)
                title = match.group(1) if match else raw_content

        source = record.get("source", "")
        rtype = record.get("type", "")
        if (isinstance(source, str) and source.startswith("MODEL")) or (
            isinstance(rtype, str) and rtype.startswith("MODEL")
        ):
            raw_model = record.get("model")
            if isinstance(raw_model, str):
                model_name = raw_model.strip()
                if model_name and model_name not in seen_models:
                    seen_models.add(model_name)
                    models.append(model_name)

    if not title:
        title = _extract_title_from_markdown(brain)

    meta = _load_brain_meta(brain, subagent_index=subagent_index)

    if not title and meta.title:
        title = meta.title

    if not title:
        if count == 0:
            return None
        title = "Untitled"

    timestamp = latest_ts or meta.created_at
    if timestamp is None:
        try:
            timestamp = datetime.fromtimestamp(
                brain.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return None

    uuid = brain.name
    parent_uuid = meta.parent_uuid
    is_subagent = (
        bool(parent_uuid)
        or (bool(meta.root_uuid) and meta.root_uuid != uuid)
        or meta.is_subagent
    )
    kind = "subagent" if is_subagent else "agent"

    extra: Dict[str, Any] = {}
    if meta.subagent_type:
        extra["subagent_type"] = meta.subagent_type
    if meta.spawn_depth is not None:
        extra["spawn_depth"] = meta.spawn_depth

    return Session(
        uuid=uuid,
        agent=AgentName.ANTIGRAVITY,
        title=_normalise_title(title),
        date=timestamp,
        path=str(brain),
        message_count=count,
        parent_uuid=parent_uuid,
        kind=kind,
        project_dir=meta.project_dir,
        launch_surface=_launch_surface_for_brain(brain),
        models=tuple(models),
        extra=extra,
    )


def list_sessions(base_dir: Optional[str] = None) -> List[Session]:
    """Return every Antigravity session across all search roots (deduped by uuid)."""
    candidates_by_uuid: Dict[str, List[Tuple[Tuple[int, int, int, float], Path, int]]] = {}
    subagent_indices: Dict[int, Dict[str, _BrainMeta]] = {}

    for root_idx, root in enumerate(_resolve_brain_roots(base_dir)):
        if not root.is_dir():
            continue
        subagent_indices[root_idx] = _build_subagent_json_index(root)
        try:
            brains = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for brain in brains:
            if not brain.is_dir():
                continue
            rank = _brain_rank(brain, root_idx)
            candidates_by_uuid.setdefault(brain.name, []).append(
                (rank, brain, root_idx)
            )

    sessions: List[Session] = []
    for candidates in candidates_by_uuid.values():
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, brain, root_idx in candidates:
            session = _scan_brain(
                brain, subagent_index=subagent_indices.get(root_idx)
            )
            if session is not None:
                sessions.append(session)
                break

    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _find_brain(uuid: str, base_dir: Optional[str]) -> Path:
    if not _is_valid_uuid(uuid):
        raise ValueError(f"Invalid Antigravity session uuid: {uuid!r}")
    matches: List[Tuple[Tuple[int, int, int, float], Path]] = []
    for root_idx, root in enumerate(_resolve_brain_roots(base_dir)):
        candidate = root / uuid
        if candidate.is_dir():
            matches.append((_brain_rank(candidate, root_idx), candidate))
    if not matches:
        raise FileNotFoundError(f"Antigravity session {uuid!r} not found")
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def read_session(uuid: str, base_dir: Optional[str] = None) -> Session:
    """Read a single Antigravity session by ``uuid`` (brain directory name)."""
    brain = _find_brain(uuid, base_dir)
    session = _scan_brain(brain)
    if session is None:
        raise FileNotFoundError(
            f"Antigravity session {uuid!r} at {brain} yielded no parseable data"
        )
    return session


# ---------------------------------------------------------------------------
# Message extraction & positional tool_calls <-> MODEL/GENERIC correlation
# ---------------------------------------------------------------------------


def _serialise_tool_args(args: Any) -> str:
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


def _extract_tool_result_entry(
    record: dict, *, tool_use_id: Optional[str] = None
) -> dict:
    """Build one normalised ``tool_result`` dict from a ``MODEL/GENERIC`` record."""
    status = record.get("status")
    err = record.get("error")
    err_str = err if isinstance(err, str) else ""
    is_error = (
        isinstance(status, str) and status in ("ERROR", "INVALID")
    ) or bool(err_str.strip())

    raw_content = record.get("content")
    if isinstance(raw_content, str) and raw_content:
        content_str = raw_content
        if err_str and err_str not in content_str:
            content_str = f"{content_str}\n{err_str}"
    elif err_str:
        content_str = err_str
    elif isinstance(raw_content, (list, dict)):
        content_str = _serialise_tool_args(raw_content)
    else:
        content_str = ""

    entry: Dict[str, Any] = {
        "content": content_str,
        "is_error": is_error,
    }
    if tool_use_id is not None:
        entry["tool_use_id"] = tool_use_id
    return entry


# NOTE (interactive question→answer pairs): the parsed Antigravity surface
# (overview.txt / transcript*.jsonl) has NO structured interactive-question
# record — observed record ``type`` values are PLANNER_RESPONSE / GENERIC /
# USER_INPUT / ... with no AskUserQuestion-style call+answer.
# Antigravity therefore never populates :attr:`Message.qa`.
def _antigravity_message_from_record(
    record: dict, *, fallback_step_id: int = 0
) -> Optional[Message]:
    """Map one Antigravity user or assistant transcript record to a :class:`Message`."""
    source = record.get("source", "")
    rtype = record.get("type", "")
    if isinstance(source, str) and source.startswith("USER"):
        role = "user"
    elif isinstance(source, str) and source.startswith("MODEL"):
        role = "assistant"
    elif isinstance(rtype, str) and rtype.startswith("USER"):
        role = "user"
    elif isinstance(rtype, str) and rtype.startswith("MODEL"):
        role = "assistant"
    else:
        return None

    ts = _parse_record_timestamp(record)
    content = record.get("content", "")
    text_chunks: List[str] = []
    tool_use: List[dict] = []
    step_id = _step_index_of(record)
    if step_id is None:
        step_id = fallback_step_id

    def _record_tool(part: dict, *, assign_id: bool = False) -> None:
        name = part.get("name", "") if isinstance(part, dict) else ""
        if not isinstance(name, str) or not name:
            return
        args = part.get("args", part.get("input", part.get("arguments", "")))
        entry: Dict[str, Any] = {
            "name": name,
            "input": _serialise_tool_args(args),
            "timestamp": ts,
        }
        if assign_id:
            entry["tool_use_id"] = f"ag-step-{step_id}-{len(tool_use)}"
        tool_use.append(entry)

    if role == "assistant":
        raw_tool_calls = record.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for tc in raw_tool_calls:
                if isinstance(tc, dict):
                    _record_tool(tc, assign_id=True)

    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype in _TOOL_PART_TYPES:
                _record_tool(part)
                continue
            t = part.get("text", "")
            if isinstance(t, str) and t:
                text_chunks.append(t)
    elif isinstance(content, dict):
        if content.get("type", "") in _TOOL_PART_TYPES:
            _record_tool(content)
    elif isinstance(content, str):
        if content:
            text_chunks.append(content)

    # Record-level tool spec (legacy synthetic shape).
    if isinstance(rtype, str) and rtype in _TOOL_PART_TYPES:
        _record_tool(record)

    text = "\n".join(text_chunks)

    if role == "user":
        return Message(
            role="user",
            text=text,
            tool_use=tuple(tool_use),
            tool_result=(),
            timestamp=ts,
            user_refs=_extract_media_user_refs(record),
        )

    raw_thinking = record.get("thinking")
    thinking = raw_thinking if isinstance(raw_thinking, str) else ""

    raw_model = record.get("model")
    model = (
        raw_model.strip()
        if isinstance(raw_model, str) and raw_model.strip()
        else None
    )

    tokens = _extract_record_tokens(record)

    return Message(
        role="assistant",
        text=text,
        tool_use=tuple(tool_use),
        tool_result=(),
        timestamp=ts,
        thinking=thinking,
        tokens=tokens,
        model=model,
    )


def _extract_messages_from_transcript(path: Path) -> List[Message]:
    """Read and correlate an Antigravity transcript/overview JSONL into messages."""
    records = _sort_records_by_step_index(list(iter_jsonl_records(path)))
    messages: List[Message] = []

    active_assistant_idx: Optional[int] = None
    pending_call_ids: deque[str] = deque()
    active_tool_results: List[dict] = []

    def _flush_active() -> None:
        nonlocal active_assistant_idx, active_tool_results
        if active_assistant_idx is not None and active_tool_results:
            target = messages[active_assistant_idx]
            messages[active_assistant_idx] = dataclasses.replace(
                target,
                tool_result=target.tool_result + tuple(active_tool_results),
            )
        active_assistant_idx = None
        pending_call_ids.clear()
        active_tool_results = []

    for rec_idx, record in enumerate(records):
        source = record.get("source", "")
        rtype = record.get("type", "")

        # Tool result record (MODEL/GENERIC or legacy tool-result types).
        if (
            isinstance(source, str)
            and source == "MODEL"
            and isinstance(rtype, str)
            and rtype in _TOOL_RESULT_TYPES
        ):
            if active_assistant_idx is not None and pending_call_ids:
                call_id = pending_call_ids.popleft()
                active_tool_results.append(
                    _extract_tool_result_entry(record, tool_use_id=call_id)
                )
            else:
                _flush_active()
                messages.append(
                    Message(
                        role="tool",
                        text="",
                        tool_result=(_extract_tool_result_entry(record),),
                        timestamp=_parse_record_timestamp(record),
                    )
                )
            continue

        # SYSTEM / SYSTEM_SDK and other non-conversation records: skip
        # without disrupting pending tool-call correlation.
        parsed = _antigravity_message_from_record(record, fallback_step_id=rec_idx)
        if parsed is None:
            continue

        _flush_active()
        messages.append(parsed)
        if parsed.role == "assistant" and parsed.tool_use:
            call_ids = [
                tc["tool_use_id"]
                for tc in parsed.tool_use
                if isinstance(tc.get("tool_use_id"), str)
            ]
            if call_ids:
                active_assistant_idx = len(messages) - 1
                pending_call_ids.extend(call_ids)
                active_tool_results = []

    _flush_active()
    return messages


def _extract_messages_from_brain(brain: Path) -> List[Message]:
    """Read messages from the highest-priority non-empty transcript in a brain."""
    for candidate in _TRANSCRIPT_CANDIDATES:
        tpath = brain / candidate
        if tpath.is_file():
            messages = _extract_messages_from_transcript(tpath)
            if messages:
                return messages
    return []


def read_messages(
    uuid: str, base_dir: Optional[str] = None
) -> List[Message]:
    """Return the full message list for an Antigravity session.

    Reuses :func:`read_session` for path resolution.  Reads the highest-priority
    available transcript file (``transcript_full.jsonl``, ``transcript.json``,
    ``transcript.jsonl``, ``overview.txt``).

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir)
    return _extract_messages_from_brain(Path(session.path))


def read_token_usage(
    uuid: str, base_dir: Optional[str] = None
) -> Optional[dict]:
    """Return exact summed token usage across ``PLANNER_RESPONSE`` steps, or ``None``.

    Antigravity ``transcript_full.jsonl`` records per-call ``input_tokens``,
    ``output_tokens``, and ``cache_read_tokens`` on ``MODEL`` steps; legacy
    brains (``overview.txt``) carry no token counters and return ``None`` so
    :mod:`ai_r.tokens` degrades honestly to a labeled transcript estimate.

    Raises:
        FileNotFoundError: the session does not exist.
        ValueError: ``uuid`` is malformed.
    """
    brain = _find_brain(uuid, base_dir)
    _, records = _select_primary_transcript(brain)
    if not records:
        return None

    total_in = 0
    total_out = 0
    total_cache_read = 0

    for record in records:
        source = record.get("source", "")
        rtype = record.get("type", "")
        if not (
            (isinstance(source, str) and source.startswith("MODEL"))
            or (isinstance(rtype, str) and rtype.startswith("MODEL"))
        ):
            continue
        usage = _extract_record_tokens(record)
        if usage is None:
            continue
        total_in += usage["input"]
        total_out += usage["output"]
        total_cache_read += usage["cache_read"]

    total = total_in + total_out + total_cache_read
    if total <= 0:
        return None
    return {
        "input": total_in,
        "output": total_out,
        "reasoning": 0,
        "cache_read": total_cache_read,
        "cache_write": 0,
        "total": total,
    }


def search(query: str, base_dir: Optional[str] = None) -> List[Session]:
    """Case-insensitive substring search across Antigravity session titles."""
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
        _find_brain(uuid, base_dir)
    except (FileNotFoundError, ValueError):
        return False
    return True
