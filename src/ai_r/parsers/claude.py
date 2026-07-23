"""Claude Code session parser.

Source layout (CLI root)::

    ~/.claude/projects/<project-slug>/<session-uuid>.jsonl

Each line is a JSON object with the following relevant keys:

* ``type``         — ``"user"``, ``"assistant"``, ``"custom-title"``,
  ``"ai-title"`` or other event types (``"queue-operation"``, …).
* ``timestamp``    — ISO 8601 string (last record wins for the date).
* ``message``      — for ``user``/``assistant`` records only.  Either a
  string or a list of parts, where each part has ``type`` (``"text"``,
  ``"tool_use"``, ``"tool_result"``) and a ``text`` / ``content`` field.
* ``aiTitle``      — for ``"ai-title"`` records, optional auto-generated
  title.
* ``customTitle``  — for ``"custom-title"`` records, optional
  user-supplied title (highest priority).

Title resolution order used by :func:`extract_title` and
:func:`_scan_file` is:

1. ``custom-title`` event value.
2. ``ai-title`` event value.
3. First user message text (first line, stripped, max 100 chars).
4. ``chat-HHMM`` derived from the JSONL file mtime, falling back to
   ``"Untitled"``.

The base directory can be overridden for tests by passing ``base_dir``
explicitly to the module-level functions.  When unset, the directory
is read from the ``AI_R_HOME`` environment variable (used as
``$AI_R_HOME/.claude/projects``), falling back to
``~/.claude/projects``.

Desktop root (metadata overlay, F1.3)
-------------------------------------

Claude Desktop keeps its OWN per-session store::

    ~/.config/Claude/claude-code-sessions/<device-uuid>/<workspace-uuid>/local_<id>.json

Each file is a SINGLE JSON object (not JSONL) of session *metadata* — no
transcript.  Relevant keys observed on disk: ``sessionId``
(``"local_<id>"``), ``cliSessionId`` (the uuid of the backing CLI JSONL
transcript under ``~/.claude/projects``), ``title`` + ``titleSource``
(the user-visible Desktop title), ``cwd``/``originCwd``,
``createdAt``/``lastActivityAt``/``lastFocusedAt`` (epoch **milliseconds**),
``model``, ``permissionMode``, ``isArchived``.

Desktop-launched sessions therefore normally exist in BOTH roots: the
transcript in the CLI root, the metadata in the Desktop root.  The parser
scans both and deduplicates by uuid (``cliSessionId`` == the JSONL stem):

* transcript found in the CLI root **and** matched by a Desktop metadata
  file → ONE session, enriched: the Desktop ``title`` wins (it is what the
  user sees in the app; the CLI-derived title is kept as
  ``extra["cli_title"]``) and ``extra["source_root"]`` flips to
  ``"desktop"``;
* transcript only → ``extra["source_root"] == "cli"``;
* metadata only (transcript deleted/never synced) → a reference-only
  session built from the metadata (``message_count == 0``, ``path`` points
  at the metadata JSON, ``source_root == "desktop"``).

``extra["source_root"]`` is deliberately a *launch-surface* signal ("was
this session driven from the Desktop app?"), NOT a "where did the bytes
come from" flag.  F1.4 surfaces it first-class as
``Session.launch_surface`` (``"claude-cli"`` | ``"claude-desktop"``),
alongside ``Session.project_dir`` (record-level ``cwd`` from the
transcript, else the Desktop metadata ``cwd``, else a
filesystem-verified decode of the storage slug).

The Desktop root honours the same overrides: an explicit ``desktop_dir``
argument, else ``$AI_R_HOME/.config/Claude/claude-code-sessions``, else
``~/.config/Claude/claude-code-sessions``.  A missing root is silently
skipped (not an error).  To keep explicit-``base_dir`` callers hermetic
(tests point ``base_dir`` at a fixture tree and must not see the real
HOME), the Desktop root participates only when ``desktop_dir`` is given
explicitly OR ``base_dir`` was NOT given (both roots env-resolved).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ._common import (
    _parse_iso_timestamp,
    _qa_entry,
    _qa_options_from_question,
    _qa_pairs_from_result_text,
    fold_orphan_thinking,
    iter_jsonl_records,
)
from .models import AgentName, Message, Session
from ..user_refs import make_user_ref


_TITLE_MAX_LEN = 100


def _resolve_base_dir(base_dir: Optional[str]) -> Path:
    """Return the Claude projects directory.

    Lookup order:

    1. Explicit ``base_dir`` argument.
    2. ``$AI_R_HOME/.claude/projects``.
    3. ``~/.claude/projects``.
    """
    if base_dir:
        return Path(base_dir).expanduser()
    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        return Path(env_home).expanduser() / ".claude" / "projects"
    return Path("~/.claude/projects").expanduser()


def _resolve_desktop_dir(desktop_dir: Optional[str]) -> Path:
    """Return the Claude Desktop session-metadata directory.

    Lookup order (mirrors :func:`_resolve_base_dir` so hermetic tests that
    fake ``AI_R_HOME`` redirect BOTH roots):

    1. Explicit ``desktop_dir`` argument.
    2. ``$AI_R_HOME/.config/Claude/claude-code-sessions``.
    3. ``~/.config/Claude/claude-code-sessions``.
    """
    if desktop_dir:
        return Path(desktop_dir).expanduser()
    env_home = os.environ.get("AI_R_HOME")
    if env_home:
        return (
            Path(env_home).expanduser()
            / ".config"
            / "Claude"
            / "claude-code-sessions"
        )
    return Path("~/.config/Claude/claude-code-sessions").expanduser()


def _desktop_scan_enabled(
    base_dir: Optional[str], desktop_dir: Optional[str]
) -> bool:
    """Whether the Desktop overlay participates in this call.

    ``True`` when ``desktop_dir`` is explicit, or when NEITHER root is
    explicit (both env-resolved — the normal production path).  An
    explicit ``base_dir`` alone pins the scan to that one root, keeping
    existing fixture-scoped callers hermetic (no real-HOME leak).
    """
    return desktop_dir is not None or base_dir is None


def source_roots(
    base_dir: Optional[str] = None, desktop_dir: Optional[str] = None
) -> List[str]:
    """Candidate source root(s) for Claude sessions.

    Returns the directories the parser *would* scan — whether or not they
    exist.  Used by :mod:`ai_r.diagnostics` to explain empty results
    ("source directory not found" vs "source present but empty").  The
    Desktop metadata root is included under the participation rule of
    :func:`_desktop_scan_enabled`.
    """
    roots = [str(_resolve_base_dir(base_dir))]
    if _desktop_scan_enabled(base_dir, desktop_dir):
        roots.append(str(_resolve_desktop_dir(desktop_dir)))
    return roots


def _extract_text_from_user_message(message: dict) -> str:
    """Return the first plain-text part of a user message, or empty string.

    ``message`` is untrusted: a corrupt record can carry a list/str/int
    where the format promises an object, so a non-dict yields "" rather
    than an ``AttributeError`` from ``.get`` (found by the parser fuzz).
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text", "")
            if not isinstance(text, str):
                continue
            text = text.strip()
            if not text or text.startswith("<"):
                continue
            return text
    return ""


def _normalise_title(raw: str) -> str:
    """Collapse newlines and truncate to ``_TITLE_MAX_LEN`` chars."""
    if not raw:
        return "Untitled"
    return raw.replace("\n", " ").replace("\r", " ").strip()[:_TITLE_MAX_LEN]


