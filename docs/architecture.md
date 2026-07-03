# Architecture

`ai-r` is a 2-layer package. Each layer has exactly one
responsibility and depends only on the layer below it.

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 1: Public API                                          │
│   • ai-r CLI  (src/ai_r/cli.py)                    │
│   • ai-r-mcp  (src/ai_r/mcp_server.py)             │
│   • Python SDK     (importable: ai_r.parsers)           │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│ Layer 2: Core parsers                                        │
│   src/ai_r/parsers/                                     │
│   • claude.py     — JSONL                                    │
│   • codex.py      — JSONL                                    │
│   • opencode.py   — SQLite (with snap/flatpak detection)     │
│   • antigravity.py — brain directory                         │
│   • pi.py          — JSONL session tree                      │
│                                                              │
│   Shared schema: Session, AgentName                          │
└──────────────────────────────────────────────────────────────┘
```

`ai-r` is a **read-only** session reader. There is no access
layer in front of the parsers: any caller that can reach the CLI,
the MCP server, or the Python package can read any session. Treat
the host's session directories as trusted-and-local — the tool does
not gate who may read what. What the reader's *caller* does with
session content is a separate concern: see
[Security — untrusted session content](security.md).

## Layer 1 — Public API

Three entry points:

### `ai-r` (CLI)
Thin wrapper over the parsers. Exits with distinct codes:
- `0` — success
- `1` — usage / argument error
- `3` — `FileNotFoundError` (session missing)

### `ai-r-mcp` (MCP server)
Stdio JSON-RPC. Thirteen tools in three groups:
- 7 classic tools: `list_sessions`, `read_session`, `search_sessions`,
  `find_file_edits`, `find_tool_calls`, `session_stats`, `session_diff`.
- 5 event-core verbs: `query`, `get_body`, `aggregate`, `diff`,
  `detect_current`.
- 1 preset: `plan`.

`list_sessions` and
`read_session` are paginated (`limit`/`offset`, `limit=0` = uncapped)
and report a `truncated` flag when more pages remain. Errors are
returned as dicts (MCP prefers
structured errors to raised exceptions); a missing session returns
`{"error": "not_found", ...}`, an unknown agent or invalid argument
returns `{"error": "invalid_argument", ...}`.

### Python SDK
`ai_r.parsers.*` is importable. See [README.md](../README.md#usage)
for the canonical example.

## Layer 2 — Parsers

Every parser exports the same five functions:

```python
def list_sessions(base_dir: str | None = None) -> list[Session]: ...
def read_session(uuid: str, base_dir: str | None = None) -> Session: ...
def read_messages(uuid: str, base_dir: str | None = None) -> list[Message]: ...
def search(query: str, base_dir: str | None = None) -> list[Session]: ...
def session_exists(uuid: str, base_dir: str | None = None) -> bool: ...
```

`base_dir` is the testing hook — when omitted, parsers honour
`$AI_R_HOME` (treated as the user's `$HOME`) and fall back to
`~`. **This is the only side-effecting test seam**; do not add
others.

Path resolution per agent:

| Agent | Source of truth |
|---|---|
| Claude | `~/.claude/projects/<slug>/<uuid>.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<uuid>.jsonl` |
| OpenCode | `~/.local/share/opencode/opencode.db` (or snap variants) |
| Antigravity | `~/.gemini/antigravity/brain/<uuid>/` |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl` |

The `Session` and `Message` models are shared; see
[`src/ai_r/parsers/models.py`](../src/ai_r/parsers/models.py).

### UUID validation

Every parser validates the requested `uuid` before touching the
filesystem: path separators (`/`, `\`), whitespace, and `..`
(Claude) are rejected with `ValueError`. This keeps `read_session`
scoped to a single session identifier — no path traversal.

## Decisions

### ADR: no access-control layer

ai-r is a local, single-user tool that reads session files owned by
the host user. There is **no access-control / authorization layer by
design**: any caller that can reach the CLI, the SDK, or the MCP stdio
server already operates as the host user with full filesystem access to
those session files, so a caller-authorization gate ("may this caller
read this session") would guard nothing.

Identity ("which session is mine") is an **orthogonal** concern,
handled by `session.py` multi-candidate detection, not by authorization.

The separate content-trust concern (what a reader's caller does with
session text) is covered in [Security](security.md) and is unaffected
by this decision.
