# Parser coverage & known limitations

This file documents which agent parsers have been live-validated
against real session data on a developer box, and the known gaps.
For the "how to add a new parser" guide, see the second half of
this file.

## Live-validation status

| Agent        | Sessions found | Messages non-empty? | Notes                                  |
|--------------|----------------|---------------------|----------------------------------------|
| claude       | yes            | yes                 | Fully working.                         |
| codex        | yes            | yes                 | Fully working.                         |
| opencode     | yes            | yes                 | Reads message bodies from `part` table.|
| pi           | yes            | yes (sparse)        | Working; many system/meta-only rows.   |
| antigravity  | optional       | optional            | Real-data smoke skips when absent.     |

Status confirmed 2026-06-21 via `ai-r list` / `ai-r read
--messages` against real session stores.

### Antigravity (optional real-data smoke)

Antigravity correctness is covered by fixture unit tests
([test_antigravity.py](../tests/test_parsers/test_antigravity.py) plus the
`fake_antigravity_brain` fixture in [conftest.py](../tests/conftest.py)).
When a host has a real `~/.gemini/antigravity*/brain` tree, the same test
module runs read-only smoke coverage against one parseable real brain.

### OpenCode (message bodies in `part` table)

OpenCode stores message text/tools in a separate `part` table linked
to `message` by `part.message_id` (ordered by `time_created`);
`message.data` holds only metadata (role/time/agent/model).  The
parser joins `message` to `part` and assembles each `Message`:

- `text`        = concatenation of `text` + `reasoning` parts (in order).
- `tool_use`     = `{name, input}` per `tool` part (`state.input`).
- `tool_result`  = `{content}` per `tool` part with `state.output`
                   (error/running tools omit results).
- `step-start`/`step-finish`/`file`/`patch` — boundary/binary markers, skipped.

Live-validated on a 106-message session: 82 messages with non-empty
text, 87 with tool calls, 83 with tool results (previously 0/106 —
old parser read only `message.data`).

## Search behaviour

`search_sessions` (MCP) and `ai-r search` (CLI) are the public
search entry points. The MCP tool exposes three knobs that
backward-compat callers can leave at their defaults:

- `scope="title"` (default) — historical title-substring behaviour.
- `scope="body"` — full-text search over message text, `tool_use[*].input`
  and `tool_result[*].content`. Useful for finding references buried in
  Bash/file invocations. Each candidate session's `read_messages(uuid)`
  is invoked, so the first run on a large vault can be slow.
- `scope="all"` — title OR body.

The query string supports bare words, quoted phrases (`"exact phrase"`)
and Google-style negative prefixes (`-claude` always excludes).
Operator modes are `AND` (default), `OR`, and `NOT`. When a body
match is found, the result carries a `snippet` field (up to 200
chars) so the caller can show "what was found" without a second
`read_session` round-trip. See [README.md § Search operators](../README.md#search-operators)
for the full table.

---

# Adding a new agent parser

A parser turns an agent's session storage into `Session` objects.
This guide walks through adding support for a new agent end-to-end.
We'll use a hypothetical `gemini-cli` (which Antigravity's
sibling app would need) as a worked example.

## Overview: three steps

1. Add a value to `AgentName`.
2. Implement a parser module under `src/ai_r/parsers/`.
3. Re-export the module from `src/ai_r/parsers/__init__.py`.
4. Add tests with fixtures.

Total: ~150 lines of code + tests.

## Step 1 — extend `AgentName`

`src/ai_r/parsers/models.py`:

```python
class AgentName(str, Enum):
    CLAUDE = "CLAUDE"
    CODEX = "CODEX"
    OPENCODE = "OPENCODE"
    ANTIGRAVITY = "ANTIGRAVITY"
    GEMINI_CLI = "GEMINI_CLI"   # new
```

The string value is what gets serialised into MCP and CLI output —
keep it UPPER_SNAKE.

## Step 2 — implement the parser

Create `src/ai_r/parsers/gemini_cli.py`. Every parser must
export exactly four functions:

```python
"""Gemini CLI session parser.

Layout (hypothetical):
    ~/.gemini/gemini-cli/sessions/<uuid>/messages.jsonl
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .models import AgentName, Session

__all__ = ["list_sessions", "read_session", "search", "session_exists"]


def _home() -> Path:
    import os
    base = os.environ.get("AI_R_HOME") or str(Path.home())
    return Path(base) / ".gemini" / "gemini-cli" / "sessions"


def list_sessions(base_dir: str | None = None) -> list[Session]:
    root = Path(base_dir) if base_dir else _home()
    if not root.is_dir():
        return []
    out: list[Session] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        messages = entry / "messages.jsonl"
        if not messages.is_file():
            continue
        out.append(_build_session(entry, messages))
    return out


def read_session(uuid: str, base_dir: str | None = None) -> Session:
    root = Path(base_dir) if base_dir else _home()
    entry = root / uuid
    messages = entry / "messages.jsonl"
    if not messages.is_file():
        raise FileNotFoundError(f"gemini-cli session not found: {uuid}")
    return _build_session(entry, messages)


def search(query: str, base_dir: str | None = None) -> list[Session]:
    needle = (query or "").lower()
    return [s for s in list_sessions(base_dir) if needle in s.title.lower()]


def session_exists(uuid: str, base_dir: str | None = None) -> bool:
    root = Path(base_dir) if base_dir else _home()
    return (root / uuid / "messages.jsonl").is_file()


def _build_session(entry: Path, messages: Path) -> Session:
    # Custom: read the first user line for the title, mtime for date
    title = ""
    count = 0
    with messages.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            count += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("role") == "user" and not title:
                title = (rec.get("content") or "")[:100].replace("\n", " ")
    stat = messages.stat()
    return Session(
        uuid=entry.name,
        agent=AgentName.GEMINI_CLI,
        title=title or entry.name,
        date=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        path=str(messages),
        message_count=count,
    )
```