def _scan_titles_from_jsonl(
    jsonl_path: Path,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(custom_title, ai_title)`` from a Claude JSONL file."""
    custom_title: Optional[str] = None
    ai_title: Optional[str] = None
    for record in iter_jsonl_records(jsonl_path):
        rec_type = record.get("type")
        if rec_type == "custom-title" and custom_title is None:
            raw = record.get("customTitle", "")
            if isinstance(raw, str) and raw.strip():
                custom_title = raw.strip()
        elif rec_type == "ai-title" and ai_title is None:
            raw = record.get("aiTitle", "")
            if isinstance(raw, str) and raw.strip():
                ai_title = raw.strip()
    return custom_title, ai_title


def _first_user_text_from_messages(messages: List[Message]) -> Optional[str]:
    """Return the first non-empty user message text, or ``None``."""
    for msg in messages:
        if msg.role == "user" and msg.text.strip():
            return msg.text
    return None


def _resolve_title(
    custom_title: Optional[str],
    ai_title: Optional[str],
    first_user_text: Optional[str],
    jsonl_path: Optional[Path],
) -> Optional[str]:
    """Pick a session title from the available signals.

    Returns ``None`` when no signal yields a usable title.
    """
    if custom_title:
        return _normalise_title(custom_title)
    if ai_title:
        return _normalise_title(ai_title)
    if first_user_text:
        return _normalise_title(first_user_text)
    if jsonl_path is not None:
        try:
            ts = datetime.fromtimestamp(jsonl_path.stat().st_mtime)
            return _normalise_title(f"chat-{ts.strftime('%H%M')}")
        except OSError:
            pass
    return None


def extract_title(
    messages: List[Message], jsonl_path: Optional[Path] = None
) -> str:
    """Resolve a Claude session title from jsonl events and message content.

    Priority:

    1. ``custom-title`` event with a non-empty string value (only when
       ``jsonl_path`` is provided).
    2. ``ai-title`` event with a non-empty string value (only when
       ``jsonl_path`` is provided).
    3. First user message in ``messages`` — first line, stripped, max
       100 characters.
    4. ``chat-HHMM`` derived from the ``jsonl_path`` mtime, falling
       back to ``"Untitled"``.
    """
    if jsonl_path is not None:
        custom_title, ai_title = _scan_titles_from_jsonl(jsonl_path)
    else:
        custom_title, ai_title = None, None
    first_user_text = _first_user_text_from_messages(messages)
    title = _resolve_title(custom_title, ai_title, first_user_text, jsonl_path)
    return title if title is not None else "Untitled"


def _parent_uuid_from_subagent_path(
    jsonl_path: Path, base_dir: Optional[Path] = None
) -> Optional[str]:
    """Return the parent-session uuid for a ``subagents/`` file, else ``None``.

    Claude stores spawned subagents under one of two on-disk shapes:

    * **directory form** — ``<base>/<slug>/<parent-uuid>/subagents/agent-*.jsonl``:
      the folder wrapping ``subagents/`` is the parent session's own uuid
      folder, so its name IS the (immediate) parent uuid.
    * **flat form** — ``<base>/<slug>/subagents/agent-*.jsonl``: the folder
      wrapping ``subagents/`` is the project-*slug* dir, not a session uuid.
      There is no per-path parent — the ``sessionId`` scan (top-level
      spawner) supplies it instead, and this function returns ``None``.

    Discriminating the two shapes must NOT hinge on a directory being
    literally named ``projects`` (that breaks the moment a caller passes a
    custom ``base_dir`` whose leaf is not ``projects`` — defect #7-B).  The
    real signal is *structural depth relative to the scan root*: the slug
    dir sits directly under ``base_dir``.  So:

    * ``subagents/``'s parent's parent == ``base_dir``  → the wrapping
      folder is the slug → **flat form** → ``None``;
    * otherwise (a per-session uuid folder sits between slug and
      ``subagents/``) → **directory form** → the wrapping folder name.

    When ``base_dir`` is not supplied (legacy single-arg callers, e.g. unit
    tests that hand a bare path), fall back to the historical literal-name
    heuristic so their behaviour is unchanged.
    """
    parent = jsonl_path.parent
    if parent.name != "subagents":
        return None
    grandparent = parent.parent
    grandparent_name = grandparent.name
    if not grandparent_name:
        return None
    if base_dir is not None:
        # Structural test: is the ``subagents/`` wrapper the slug dir
        # (its own parent is the scan root)?  Then it is the flat form.
        try:
            base_resolved = base_dir.resolve()
            wrapper_parent_resolved = grandparent.parent.resolve()
        except OSError:
            base_resolved = base_dir
            wrapper_parent_resolved = grandparent.parent
        if wrapper_parent_resolved == base_resolved:
            return None
        return grandparent_name
    # Legacy fallback (no scan root known): the flat form's slug dir sits
    # directly under a folder literally named ``projects``.
    if grandparent.parent.name == "projects":
        return None
    return grandparent_name


def _read_subagent_meta(
    jsonl_path: Path,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Return ``(tool_use_id, spawn_depth, agent_type)`` from a subagent's
    ``.meta.json``.

    Claude writes a sidecar ``agent-*.meta.json`` next to every
    ``subagents/agent-*.jsonl`` transcript.  Observed shape::

        {"agentType": "...", "description": "...",
         "toolUseId": "toolu_...", "spawnDepth": <int>}

    ``toolUseId`` is the id of the ``Task``/``Agent`` ``tool_use`` block
    that spawned THIS subagent — it appears verbatim in the *spawner's*
    transcript, which is how a >1-level spawn chain is reconnected to its
    true parent (defect #7-A: without it, every subagent collapses to the
    top-level session).  ``spawnDepth`` is 1 for a subagent spawned by the
    top-level session and increments per nesting level.

    ``agentType`` is the PERSONA the child ran as (``explorer`` / ``auditor``
    / …) — the dimension model pins are set on, and the one a cost audit
    groups by.  It is read HERE, from the child's own meta, and NOT from the
    spawner's result sidecar: a background spawn's sidecar is written at
    launch and carries no ``agentType`` at all (the majority of spawns in a
    real vault), whereas the meta file names every child.

    Returns ``(None, None, None)`` when the file is absent, unreadable, not a
    JSON object, or carries no usable ``toolUseId`` — absence is honest and
    the caller falls back to the folder/``sessionId`` parent signal.
    """
    if jsonl_path.suffix != ".jsonl":
        return None, None, None
    meta_path = jsonl_path.with_suffix(".meta.json")
    try:
        record = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None, None, None
    if not isinstance(record, dict):
        return None, None, None
    raw_tuid = record.get("toolUseId")
    tool_use_id = (
        raw_tuid.strip()
        if isinstance(raw_tuid, str) and raw_tuid.strip()
        else None
    )
    raw_depth = record.get("spawnDepth")
    spawn_depth = (
        raw_depth
        if isinstance(raw_depth, int) and not isinstance(raw_depth, bool)
        else None
    )
    raw_type = record.get("agentType")
    agent_type = (
        raw_type.strip()
        if isinstance(raw_type, str) and raw_type.strip()
        else None
    )
    return tool_use_id, spawn_depth, agent_type


def _project_dir_from_slug(slug: str) -> Optional[str]:
    """Best-effort decode of a ``projects/<slug>`` name back to a path.

    Claude flattens the session cwd into the storage slug by replacing
    ``/`` and ``.`` with ``-`` (``/home/u/dev/ai-r`` →
    ``-home-u-dev-ai-r``).  The encoding is LOSSY: a dash inside a real
    directory name is indistinguishable from a separator, so a naive
    ``-``→``/`` decode would corrupt names like ``ai-r`` → ``ai/r``.

    Decoding therefore searches over the possible segment boundaries
    (each dash is either a ``/`` separator or a literal dash inside one
    segment) and verifies against the filesystem at every *segment
    boundary*: every ancestor of a real cwd is itself an existing
    directory, so any candidate prefix that is not a directory prunes
    that branch immediately (bounded DFS, no unverified guessing).
    Returns the decoded path only when the full directory exists;
    ``None`` otherwise — this is a *fallback* signal used only when the
    transcript carries no record-level ``cwd``, and an unverifiable
    guess is worse than an honest absence.  (Dots flattened by the
    encoder are NOT recovered; a dotted cwd only resolves if its dashed
    sibling exists.)
    """
    if not slug.startswith("-"):
        return None
    tokens = slug[1:].split("-")
    if not tokens or not all(tokens):
        return None

    def _resolve(i: int, base: str, pending: str) -> Optional[str]:
        # ``base`` is a verified existing directory ("" == fs root);
        # ``pending`` is the segment currently being assembled.
        if i == len(tokens):
            full = f"{base}/{pending}"
            return full if os.path.isdir(full) else None
        token = tokens[i]
        # Option 1: the next dash was a literal dash — extend the
        # pending segment.  Deferred verification (checked at closure).
        resolved = _resolve(i + 1, base, f"{pending}-{token}")
        if resolved is not None:
            return resolved
        # Option 2: the dash was a separator — close the pending
        # segment (must exist as a directory) and start a new one.
        closed = f"{base}/{pending}"
        if os.path.isdir(closed):
            return _resolve(i + 1, closed, token)
        return None

    return _resolve(1, "", tokens[0])


_SPAWN_TOOL_NAMES = ("Task", "Agent")


def _scan_file(
    jsonl_path: Path, base_dir: Optional[Path] = None
) -> Optional[Session]:
    """Build a :class:`Session` from one Claude JSONL file.

    ``base_dir`` (the scan root) is threaded through so subagent
    flat/nested detection is structural, not tied to a directory literally
    named ``projects`` (defect #7-B).  It is optional so existing
    single-arg unit callers keep working.

    Returns ``None`` if the file yields no usable title/timestamp.

    Subagent detection covers both on-disk shapes:

    * **directory form** — the file lives under a ``subagents/`` folder
      (``.../<parent-uuid>/subagents/agent-*.jsonl``); the parent uuid is
      taken from the folder wrapping ``subagents/``.
    * **inline / flat form** — any record carries ``isSidechain: true``.

    The presence of an ``isSidechain`` *key* is NOT a signal — only the
    value ``True`` marks a sidechain (Claude writes ``isSidechain: false``
    on every normal record).

    The spawner-session uuid (``parent_uuid``) is derived from a *session*
    signal, never a message signal.  Priority: the ``subagents/`` wrapper
    folder name (directory form) → else the sidechain records' own
    ``sessionId`` field (which equals that folder name when present, and is
    the only spawner signal for the flat form).  Record-level
    ``parentUuid`` is deliberately NOT used: it is a message uuid (the
    chain root / previous message), not the spawner session, and using it
    was the A2 defect (``parent_uuid`` = chain root instead of spawner).
    """
    custom_title: Optional[str] = None
    ai_title: Optional[str] = None
    first_user_text: Optional[str] = None
    last_timestamp: Optional[datetime] = None
    message_count = 0
    is_sidechain = False
    record_session_id: Optional[str] = None
    record_cwd: Optional[str] = None
    # Unique assistant ``message.model`` values, in order of first
    # appearance (``<synthetic>`` stubs excluded) → ``Session.models``.
    models: List[str] = []
    # Ids of ``Task``/``Agent`` ``tool_use`` blocks this session emitted —
    # the spawn edges it is the PARENT of.  The list-level reconciliation
    # pass maps a child's ``toolUseId`` (from its ``.meta.json``) back to
    # whichever session emitted it, restoring >1-level hierarchy (#7-A).
    spawn_tool_use_ids: List[str] = []

    for record in iter_jsonl_records(jsonl_path):
        ts = _parse_iso_timestamp(record.get("timestamp", ""))
        if ts is not None:
            last_timestamp = ts

        # Record-level ``cwd`` (present on user/assistant records) is the
        # authoritative project-dir signal; first occurrence wins.
        if record_cwd is None:
            raw_cwd = record.get("cwd")
            if isinstance(raw_cwd, str) and raw_cwd.strip():
                record_cwd = raw_cwd.strip()

        # Inline sidechain detection: value must be True, the mere
        # presence of the key is not enough (it is False everywhere
        # on normal records).  The spawner-session uuid comes from the
        # record's ``sessionId`` (a *session* signal), NOT its
        # ``parentUuid`` (a *message* uuid — chain root / previous msg).
        if record.get("isSidechain") is True:
            is_sidechain = True
            if record_session_id is None:
                raw_sid = record.get("sessionId")
                if isinstance(raw_sid, str) and raw_sid.strip():
                    record_session_id = raw_sid.strip()

        rec_type = record.get("type")
        # Harvest spawn-edge ids: an assistant ``Task``/``Agent`` tool_use
        # block's ``id`` equals the ``toolUseId`` recorded in the spawned
        # child's ``.meta.json``.  Collected here (cheap, id-only) so the
        # list pass can rebuild the true parent for depth>1 subagents.
        if rec_type == "assistant":
            payload = record.get("message")
            if isinstance(payload, dict):
                model = _record_model(payload)
                if model is not None and model not in models:
                    models.append(model)
                content = payload.get("content")
                if isinstance(content, list):
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "tool_use"
                            and part.get("name") in _SPAWN_TOOL_NAMES
                        ):
                            tu_id = part.get("id")
                            if isinstance(tu_id, str) and tu_id:
                                spawn_tool_use_ids.append(tu_id)
        if rec_type == "custom-title" and custom_title is None:
            raw = record.get("customTitle", "")
            if isinstance(raw, str) and raw.strip():
                custom_title = raw.strip()
        elif rec_type == "ai-title" and ai_title is None:
            raw = record.get("aiTitle", "")
            if isinstance(raw, str) and raw.strip():
                ai_title = raw.strip()
        elif rec_type == "user":
            message_count += 1
            text = _extract_text_from_user_message(
                record.get("message", {}) or {}
            )
            if (
                text
                and not text.startswith("<")
                and first_user_text is None
            ):
                first_user_text = text
        elif rec_type == "assistant":
            message_count += 1

    title = _resolve_title(custom_title, ai_title, first_user_text, jsonl_path)
    if title is None:
        return None

    if last_timestamp is None:
        try:
            last_timestamp = datetime.fromtimestamp(
                jsonl_path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return None

    # Resolve subagent classification + parent uuid from BOTH the directory
    # layout and any inline sidechain marker.  ``parent_uuid`` is the
    # *spawner session* uuid, derived only from session-level signals:
    #   1. the ``subagents/`` wrapper folder name (directory form) — the
    #      canonical parent-session folder, most explicit;
    #   2. else the sidechain records' ``sessionId`` (spawner session; the
    #      only signal for the flat form) — but never the file's own uuid,
    #      guarding against a session becoming its own parent.
    # Record-level ``parentUuid`` (a message uuid) is intentionally NOT a
    # source here — that was the A2 defect.
    own_uuid = jsonl_path.stem
    path_parent_uuid = _parent_uuid_from_subagent_path(jsonl_path, base_dir)
    in_subagents_dir = jsonl_path.parent.name == "subagents"
    is_subagent = path_parent_uuid is not None or is_sidechain or in_subagents_dir
    if path_parent_uuid is not None:
        parent_uuid = path_parent_uuid
    elif record_session_id is not None and record_session_id != own_uuid:
        parent_uuid = record_session_id
    else:
        parent_uuid = None

    # project_slug is the first non-``subagents`` ancestor folder name.
    # Directory form: ``<slug>/<uuid>/subagents/`` → skip TWO levels.
    # Flat form:      ``<slug>/subagents/``        → skip ONE level.
    # The two are told apart structurally by ``_parent_uuid_from_subagent_path``
    # (non-``None`` == a per-session wrapper folder is present == directory form).
    slug_dir = jsonl_path.parent
    if slug_dir.name == "subagents":
        slug_dir = slug_dir.parent
        if path_parent_uuid is not None:
            slug_dir = slug_dir.parent
    project_slug = slug_dir.name

    # project_dir: the record-level cwd is authoritative; the storage-slug
    # decode is a filesystem-verified fallback (see _project_dir_from_slug).
    project_dir = record_cwd or _project_dir_from_slug(project_slug)

    extra: dict = {"project_slug": project_slug, "source_root": "cli"}
    # Spawn hierarchy signals (defect #7-A): the child's own spawning
    # ``toolUseId`` + ``spawnDepth`` from its ``.meta.json``, and the ids of
    # the spawn calls THIS session emitted.  Stored so the list-level
    # reconciliation pass can wire depth>1 subagents to their real parent
    # instead of collapsing every subagent to the top-level session.
    if in_subagents_dir:
        spawn_tool_use_id, spawn_depth, subagent_type = _read_subagent_meta(
            jsonl_path
        )
        if spawn_tool_use_id is not None:
            extra["spawn_tool_use_id"] = spawn_tool_use_id
        if spawn_depth is not None:
            extra["spawn_depth"] = spawn_depth
        if subagent_type is not None:
            # The persona this child ran as. From the child's OWN meta, so a
            # background spawn (whose parent-side sidecar names no persona) is
            # still attributable.
            extra["subagent_type"] = subagent_type
    if spawn_tool_use_ids:
        extra["_emitted_spawn_ids"] = tuple(spawn_tool_use_ids)

    return Session(
        uuid=jsonl_path.stem,
        agent=AgentName.CLAUDE,
        title=title,
        date=last_timestamp,
        path=str(jsonl_path),
        message_count=message_count,
        parent_uuid=parent_uuid,
        kind="subagent" if is_subagent else "agent",
        project_dir=project_dir,
        launch_surface="claude-cli",
        models=tuple(models),
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Claude Desktop metadata overlay (F1.3)
# ---------------------------------------------------------------------------


# Desktop-index cache: ``_load_desktop_index`` used to re-``rglob`` +
# re-``json.loads`` every metadata file on every call (and it is called once
# per list/read/find pass).  This cache keeps the last built index per root
# and revalidates it with a stat-only signature of the ``*.json`` files:
# ``(path, mtime_ns, size)`` per file.  Desktop rewrites metadata IN PLACE —
# the file's mtime/size change while the directory's mtime does not — so the
# per-file signature (never a directory mtime) is the correct validator; any
# rewrite, addition or removal forces a rebuild and a HIT is byte-identical
# to a MISS.  An unstattable file (OSError) yields ``None`` → uncacheable
# round, fail open to a fresh scan.  Keyed by the root path (the default
# root plus any explicit ``desktop_dir`` overrides), no eviction needed.
_desktop_index_cache: "dict[str, tuple[tuple, dict[str, Tuple[dict, Path]]]]" = {}
_desktop_index_cache_lock = threading.Lock()


def _desktop_index_signature(root: Path) -> Optional[tuple]:
    """Stat-only change signature of the Desktop root's ``*.json`` files."""
    entries: list[tuple] = []
    try:
        for json_path in sorted(root.rglob("*.json")):
            if not json_path.is_file():
                continue
            st = json_path.stat()
            entries.append((str(json_path), st.st_mtime_ns, st.st_size))
    except OSError:
        return None
    return tuple(entries)


def _load_desktop_index(root: Path) -> dict[str, Tuple[dict, Path]]:
    """Map session uuid -> ``(metadata, json_path)`` from the Desktop root.

    Cached per root, revalidated by the stat signature of the metadata
    files (see :data:`_desktop_index_cache`); the scan itself lives in
    :func:`_scan_desktop_index`.  Callers must treat the returned index as
    immutable — it is shared across calls.  A missing root yields an empty
    index — never an error.
    """
    if not root.is_dir():
        return {}
    key = str(root)
    signature = _desktop_index_signature(root)
    if signature is not None:
        with _desktop_index_cache_lock:
            cached = _desktop_index_cache.get(key)
            if cached is not None and cached[0] == signature:
                return cached[1]
    index = _scan_desktop_index(root)
    if signature is not None:
        with _desktop_index_cache_lock:
            _desktop_index_cache[key] = (signature, index)
    return index


def _scan_desktop_index(root: Path) -> dict[str, Tuple[dict, Path]]:
    """Build the Desktop metadata index by reading every ``*.json`` file.

    Scans ``<root>/**/*.json`` (observed layout is
    ``<device-uuid>/<workspace-uuid>/local_<id>.json`` but the depth is not
    load-bearing).  A file must parse as a JSON *object* carrying a usable
    id to be indexed; anything else is silently skipped.  The key is
    ``cliSessionId`` when present (== the stem of the backing CLI JSONL,
    which makes deduplication a plain dict lookup), else ``sessionId``.
    """
    index: dict[str, Tuple[dict, Path]] = {}
    for json_path in sorted(root.rglob("*.json")):
        if not json_path.is_file():
            continue
        try:
            record = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        uuid = ""
        for key in ("cliSessionId", "sessionId"):
            raw = record.get(key)
            if isinstance(raw, str) and raw.strip():
                uuid = raw.strip()
                break
        if not uuid:
            continue
        index[uuid] = (record, json_path)
    return index


def _desktop_timestamp(record: dict, json_path: Path) -> Optional[datetime]:
    """Best-effort last-activity time from Desktop metadata (UTC).

    ``lastActivityAt``/``createdAt`` are epoch **milliseconds**; file
    mtime is the fallback.
    """
    for key in ("lastActivityAt", "createdAt"):
        raw = record.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            try:
                return datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
    try:
        return datetime.fromtimestamp(
            json_path.stat().st_mtime, tz=timezone.utc
        )
    except OSError:
        return None


def _desktop_extra(record: dict) -> dict:
    """Common ``extra`` payload derived from one Desktop metadata record."""
    extra: dict = {"source_root": "desktop"}
    session_id = record.get("sessionId")
    if isinstance(session_id, str) and session_id:
        extra["desktop_session_id"] = session_id
    cwd = record.get("cwd") or record.get("originCwd")
    if isinstance(cwd, str) and cwd:
        extra["cwd"] = cwd
    # The Desktop-configured model (a session-level setting, less granular
    # than the per-message transcript signal — kept in ``extra``, not
    # merged into ``Session.models``, which stays transcript-evidenced).
    model = record.get("model")
    if isinstance(model, str) and model.strip():
        extra["model"] = model.strip()
    return extra


def _session_from_desktop_meta(
    uuid: str, record: dict, json_path: Path
) -> Optional[Session]:
    """Build a reference-only :class:`Session` from Desktop metadata alone.

    Used for sessions visible ONLY in the Desktop root (the backing CLI
    transcript is gone).  ``path`` points at the metadata JSON — reading
    messages through it yields an empty list (the file holds no
    transcript), which is the honest answer.
    """
    date = _desktop_timestamp(record, json_path)
    if date is None:
        return None
    raw_title = record.get("title")
    title = (
        _normalise_title(raw_title)
        if isinstance(raw_title, str) and raw_title.strip()
        else _resolve_title(None, None, None, json_path)
    )
    if not title:
        return None
    extra = _desktop_extra(record)
    cwd = extra.get("cwd")
    if isinstance(cwd, str) and cwd:
        # Mirror the CLI slug convention (path with separators/dots
        # flattened to dashes) so slug-based grouping stays uniform.
        extra["project_slug"] = re.sub(r"[/.]", "-", cwd)
    return Session(
        uuid=uuid,
        agent=AgentName.CLAUDE,
        title=title,
        date=date,
        path=str(json_path),
        message_count=0,
        parent_uuid=None,
        kind="agent",
        project_dir=cwd if isinstance(cwd, str) and cwd else None,
        launch_surface="claude-desktop",
        extra=extra,
    )


def _enrich_from_desktop(session: Session, record: dict) -> Session:
    """Overlay Desktop metadata onto a CLI-discovered session.

    The Desktop ``title`` wins (it is the title the user sees in the app,
    hence what they will search for); the CLI-derived title is preserved
    as ``extra["cli_title"]``.  ``extra["source_root"]`` flips to
    ``"desktop"`` — the session was driven from the Desktop app even
    though its transcript lives in the CLI root.
    """
    extra = dict(session.extra)
    extra.update(_desktop_extra(record))
    title = session.title
    raw_title = record.get("title")
    if isinstance(raw_title, str) and raw_title.strip():
        desktop_title = _normalise_title(raw_title)
        if desktop_title and desktop_title != session.title:
            extra["cli_title"] = session.title
            title = desktop_title
    # The transcript-derived project_dir wins (it is what actually ran);
    # the Desktop metadata cwd only fills an absent signal.
    project_dir = session.project_dir or extra.get("cwd") or None
    return dataclasses.replace(
        session,
        title=title,
        project_dir=project_dir,
        launch_surface="claude-desktop",
        extra=extra,
    )


def _apply_desktop_overlay(
    sessions: List[Session], desktop_root: Path
) -> List[Session]:
    """Merge the Desktop metadata index into a CLI session list.

    Deduplication key is the session uuid (Desktop ``cliSessionId`` ==
    CLI JSONL stem): a uuid present on both sides yields ONE enriched
    session, a Desktop-only uuid appends a reference-only session.
    """
    index = _load_desktop_index(desktop_root)
    if not index:
        return sessions
    by_uuid = {s.uuid: i for i, s in enumerate(sessions)}
    for uuid, (record, json_path) in index.items():
        pos = by_uuid.get(uuid)
        if pos is not None:
            sessions[pos] = _enrich_from_desktop(sessions[pos], record)
            continue
        extra_session = _session_from_desktop_meta(uuid, record, json_path)
        if extra_session is not None:
            sessions.append(extra_session)
    return sessions


def _reconcile_spawn_hierarchy(sessions: List[Session]) -> List[Session]:
    """Rewire depth>1 subagents to their TRUE parent (defect #7-A).

    Every subagent transcript lives in ONE flat ``subagents/`` folder under
    the top-level session, and its ``sessionId`` / wrapper-folder name both
    point at that top-level session — so on their own signals ALL
    subagents, however deeply nested, collapse to the root.  Claude's
    ``.meta.json`` breaks the tie: each child records the ``toolUseId`` of
    the ``Task``/``Agent`` call that spawned it, and that id appears in the
    *spawner's* transcript.  ``_scan_file`` already stashed, per session,
    the child's own ``spawn_tool_use_id`` (``extra``) and the ids it
    emitted (``extra["_emitted_spawn_ids"]``); here we join the two.

    Only a subagent whose ``spawn_depth > 1`` (or, absent depth, whose
    ``spawn_tool_use_id`` resolves to an emitter that is NOT its current
    parent) is rewritten — a depth-1 subagent's top-level parent is already
    correct.  The internal ``_emitted_spawn_ids`` bookkeeping key is
    stripped from every session's ``extra`` on the way out so it never
    leaks downstream.  ``kind``/subagent status is untouched — only
    ``parent_uuid`` is corrected.
    """
    # Map every emitted spawn tool_use_id -> the session uuid that emitted it.
    emitter_of: dict[str, str] = {}
    for s in sessions:
        emitted = s.extra.get("_emitted_spawn_ids")
        if isinstance(emitted, tuple):
            for tuid in emitted:
                if isinstance(tuid, str) and tuid:
                    emitter_of.setdefault(tuid, s.uuid)

    out: List[Session] = []
    for s in sessions:
        extra = dict(s.extra)
        emitted_present = "_emitted_spawn_ids" in extra
        extra.pop("_emitted_spawn_ids", None)
        new_parent = s.parent_uuid
        spawn_tuid = extra.get("spawn_tool_use_id")
        if isinstance(spawn_tuid, str) and spawn_tuid:
            true_parent = emitter_of.get(spawn_tuid)
            # Rewire only when the meta points at a real, different emitter
            # that is not the session itself (self-parent guard).
            if (
                true_parent is not None
                and true_parent != s.uuid
                and true_parent != s.parent_uuid
            ):
                depth = extra.get("spawn_depth")
                if not isinstance(depth, int) or depth > 1:
                    new_parent = true_parent
        if new_parent == s.parent_uuid and not emitted_present:
            out.append(s)
            continue
        out.append(dataclasses.replace(s, parent_uuid=new_parent, extra=extra))
    return out


def list_sessions(
    base_dir: Optional[str] = None, desktop_dir: Optional[str] = None
) -> List[Session]:
    """Return every Claude session visible under the CLI + Desktop roots.

    Sessions are sorted by date (most recent first).  Files that fail
    to parse are silently skipped — Claude JSONL records are noisy
    and one bad line should not break enumeration.  The Desktop metadata
    root (see module docstring) is overlaid under the participation rule
    of :func:`_desktop_scan_enabled`; a missing root contributes nothing.
    """
    root = _resolve_base_dir(base_dir)
    sessions: List[Session] = []
    if root.is_dir():
        seen: set[str] = set()
        # Two discovery passes:
        #  1. ``<slug>/<uuid>.jsonl`` — top-level sessions (and
        #     inline-sidechain files, which live alongside their parent and
        #     are classified by ``_scan_file`` via the ``isSidechain``
        #     marker).
        #  2. ``**/subagents/agent-*.jsonl`` — directory-form subagent
        #     sessions, which the shallow ``*/*.jsonl`` glob never reaches.
        globs = ("*/*.jsonl", "**/subagents/agent-*.jsonl")
        for pattern in globs:
            for jsonl_path in root.glob(pattern):
                if not jsonl_path.is_file():
                    continue
                key = str(jsonl_path)
                if key in seen:
                    continue
                seen.add(key)
                session = _scan_file(jsonl_path, root)
                if session is not None:
                    sessions.append(session)

    # Rewire >1-level spawn chains to their real parent BEFORE the Desktop
    # overlay/sort (needs the full CLI session set to resolve emitters).
    sessions = _reconcile_spawn_hierarchy(sessions)

    if _desktop_scan_enabled(base_dir, desktop_dir):
        sessions = _apply_desktop_overlay(
            sessions, _resolve_desktop_dir(desktop_dir)
        )

    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def _find_session_file(uuid: str, base_dir: Optional[str]) -> Path:
    """Locate the JSONL for ``uuid`` and validate the identifier.

    Raises:
        ValueError: ``uuid`` contains path separators or whitespace.
        FileNotFoundError: no file with this name exists under
            ``base_dir`` (or it is not a regular file).
    """
    if not uuid or "/" in uuid or "\\" in uuid or ".." in uuid:
        raise ValueError(f"Invalid Claude session uuid: {uuid!r}")
    if uuid != uuid.strip() or any(c.isspace() for c in uuid):
        raise ValueError(f"Invalid Claude session uuid: {uuid!r}")

    root = _resolve_base_dir(base_dir)
    for pattern in (f"*/{uuid}.jsonl", f"**/subagents/{uuid}.jsonl"):
        for jsonl_path in root.glob(pattern):
            if jsonl_path.is_file():
                return jsonl_path

    raise FileNotFoundError(
        f"Claude session {uuid!r} not found under {root}"
    )


def _emitter_of_tool_use_id(jsonl_path: Path, tool_use_id: str) -> bool:
    """Whether ``jsonl_path`` emitted a spawn ``tool_use`` with this id."""
    try:
        for record in iter_jsonl_records(jsonl_path):
            if record.get("type") != "assistant":
                continue
            payload = record.get("message")
            if not isinstance(payload, dict):
                continue
            content = payload.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool_use"
                    and part.get("name") in _SPAWN_TOOL_NAMES
                    and part.get("id") == tool_use_id
                ):
                    return True
    except OSError:
        return False
    return False


def _resolve_spawn_parent_single(session: Session, path: Path) -> Session:
    """Correct a single subagent's ``parent_uuid`` via its ``.meta.json``.

    The list-level :func:`_reconcile_spawn_hierarchy` needs the full session
    set; a lone :func:`read_session` does not have it, so it resolves the
    emitter of this subagent's ``spawn_tool_use_id`` by scanning the sibling
    ``subagents/`` transcripts.  Only depth>1 subagents are rewritten (a
    depth-1 subagent's top-level parent is already correct); the self-parent
    and same-parent guards mirror the list pass.  No-op when the session is
    not a subagent, carries no ``spawn_tool_use_id``, or no sibling emits it.
    """
    spawn_tuid = session.extra.get("spawn_tool_use_id")
    if not (isinstance(spawn_tuid, str) and spawn_tuid):
        return session
    depth = session.extra.get("spawn_depth")
    if isinstance(depth, int) and depth <= 1:
        return session
    subagents_dir = path.parent
    if subagents_dir.name != "subagents":
        return session
    for sibling in sorted(subagents_dir.glob("agent-*.jsonl")):
        if sibling == path:
            continue
        if _emitter_of_tool_use_id(sibling, spawn_tuid):
            true_parent = sibling.stem
            if true_parent != session.uuid and true_parent != session.parent_uuid:
                return dataclasses.replace(session, parent_uuid=true_parent)
            return session
    return session


def read_session(
    uuid: str,
    base_dir: Optional[str] = None,
    desktop_dir: Optional[str] = None,
) -> Session:
    """Read and return a single Claude session by ``uuid``.

    The Desktop metadata overlay applies here too (same participation
    rule as :func:`list_sessions`): a CLI-backed session is enriched with
    its Desktop title/``source_root``; a uuid known ONLY to the Desktop
    root resolves to the reference-only metadata session instead of
    raising.

    Raises:
        FileNotFoundError: the session does not exist in either root.
        ValueError: ``uuid`` is malformed.
    """
    desktop_enabled = _desktop_scan_enabled(base_dir, desktop_dir)
    try:
        path = _find_session_file(uuid, base_dir)
    except FileNotFoundError:
        if desktop_enabled:
            index = _load_desktop_index(_resolve_desktop_dir(desktop_dir))
            entry = index.get(uuid)
            if entry is not None:
                session = _session_from_desktop_meta(uuid, *entry)
                if session is not None:
                    return session
        raise
    root = _resolve_base_dir(base_dir)
    session = _scan_file(path, root)
    if session is None:
        raise FileNotFoundError(
            f"Claude session {uuid!r} at {path} yielded no parseable data"
        )
    # Depth>1 spawn parent (defect #7-A): a single read has no session list
    # to join against, so resolve the emitter of this subagent's
    # ``spawn_tool_use_id`` directly against its sibling ``subagents/``
    # transcripts.  Same rewrite rule as the list-level pass.
    session = _resolve_spawn_parent_single(session, path)
    # ``_emitted_spawn_ids`` is internal bookkeeping; never surface it.
    if "_emitted_spawn_ids" in session.extra:
        cleaned = dict(session.extra)
        cleaned.pop("_emitted_spawn_ids", None)
        session = dataclasses.replace(session, extra=cleaned)
    if desktop_enabled:
        index = _load_desktop_index(_resolve_desktop_dir(desktop_dir))
        entry = index.get(uuid)
        if entry is not None:
            session = _enrich_from_desktop(session, entry[0])
    return session


def _parse_jsonl_line(line: str) -> Optional[Message]:
    """Parse one Claude JSONL line into a :class:`Message`, or skip it.

    Returns ``None`` for blank lines, malformed JSON, non-dict records,
    and records whose ``type`` is not ``"user"`` or ``"assistant"``.
    Assistant records yield ``text`` (from ``text`` blocks),
    ``thinking`` (from ``thinking`` blocks) and ``tool_use`` entries
    (from ``tool_use`` blocks), plus a normalized ``tokens`` block when
    the record carries ``message.usage``.  User records yield
    ``text`` plus ``tool_result`` entries for any ``tool_result`` blocks
    they carry (Claude embeds tool results in user-role records); each
    result carries ``is_error`` derived from the block's ``is_error`` flag
    with two format fallbacks (see :func:`_derive_tool_result_error`).
    """
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        # RecursionError: a pathologically nested blob — skipped like any
        # other unparseable line (see ``_common._parse_jsonl_line_str``).
        return None
    if not isinstance(record, dict):
        return None
    return _message_from_record(record)


_TOOL_USE_ERROR_MARK = "<tool_use_error>"


def _tool_result_content_starts_with(result_content, prefix: str) -> bool:
    """Whether a raw ``tool_result`` content payload starts with ``prefix``.

    Claude encodes the content of a ``tool_result`` block either as a plain
    string or as a list of typed blocks (``{"type": "text", "text": ...}``).
    Both encodings are checked against the RAW (unredacted) content: a
    leading-string match on the string form, or on the first ``text`` block's
    ``text`` when the content is a list.  Any other shape yields ``False``.
    """
    if isinstance(result_content, str):
        return result_content.startswith(prefix)
    if isinstance(result_content, list):
        for piece in result_content:
            if isinstance(piece, dict) and piece.get("type") == "text":
                text = piece.get("text", "")
                return isinstance(text, str) and text.startswith(prefix)
    return False


def _derive_tool_result_error(
    part: dict, tool_use_result: object
) -> bool:
    """Resolve a ``tool_result`` block's error state, format-derive included.

    Priority (an EXPLICIT flag always wins — a real ``is_error`` value is
    never overridden by a format signal, so ``True`` stays ``True`` and an
    explicit ``False`` stays ``False``):

    1. the block's own ``is_error`` flag, when present (bool-coerced);
    2. else DERIVE ``True`` from either Claude error-format signal:
       * the RAW ``tool_result`` content starts with ``<tool_use_error>``
         (string form, or the first ``text`` block of the list form), OR
       * the record's top-level ``toolUseResult`` is a string starting with
         ``"Error:"`` (Claude writes failed calls this way *without* setting
         the per-block ``is_error`` flag — the defect this closes);
    3. else ``False`` (no signal — honest absence).

    Matching is on raw content (before any redaction pass) so a scrubbed
    ``[redacted]`` prefix can never mask a real failure.
    """
    explicit = part.get("is_error")
    if explicit is not None:
        return bool(explicit)
    if _tool_result_content_starts_with(
        part.get("content", ""), _TOOL_USE_ERROR_MARK
    ):
        return True
    if isinstance(tool_use_result, str) and tool_use_result.startswith("Error:"):
        return True
    return False


# Claude writes ``model: "<synthetic>"`` on locally-generated assistant
# records (interrupt notices, API-error stubs) — a placeholder, not a model.
_SYNTHETIC_MODEL = "<synthetic>"


def _record_model(payload: dict) -> Optional[str]:
    """Return the assistant record's ``message.model``, or ``None``.

    The ``<synthetic>`` placeholder marks a locally-generated record (no
    API call behind it) and is NOT a model — mapped to ``None``, so the
    model dimension never counts synthetic stubs as model output.
    """
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    model = model.strip()
    if model == _SYNTHETIC_MODEL:
        return None
    return model


#: Claude's ``usage`` wire names → the field names ai_r.tokens speaks.
_SUBAGENT_USAGE_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_write",
    "cache_read_input_tokens": "cache_read",
}

#: Mirror of :data:`ai_r.tokens.TOKEN_FIELDS`.  Restated rather than imported:
#: ``ai_r.tokens`` imports ``ai_r.parsers``, so importing it here would close a
#: cycle.  ``test_subagent_token_block_matches_tokens_ssot`` fails if the two
#: ever drift apart.
_TOKEN_FIELDS = (
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
    "total",
)


def _subagent_sidecar(tool_use_result: Any) -> Optional[dict]:
    """Normalise a ``Task`` call's record-level ``toolUseResult``, or ``None``.

    When Claude Code finishes a subagent it records what the child actually
    cost — ``resolvedModel`` (which can differ from the parent's model, since a
    persona may pin a cheaper tier), the billed ``usage``, wall time, tool
    count, and status.  Ordinary tools carry a ``toolUseResult`` too (commonly
    a plain string), so the shape is checked rather than assumed: without a
    ``resolvedModel`` or ``agentType`` this is not a subagent payload and is
    left alone.

    ``tokens`` reports EXACT billed usage (``source="exact"``), never an
    estimate, and always carries the FULL :data:`ai_r.tokens.TOKEN_FIELDS`
    shape (``None`` for what the harness did not record) so a consumer can
    index it like any other token block instead of guarding every key.
    ``total`` is the harness's own ``totalTokens`` where present — it is the
    figure the harness bills — and falls back to the sum of the components.
    """
    if not isinstance(tool_use_result, dict):
        return None
    model = tool_use_result.get("resolvedModel")
    agent_type = tool_use_result.get("agentType")
    if not isinstance(model, str) and not isinstance(agent_type, str):
        return None

    def _int(value: Any) -> Optional[int]:
        # JSON ``true`` is an int in Python — a bool is not a token count.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    usage = tool_use_result.get("usage")
    counted: dict[str, int] = {}
    if isinstance(usage, dict):
        for wire, field_name in _SUBAGENT_USAGE_FIELDS.items():
            value = _int(usage.get(wire))
            if value is not None:
                counted[field_name] = value
    total = _int(tool_use_result.get("totalTokens"))

    tokens: Optional[dict[str, Any]] = None
    if counted or total is not None:
        tokens = {field: counted.get(field) for field in _TOKEN_FIELDS}
        tokens["total"] = (
            total if total is not None else sum(counted.values())
        )
        tokens["source"] = "exact"

    sidecar: dict[str, Any] = {}
    if isinstance(agent_type, str) and agent_type:
        sidecar["agent_type"] = agent_type
    if isinstance(model, str) and model:
        sidecar["model"] = model
    status = tool_use_result.get("status")
    if isinstance(status, str) and status:
        sidecar["status"] = status
    duration = _int(tool_use_result.get("totalDurationMs"))
    if duration is not None:
        sidecar["duration_ms"] = duration
    tool_uses = _int(tool_use_result.get("totalToolUseCount"))
    if tool_uses is not None:
        sidecar["tool_uses"] = tool_uses
    if tokens is not None:
        sidecar["tokens"] = tokens
    return sidecar or None


def _message_from_record(record: dict) -> Optional[Message]:
    """Build a :class:`Message` from a parsed Claude record, or skip it.

    The record→Message half of :func:`_parse_jsonl_line`, factored out so
    the generator-driven extraction loop can reuse it without a redundant
    ``json.loads``.  Returns ``None`` for records whose ``type`` is not
    ``"user"``/``"assistant"``.
    """
    rec_type = record.get("type")
    if rec_type not in ("user", "assistant"):
        return None
    payload = record.get("message") or {}
    ts = _parse_iso_timestamp(record.get("timestamp", ""))
    if not isinstance(payload, dict):
        return None
    content = payload.get("content", "")
    text_chunks: List[str] = []
    thinking_chunks: List[str] = []
    tool_use: List[dict] = []
    tool_result: List[dict] = []
    user_refs: List[dict] = []
    # ``toolUseResult`` is RECORD-level while ``content`` may hold several
    # ``tool_result`` parts.  With more than one, the sidecar cannot be
    # attributed to a particular call — drop it rather than bill the wrong
    # subagent.  (``_derive_tool_result_error`` is unaffected: an error string
    # is safe to apply per-part, a cost figure is not.)
    result_part_count = (
        sum(
            1
            for p in content
            if isinstance(p, dict) and p.get("type") == "tool_result"
        )
        if isinstance(content, list)
        else 0
    )
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "image" and rec_type == "user":
                # An image the USER attached in their turn (a distinct
                # content block, not prose).  Claude stores it inline as
                # ``source.type == "base64"`` — the raw bytes only, with no
                # filename/path — so ``target`` is honestly ``None`` (never
                # fabricated).  Assistant records carry no such user image,
                # so this is gated on the user role.
                user_refs.append(make_user_ref("image", None, "structured"))
            elif part_type == "text":
                text = part.get("text", "")
                if isinstance(text, str) and text:
                    text_chunks.append(text)
            elif part_type == "thinking":
                # Extended-thinking block: the reasoning plaintext lives in
                # the ``thinking`` key.  ``redacted_thinking`` blocks carry
                # only an encrypted ``data`` blob (no plaintext) and are
                # intentionally NOT matched here — absence is honest.
                thought = part.get("thinking", "")
                if isinstance(thought, str) and thought:
                    thinking_chunks.append(thought)
            elif part_type == "tool_use":
                name = part.get("name", "")
                raw_input = part.get("input", "")
                if isinstance(raw_input, str):
                    input_str = raw_input
                else:
                    try:
                        input_str = json.dumps(
                            raw_input, ensure_ascii=False
                        )
                    except (TypeError, ValueError):
                        input_str = str(raw_input)
                entry = {"name": name, "input": input_str}
                # Carry the call id (public, survives scrubbing) so the event
                # layer can correlate this call with its tool_result and
                # surface success/error on the tool_call event.
                tu_id = part.get("id")
                if isinstance(tu_id, str) and tu_id:
                    entry["tool_use_id"] = tu_id
                # Carry the AskUserQuestion id + structured questions so a
                # later pass can pair them with the user's chosen answer
                # (the answer text lives only in the matching tool_result).
                if name == "AskUserQuestion" and isinstance(raw_input, dict):
                    questions = raw_input.get("questions")
                    if isinstance(questions, list):
                        entry["_ask_id"] = part.get("id", "")
                        entry["_ask_questions"] = questions
                tool_use.append(entry)
            elif part_type == "tool_result":
                result_content = part.get("content", "")
                if isinstance(result_content, list):
                    pieces: List[str] = []
                    for piece in result_content:
                        if isinstance(piece, dict):
                            t = piece.get("text", "")
                            if isinstance(t, str) and t:
                                pieces.append(t)
                    result_str = "\n".join(pieces)
                elif isinstance(result_content, str):
                    result_str = result_content
                else:
                    result_str = ""
                # is_error: explicit block flag wins; else derive from the
                # Claude error-format signals (``<tool_use_error>`` content
                # prefix, or a record-level ``toolUseResult: "Error: …"``),
                # so failures recorded WITHOUT the flag are not lost.
                is_error = _derive_tool_result_error(
                    part, record.get("toolUseResult")
                )
                result_entry = {
                    "content": result_str,
                    "is_error": is_error,
                }
                tuid = part.get("tool_use_id")
                if isinstance(tuid, str) and tuid:
                    # Public id (no leading underscore): survives scrubbing so
                    # the event layer can correlate result↔call.  The qa-pair
                    # linker keys off this same field.
                    result_entry["tool_use_id"] = tuid
                if result_part_count == 1:
                    sidecar = _subagent_sidecar(record.get("toolUseResult"))
                    if sidecar is not None:
                        result_entry["subagent"] = sidecar
                tool_result.append(result_entry)
    elif isinstance(content, str):
        text_chunks.append(content)
    # Per-record exact usage (assistant records only).  A streamed API
    # call writes ONE JSONL record per content block, ALL carrying the
    # same (message.id, requestId) and identical usage numbers — and the
    # first record may be thinking-only, which downstream projections
    # drop.  So the block is attached to EVERY record of the call plus an
    # internal ``_call`` key; consumers dedup by ``_call`` and emit the
    # block once per API call, on whichever record survives their view.
    tokens: Optional[dict] = None
    if rec_type == "assistant":
        block = _usage_block(payload)
        if block is not None:
            msg_id = payload.get("id")
            request_id = record.get("requestId")
            tokens = {
                **block,
                "_call": "{}|{}".format(
                    msg_id if isinstance(msg_id, str) else "",
                    request_id if isinstance(request_id, str) else "",
                ),
            }
    return Message(
        role=rec_type,
        text="\n".join(text_chunks),
        tool_use=tuple(tool_use),
        tool_result=tuple(tool_result),
        timestamp=ts,
        thinking="\n".join(thinking_chunks),
        tokens=tokens,
        model=_record_model(payload) if rec_type == "assistant" else None,
        user_refs=tuple(user_refs),
    )


def _extract_messages_from_jsonl(path: Path) -> List[Message]:
    """Read a Claude JSONL file into structured :class:`Message` objects.

    Lines that are not valid JSON or not ``user``/``assistant`` records
    are silently skipped.  An :class:`OSError` reading the file returns
    whatever was collected so far.
    """
    messages: List[Message] = []
    for record in iter_jsonl_records(path):
        msg = _message_from_record(record)
        if msg is not None:
            messages.append(msg)
    return fold_orphan_thinking(_link_ask_user_questions(messages))


def _link_ask_user_questions(messages: List[Message]) -> List[Message]:
    """Pair ``AskUserQuestion`` calls with the user's answers.

    Claude records an interactive question as an assistant ``tool_use``
    (name ``AskUserQuestion`` carrying the structured ``questions``) and
    the user's reply as a ``tool_result`` in a following user-role
    record.  The chosen-answer text lives ONLY in that result string
    (``"question"="answer", ...``), so the pairing must join the two by
    ``tool_use_id`` (a public field kept on both the ``tool_use`` call and
    the ``tool_result``).

    Returns a new list where every answer-bearing message gains a
    populated :attr:`~ai_r.parsers.models.Message.qa` tuple; internal
    linkage keys (``_ask_id`` / ``_ask_questions``) are stripped from the
    surfaced ``tool_use`` entries so they never leak downstream.  The
    public ``tool_use_id`` is retained (the event layer correlates on it).
    Messages without an answered question are returned unchanged.
    """
    # Map AskUserQuestion tool_use_id -> its structured questions list.
    ask_by_id: dict[str, list] = {}
    for msg in messages:
        for tu in msg.tool_use:
            if not isinstance(tu, dict):
                continue
            ask_id = tu.get("_ask_id")
            questions = tu.get("_ask_questions")
            if isinstance(ask_id, str) and ask_id and isinstance(questions, list):
                ask_by_id[ask_id] = questions

    def _scrub_tool_use(entries: Tuple[dict, ...]) -> Tuple[dict, ...]:
        return tuple(
            {k: v for k, v in e.items() if not k.startswith("_")}
            if isinstance(e, dict) else e
            for e in entries
        )

    def _scrub_tool_result(entries: Tuple[dict, ...]) -> Tuple[dict, ...]:
        return tuple(
            {k: v for k, v in e.items() if not k.startswith("_")}
            if isinstance(e, dict) else e
            for e in entries
        )

    out: List[Message] = []
    for msg in messages:
        qa: List[dict] = []
        for tr in msg.tool_result:
            if not isinstance(tr, dict):
                continue
            tuid = tr.get("tool_use_id")
            if not (isinstance(tuid, str) and tuid in ask_by_id):
                continue
            questions = ask_by_id[tuid]
            pairs = _qa_pairs_from_result_text(tr.get("content", ""))
            # Build a question-text -> options lookup so each parsed
            # answer pair can be enriched with the options that were
            # offered (the result string carries text only).
            opts_by_q: dict[str, Tuple[str, ...]] = {}
            for q in questions:
                if isinstance(q, dict):
                    qtext = q.get("question")
                    if isinstance(qtext, str):
                        opts_by_q[qtext.strip()] = _qa_options_from_question(q)
            for q_text, answer in pairs:
                qa.append(
                    _qa_entry(q_text, opts_by_q.get(q_text, ()), answer)
                )

        needs_scrub = any(
            isinstance(e, dict) and any(k.startswith("_") for k in e)
            for e in (*msg.tool_use, *msg.tool_result)
        )
        if not qa and not needs_scrub:
            out.append(msg)
            continue
        out.append(
            Message(
                role=msg.role,
                text=msg.text,
                tool_use=_scrub_tool_use(msg.tool_use),
                tool_result=_scrub_tool_result(msg.tool_result),
                timestamp=msg.timestamp,
                qa=tuple(qa),
                # Reconstruction MUST carry every remaining field —
                # dropping ``thinking``/``tokens``/``model`` here would
                # silently lose them on any qa-bearing message.
                thinking=msg.thinking,
                tokens=msg.tokens,
                model=msg.model,
            )
        )
    return out


def read_messages(
    uuid: str,
    base_dir: Optional[str] = None,
    desktop_dir: Optional[str] = None,
) -> List[Message]:
    """Return the full message list for a Claude session.

    Reuses :func:`read_session` for path resolution — and threads
    ``desktop_dir`` through it so this call resolves EVERY session
    :func:`list_sessions` surfaced under the same roots (defect #7-C: a
    Desktop-ghost was listable but ``read_messages`` — lacking the
    ``desktop_dir`` argument — 404'd, because passing only ``base_dir``
    disables the Desktop overlay).  A Desktop-only session carries no
    transcript, so its message list is legitimately empty (its ``path`` is
    the metadata JSON, which yields no ``user``/``assistant`` records) —
    an honest empty answer, never a crash.  Tool calls and tool results are
    preserved on the returned :class:`Message` objects.

    Raises:
        FileNotFoundError: the session does not exist in either root.
        ValueError: ``uuid`` is malformed.
    """
    session = read_session(uuid, base_dir, desktop_dir)
    return _extract_messages_from_jsonl(Path(session.path))


# ``message.usage`` key → normalized token-usage field (F3.3).
_USAGE_FIELD_MAP: Tuple[Tuple[str, str], ...] = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("cache_write", "cache_creation_input_tokens"),
)


