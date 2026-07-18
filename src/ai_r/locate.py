"""The ``locate`` preset — find a session across every agent by id or title.

Answers "I remember a session — where does it live and how do I read it?" in
ONE call.  The needle is a full uuid, an id prefix (e.g. the 8-hex head), or a
case-insensitive title substring; the answer is where the session lives (path,
agent, project dir, date, size), whether its content is readable locally, and
the ready-to-run commands (``ai-r read …`` + the F2.2 ``resume_command``).

A thin preset over the EXISTING enumeration (project preset rule): candidates
come from each parser's ``list_sessions()`` — the same inventory
``list_sessions``/``search_sessions`` walk — with zero new scanning code; the
algorithm inside is deterministic selection + ranking:

* **match** — the needle prefix-matches the uuid / path stem (id match) OR is
  a case-insensitive substring of the title (title match);
* **rank** — matches are ordered by last activity (mtime) descending, newest
  first; ``limit`` bounds the emitted list (``count`` keeps the full total);
* **zero matches** — an honest empty + closest-title ``suggestions``
  (:func:`difflib.get_close_matches`) + the house empty-result
  ``diagnostics`` — never a fabricated match.

``readable`` is an honest local-content claim: ``True`` when the transcript
holds messages on this machine, ``False`` for a reference-only stub (e.g. a
Claude-Desktop metadata record whose transcript is gone — F1.3).

**``web=True`` (v1, honest scope)** additionally reports web sessions KNOWN
LOCALLY — this machine cannot enumerate claude.ai's cloud store, so only two
local traces are surfaced, each labeled by ``source``:

* ``hook_export`` — materialized hook-export files in the session-watch
  web-sessions directory (``$SW_HOME/web-sessions``, default
  ``~/.session-watch/web-sessions``); these ARE readable local files;
* ``teleport_stub`` — ``~/.claude.json → projects[*].lastSessionId`` ids left
  by the web→CLI teleport flow: the id is known, the transcript is NOT on
  this machine (``content_local: false`` — honest absence, never fabricated).

The fuller source — a per-repo teleport-picker sweep — needs a PTY and is a
documented follow-up, deliberately NOT built here.  A missing dir/file is
skipped (never an error); an unreadable ``~/.claude.json`` degrades to an
honest ``claude_json_error`` note.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ai_r.parsers import PARSERS, Session, iso, target_agents
from ai_r.redact import merge_redaction_counts, redact_text
from ai_r.resume import resume_command

__all__ = ["DEFAULT_LIMIT", "locate"]


DEFAULT_LIMIT: int = 10

# Zero-match title suggestions: how many, and how close (difflib ratio).
_SUGGESTIONS_MAX = 3
_SUGGESTIONS_CUTOFF = 0.5


def _home() -> Path:
    """The scan home — ``AI_R_HOME`` when set (the test seam), else ``~``."""
    env = os.environ.get("AI_R_HOME")
    return Path(env) if env else Path.home()


def _size_bytes(path: Any) -> Optional[int]:
    try:
        return os.path.getsize(str(path))
    except OSError:
        return None


def _sort_ts(session: Session) -> float:
    """Last-activity timestamp for ranking (newest first); undated → 0."""
    date = session.date
    try:
        if date.tzinfo is not None:
            return date.timestamp()
        return date.replace(tzinfo=timezone.utc).timestamp()
    except (OSError, OverflowError, ValueError):
        return 0.0


def _match_kind(session: Session, needle_l: str) -> Optional[str]:
    """``"id"`` / ``"title"`` when ``session`` matches, else ``None``."""
    if session.uuid.lower().startswith(needle_l):
        return "id"
    stem = Path(str(session.path)).stem.lower()
    if stem.startswith(needle_l):
        return "id"
    title = (session.title or "").lower()
    if needle_l in title:
        return "title"
    return None


def _record(session: Session, match: str, redact: bool,
            redactions: Dict[str, int]) -> dict[str, Any]:
    title = session.title
    if redact and isinstance(title, str) and title:
        new_val, counts = redact_text(title)
        if counts and isinstance(new_val, str):
            title = new_val
            merge_redaction_counts(redactions, counts)
    agent_label = session.agent.value.lower()
    return {
        "uuid": session.uuid,
        "agent": agent_label,
        "title": title,
        "date": iso(session.date),
        "path": str(session.path),
        "project_dir": session.project_dir,
        "launch_surface": session.launch_surface,
        "kind": session.kind,
        "size_bytes": _size_bytes(session.path),
        "message_count": session.message_count,
        # Honest local-content claim: a reference-only stub (0 messages —
        # e.g. a Desktop metadata record whose transcript is gone) is NOT
        # readable here even though it is listed.
        "readable": session.message_count > 0,
        "match": match,
        "read_command": (
            f"ai-r read {shlex.quote(session.uuid)} --agent {agent_label}"
        ),
        "resume_command": resume_command(session),
    }


def _web_block(needle_l: str) -> dict[str, Any]:
    """The ``web=True`` v1 block — locally-known web-session traces only."""
    home = _home()
    sw_env = os.environ.get("SW_HOME")
    sw_home = Path(sw_env) if sw_env else home / ".session-watch"
    exports_dir = sw_home / "web-sessions"
    claude_json = home / ".claude.json"

    exports: List[dict[str, Any]] = []
    if exports_dir.is_dir():
        try:
            entries = sorted(exports_dir.iterdir())
        except OSError:
            entries = []
        for f in entries:
            if not f.is_file() or needle_l not in f.name.lower():
                continue
            try:
                stat = f.stat()
                mtime: Optional[str] = iso(
                    datetime.fromtimestamp(stat.st_mtime)
                )
                size: Optional[int] = stat.st_size
            except OSError:
                mtime, size = None, None
            exports.append({
                "id": f.stem,
                "path": str(f),
                "size_bytes": size,
                "mtime": mtime,
                "source": "hook_export",
                "readable": True,
            })
        exports.sort(key=lambda r: (r["mtime"] is None, r["mtime"] or "",
                                    r["path"]), reverse=True)

    stubs: List[dict[str, Any]] = []
    claude_json_error: Optional[str] = None
    if claude_json.is_file():
        try:
            data = json.loads(claude_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
            claude_json_error = (
                "unreadable/malformed — teleport stubs skipped"
            )
        projects = data.get("projects") if isinstance(data, dict) else None
        if isinstance(projects, dict):
            for proj_dir in sorted(projects):
                entry = projects[proj_dir]
                sid = (
                    entry.get("lastSessionId")
                    if isinstance(entry, dict) else None
                )
                if not isinstance(sid, str) or not sid:
                    continue
                if not sid.lower().startswith(needle_l):
                    continue
                stubs.append({
                    "uuid": sid,
                    "project_dir": proj_dir,
                    "source": "teleport_stub",
                    # The id is known locally; the transcript is not.
                    "content_local": False,
                    "note": (
                        "id known from the web→CLI teleport stub; the "
                        "transcript is NOT on this machine"
                    ),
                })

    block: dict[str, Any] = {
        "exports": exports,
        "stubs": stubs,
        "sources": {
            "exports_dir": str(exports_dir),
            "exports_dir_found": exports_dir.is_dir(),
            "claude_json": str(claude_json),
            "claude_json_found": claude_json.is_file(),
        },
        "scope_note": (
            "v1 honest scope: only web sessions KNOWN LOCALLY (hook-export "
            "files + ~/.claude.json teleport stubs). The per-repo teleport "
            "picker is the fuller source — a documented follow-up (needs a "
            "PTY), not guessed here."
        ),
    }
    if claude_json_error:
        block["sources"]["claude_json_error"] = claude_json_error
    return block


def locate(
    needle: str,
    *,
    agent: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    web: bool = False,
    redact: bool = True,
) -> dict[str, Any]:
    """Find a session across all agents by uuid / id-prefix / title substring.

    Args:
        needle: Full uuid, an id prefix (e.g. the 8-hex head), or a
            case-insensitive title substring.  Empty/blank fails loud.
        agent: Optional agent filter (``claude``/``codex``/…); ``None`` = all.
        limit: Max matches emitted (``0`` = no cap, default
            :data:`DEFAULT_LIMIT`).  ``count`` reflects the FULL match set.
        web: ``True`` adds the v1 locally-known web-session block (see
            module docstring — honest scope, nothing fetched).
        redact: ``True`` (default) masks secrets in emitted titles /
            suggestions (F2.1); ``False`` returns raw.

    Returns:
        A dict::

            {
              "matches": [{uuid, agent, title, date, path, project_dir,
                           launch_surface, kind, size_bytes, message_count,
                           readable, match: "id"|"title", read_command,
                           resume_command}, ...],   # mtime desc
              "count": N,                # full match total
              "truncated": bool,
              "suggestions": [...],      # only when count == 0
              "diagnostics": {...},      # only when count == 0
              "web": {...},              # only when web=True
              "redactions": {...}        # only when something was masked
            }

    Raises:
        ValueError: empty ``needle``, unknown ``agent``, negative/non-int
            ``limit``, non-bool ``web``/``redact``.
    """
    if not isinstance(needle, str) or not needle.strip():
        raise ValueError(
            f"needle must be a non-empty uuid/prefix/title string, got {needle!r}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(f"limit must be a non-negative integer, got {limit!r}")
    if not isinstance(web, bool):
        raise ValueError(f"web must be a bool, got {web!r}")
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")

    needle_l = needle.strip().lower()
    redactions: dict[str, int] = {}

    # Candidates: the SAME per-parser inventory list_sessions walks — no new
    # scanning code, one enumeration kept for match + suggestions + diagnostics.
    scanned_sessions: Dict[str, List[Session]] = {}
    matched: List[Tuple[Session, str]] = []
    for agent_name in target_agents(agent):
        parser = PARSERS[agent_name]
        try:
            sessions = parser.list_sessions()
        except (OSError, ValueError):
            sessions = []
        scanned_sessions[agent_name.value.lower()] = sessions
        for session in sessions:
            kind = _match_kind(session, needle_l)
            if kind is not None:
                matched.append((session, kind))

    # Rank: last activity (mtime) descending — newest first; id ties break
    # deterministically by uuid.
    matched.sort(key=lambda pair: (-_sort_ts(pair[0]), pair[0].uuid))

    total = len(matched)
    truncated = False
    if limit and total > limit:
        matched = matched[:limit]
        truncated = True

    matches = [
        _record(session, kind, redact, redactions)
        for session, kind in matched
    ]

    response: dict[str, Any] = {
        "matches": matches,
        "count": total,
        "truncated": truncated,
    }

    if total == 0:
        # Honest empty: closest-title suggestions + the house diagnostics.
        titles: List[str] = []
        seen: set[str] = set()
        for sessions in scanned_sessions.values():
            for session in sessions:
                title = session.title or ""
                if title and title.lower() not in seen:
                    seen.add(title.lower())
                    titles.append(title)
        close = difflib.get_close_matches(
            needle.strip(), titles, n=_SUGGESTIONS_MAX,
            cutoff=_SUGGESTIONS_CUTOFF,
        )
        suggestions: List[str] = []
        for title in close:
            if redact:
                new_val, counts = redact_text(title)
                if counts and isinstance(new_val, str):
                    title = new_val
                    merge_redaction_counts(redactions, counts)
            suggestions.append(title)
        response["suggestions"] = suggestions
        from ai_r.diagnostics import empty_result_diagnostics

        response["diagnostics"] = empty_result_diagnostics(
            agent=agent,
            filters={"needle": needle},
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )

    if web:
        response["web"] = _web_block(needle_l)
    if redactions:
        response["redactions"] = redactions
    return response