### Rules of thumb

- **Never raise on a missing root.** `list_sessions()` returns `[]`
  when the storage path doesn't exist. Only `read_session()` raises
  `FileNotFoundError` (and only for a specific uuid).
- **Use `AI_R_HOME` for tests.** The package honours this env
  var; set it in `conftest.py` to point at a fixture root.
- **Truncate titles to 100 chars** and collapse newlines to spaces
  — the table formatter relies on it.
- **Prefer in-file timestamps** for `date`; fall back to mtime. The
  `Session.date` field is the *last activity* — the field that
  matters for sorting "recent" sessions.
- **Don't import private helpers from other parsers.** Copy small
  utilities; share only the data models.

## Step 3 — re-export

`src/ai_r/parsers/__init__.py`:

```python
from . import antigravity, claude, codex, gemini_cli, opencode
...
```

…and update the module docstring with the new layout.

## Step 4 — register in the CLI/MCP

The CLI and MCP both dispatch via a `_PARSERS` dict. Find and update:

`src/ai_r/cli.py`:

```python
_PARSERS = {
    AgentName.CLAUDE: claude,
    AgentName.CODEX: codex,
    AgentName.OPENCODE: opencode,
    AgentName.ANTIGRAVITY: antigravity,
    AgentName.GEMINI_CLI: gemini_cli,  # new
}
```

`src/ai_r/mcp_server.py`: same change in its `_PARSERS` dict
and the `_AGENT_NAMES_LOWER` map.

## Step 5 — tests

Create `tests/test_parsers/test_gemini_cli.py`:

```python
import json
import textwrap
from pathlib import Path

import pytest

from ai_r.parsers import AgentName
from ai_r.parsers import gemini_cli


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    root = tmp_path / ".gemini" / "gemini-cli" / "sessions"
    root.mkdir(parents=True)
    (root / "sess-001").mkdir()
    (root / "sess-001" / "messages.jsonl").write_text(textwrap.dedent("""\
        {"role": "user", "content": "hello world"}
        {"role": "assistant", "content": "hi"}
    """))
    monkeypatch.setenv("AI_R_HOME", str(tmp_path))
    return root


def test_list(fake_home):
    sessions = gemini_cli.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.agent == AgentName.GEMINI_CLI
    assert s.uuid == "sess-001"
    assert s.title == "hello world"
    assert s.message_count == 2


def test_read_session(fake_home):
    s = gemini_cli.read_session("sess-001")
    assert s.uuid == "sess-001"


def test_missing_raises(fake_home):
    with pytest.raises(FileNotFoundError):
        gemini_cli.read_session("does-not-exist")


def test_search(fake_home):
    matches = gemini_cli.search("hello")
    assert len(matches) == 1


def test_session_exists(fake_home):
    assert gemini_cli.session_exists("sess-001")
    assert not gemini_cli.session_exists("nope")
```

Use the existing `tests/conftest.py` helpers if it provides
`tmp_ai_r_home` — keep fixtures consistent.

## Step 6 — update docs

- Add a row to the table in [README.md](../README.md#supported-agents).
- Update [CONTEXT.md](../CONTEXT.md#module-map-where-to-look-first)
  if the parser has a non-obvious storage layout.
- Mention the new agent in [CHANGELOG.md](../CHANGELOG.md) under
  `Added` (next release).

## Review checklist

- [ ] Parser is pure (no network, no global state)
- [ ] `list_sessions()` returns `[]` on missing root, never raises
- [ ] `read_session()` raises `FileNotFoundError` for unknown uuid
- [ ] All four functions honour `base_dir` and `$AI_R_HOME`
- [ ] Tests cover: happy path, missing root, missing uuid, search
- [ ] Title truncated to 100 chars, newlines collapsed
- [ ] `AgentName` extended; `parsers/__init__.py` updated
- [ ] `cli.py`, `mcp_server.py` `_PARSERS` updated
- [ ] Coverage stays ≥ 80% (`pytest --cov`)
- [ ] Docs (README, CONTEXT, CHANGELOG) updated