def _usage_block(message: dict) -> Optional[dict]:
    """Normalize a record's ``message.usage`` into the F3.3 token block.

    Returns ``{"input", "output", "reasoning", "cache_read",
    "cache_write", "total"}`` with non-int counters defaulting to ``0``
    (``reasoning`` is always ``None`` — Claude records no reasoning
    breakdown), or ``None`` when the message carries no ``usage`` dict.
    Shared by :func:`read_token_usage` (session totals, the SSOT) and the
    per-message ``Message.tokens`` attachment in ``_message_from_record``
    so the two can never drift.
    """
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    block: dict = {}
    for field, usage_key in _USAGE_FIELD_MAP:
        val = usage.get(usage_key)
        block[field] = (
            val if isinstance(val, int) and not isinstance(val, bool) else 0
        )
    block["reasoning"] = None
    block["total"] = (
        block["input"] + block["output"]
        + block["cache_read"] + block["cache_write"]
    )
    return block


def read_token_usage(
    uuid: str, base_dir: Optional[str] = None
) -> Optional[dict]:
    """Return the session's recorded token usage, or ``None`` without signal.

    Claude CLI transcripts record a per-API-call ``message.usage`` block on
    every assistant JSONL record.  A streamed response writes ONE record per
    content block, all sharing the same ``message.id`` / ``requestId`` and
    the same usage numbers — so calls are **deduplicated** by
    ``(message.id, requestId)`` before summing (an id-less record is counted
    as its own call).

    Normalized fields (format-native semantics): ``input`` =
    ``input_tokens`` (uncached), ``output`` = ``output_tokens``,
    ``cache_read`` / ``cache_write`` = the prompt-cache read/creation
    counts; ``reasoning`` has no Claude breakdown → ``None``.  ``total`` is
    the sum of the four counters.  Returns ``None`` when no record carries a
    usage block (e.g. a Desktop reference-only session raises
    ``FileNotFoundError`` upstream) or the sum is zero — absence is honest.

    Raises:
        FileNotFoundError: the session does not exist (CLI root).
        ValueError: ``uuid`` is malformed.
    """
    path = _find_session_file(uuid, base_dir)
    totals = {field: 0 for field, _ in _USAGE_FIELD_MAP}
    seen_calls: set[Tuple[str, object]] = set()
    found = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    # Same skip-the-line contract as the shared reader: a
                    # malformed OR pathologically nested record is dropped;
                    # the healthy records around it still count.
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                block = _usage_block(message)
                if block is None:
                    continue
                msg_id = message.get("id")
                if isinstance(msg_id, str) and msg_id:
                    key = (msg_id, record.get("requestId"))
                    if key in seen_calls:
                        continue
                    seen_calls.add(key)
                for field in totals:
                    totals[field] += block[field]
                found = True
    except OSError:
        return None
    total = sum(totals.values())
    if not found or total <= 0:
        return None
    return {
        "input": totals["input"],
        "output": totals["output"],
        "reasoning": None,
        "cache_read": totals["cache_read"],
        "cache_write": totals["cache_write"],
        "total": total,
    }


def get_session_size(uuid: str, base_dir: Optional[str] = None) -> int:
    """Return the on-disk byte size of the JSONL file backing ``uuid``.

    Useful for incremental readers: a caller that knows the
    ``new_offset`` returned by :func:`read_session_incremental` can poll
    this to decide whether the agent has appended more data.  Returns
    ``0`` if the file's size cannot be determined.
    """
    path = _find_session_file(uuid, base_dir)
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_session_incremental(
    uuid: str,
    from_offset: int = 0,
    base_dir: Optional[str] = None,
) -> Tuple[List[Message], int]:
    """Read Claude-session messages from ``from_offset`` to end of file.

    Opens the JSONL file in binary mode, seeks to ``from_offset``, and
    parses every line that follows.  Returns ``(messages, new_offset)``
    where ``new_offset`` is the byte position immediately after the last
    byte read — pass it back in on the next call to fetch only the
    delta.

    An :class:`OSError` while reading returns whatever messages were
    collected up to the failure point along with the current offset.
    ``FileNotFoundError`` from path resolution still propagates.
    """
    path = _find_session_file(uuid, base_dir)
    messages: List[Message] = []
    new_offset = max(from_offset, 0)
    try:
        with path.open("rb") as fh:
            fh.seek(new_offset)
            for raw_line in fh:
                msg = _parse_jsonl_line(
                    raw_line.decode("utf-8", errors="replace")
                )
                if msg is not None:
                    messages.append(msg)
            new_offset = fh.tell()
    except OSError:
        return messages, new_offset
    return messages, new_offset


def search(query: str, base_dir: Optional[str] = None) -> List[Session]:
    """Case-insensitive substring search across Claude session titles."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [
        session
        for session in list_sessions(base_dir)
        if needle in session.title.lower()
    ]


def session_exists(
    uuid: str,
    base_dir: Optional[str] = None,
    desktop_dir: Optional[str] = None,
) -> bool:
    """Return ``True`` if a Claude session with this uuid is on disk.

    Checks the CLI transcript root first, then (under the participation
    rule) the Desktop metadata root — a Desktop-only session exists too.
    """
    if not uuid or "/" in uuid or "\\" in uuid or ".." in uuid:
        return False
    try:
        _find_session_file(uuid, base_dir)
        return True
    except ValueError:
        return False
    except FileNotFoundError:
        pass
    if _desktop_scan_enabled(base_dir, desktop_dir):
        index = _load_desktop_index(_resolve_desktop_dir(desktop_dir))
        return uuid in index
    return False
