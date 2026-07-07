# Architecture

`ai-r` is a 2-layer package. Each layer has exactly one
responsibility and depends only on the layer below it.

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 1: Public API                                          │
│   • ai-r CLI  (src/ai_r/cli/)                      │
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
Stdio JSON-RPC. Fifteen tools in three groups:
- 7 classic tools: `list_sessions`, `read_session`, `search_sessions`,
  `find_file_edits`, `find_tool_calls`, `session_stats`, `session_diff`.
- 5 event-core verbs: `query`, `get_body`, `aggregate`, `diff`,
  `detect_current`.
- 3 presets: `plan`, `incidents`, `network`.

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

## Event core & cross-cutting modules

Above the parsers, a normalized **event core** (`src/ai_r/events/`) folds
every agent's messages into one agent-neutral `Event` stream
(`user_turn` / `assistant_turn` / `tool_call` (+ `tool_kind`) / `thinking` /
`plan_event`). The five event verbs (`query`, `get_body`, `aggregate`,
`diff`, `detect_current`) and the three presets (`plan`, `incidents`,
`network`) are thin readers over that stream — `query` is the workhorse;
a preset wires a fixed chain of base verbs, never a second engine.

Three cross-cutting modules sit beside the core:

| Module | Responsibility |
|---|---|
| `redact.py` | Secret redaction on every emitting surface (title, message content, intent, qa) as `[REDACTED_<TYPE>]`, with a `redactions` type→count report. On by default. |
| `tokens.py` | Token accounting: `session_tokens` (exact where the agent records usage, a labeled estimate otherwise, honest `source=None` without signal) + `component_tokens` breakdown over the event taxonomy. `rollup_component_tokens` is the SSOT fold for a parent + its spawned children — it drops the parent's double-counted `task` bucket when children are present and reports `total: None` (never a fabricated `0`) when nothing is measurable; both rollup callers (`read_session(include_subagents)` and the CLI) share it. |
| `semantic.py` | Optional relevance re-rank (see ADR below): re-ranks the BM25 top-50 with a local ONNX embedding model. Strictly opt-in, fail-soft to BM25. |
| `serve.py` | MCP transport selection (see ADR below): `stdio` by default, opt-in shared `streamable-http` server with idle self-exit and systemd socket-activation support. Pure predicates (`resolve_transport`, `should_exit_idle`, `systemd_listen_sockets`) + a thin uvicorn runner. |

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

- **Amendment (v0.3.0) — the shared HTTP transport is the one exception.**
  The "no auth guards nothing" reasoning holds only while every caller is
  already the host user (CLI / SDK / stdio). The opt-in `streamable-http`
  transport breaks that premise: it is reachable over a socket, so a
  co-resident user or a browser page (DNS-rebinding) is a caller that is
  *not* the session owner. There the transport **does** carry access
  control — see the http-transport ADR below (SDK DNS-rebinding/Origin
  allowlist, always on for loopback; opt-in bearer token `AI_R_HTTP_TOKEN`,
  **required** — fail-closed — for any non-loopback bind). stdio and local
  callers stay auth-free by design; the exception is scoped to the socket.

### ADR: semantic re-rank as an optional extra

Earlier design deliberately shipped **no semantic embeddings** — relevance
was BM25 only, with no `torch` dependency and no persistent index. v0.3.0
revisits that stance and adds an **opt-in** semantic re-rank; this ADR records
the reversal and its boundaries.

- **What changed.** `sort="semantic"` (in `query` / `search_sessions`)
  re-ranks the BM25 **top-50** candidates with a local multilingual embedding
  model (`multilingual-e5-small`, ONNX via `onnxruntime` — no `torch`). BM25
  stays the primary retriever; the embedder only reorders a shortlist.
- **Why.** Cross-lingual recall: a Russian query over English sessions (and the
  reverse) is exactly where lexical BM25 is weakest. Re-rank buys that without a
  heavyweight stack.
- **Boundaries.** Strictly optional (`pip install "ai-r[semantic]"` + a one-time
  model download). No `torch`, no background daemon, no persistent index.
  Resource-capped: `AI_R_SEMANTIC_THREADS` (default 2) and idle model release
  (`AI_R_SEMANTIC_IDLE_SEC`, default 300 s, ~118 MB freed). The idle release is
  driven by the shared http server's existing idle loop (`serve.py`), not by an
  opportunistic pull on the request path (a request-path release can never free
  a model that is idle *because no request is arriving*, and only forces a
  redundant reload); a `threading.Lock` guards the shared model handle because
  the sync MCP tools run in a worker thread that races that loop. A4 is
  therefore meaningful for the long-lived http transport; stdio is short-lived
  per-agent with no persistent server to reclaim.
- **Fail-soft.** Missing package/model → `{active: false, reason, fallback:
  "bm25"}`; it degrades to plain BM25 order, never crashes.
- **Zero-LLM invariant preserved.** The embedder computes vector similarities;
  it generates no text and makes no model/network API call. The "no generative
  model in the read path" invariant holds — this is retrieval math, not an LLM.

### ADR: plan signals bind by file order, not timestamp

Earlier the `plan` preset ordered a session's plan revisions by **timestamp**
(the order `query` returns `plan_event`s in) and indexed the per-revision
signals (body / steps / version / final) by that ordinal. This ADR records the
reversal to **file (append) order** and why.

- **What broke.** Timestamps in a transcript are not guaranteed monotonic (a
  resumed or clock-skewed session can write a later revision with an *earlier*
  `ts`). When they were non-monotonic, the ts-ordinal pointed `plan()` at a
  *different* revision's body/steps/version, and `plan_feedback` disagreed with
  `get_body` (which already resolved by file order) — a silent
  wrong-revision result, invisible to tests because every fixture was
  monotonic.
- **What changed.** Plan signals and version/final kinds now key on the
  trailing `seq` of the event id (`"{session}:{seq}"`), a monotonic file-order
  index assigned as messages are read — the same order the signal detection
  itself walks. `plan()`, `plan_feedback()` and `get_body()` therefore always
  agree.
- **Boundary.** Public verb signatures are unchanged; this is an internal
  ordering-correctness fix. A non-monotonic-timestamp regression test now
  guards it.

### ADR: shared http transport (one server, not a per-agent stdio swarm)

The MCP server originally ran **stdio only**. Under stdio, every agent — and
every *subagent* — spawns its own `ai-r-mcp` process, each with a cold,
per-process cache, so N concurrent agents re-scan the whole session corpus N
times. On a real multi-agent fan-out this exhausted host RAM (swap thrash →
compositor starvation → graphical artifacts); a session audit measured ten
`ai-r-mcp` instances alive at once, two of them pinned at 20 % CPU. This ADR
records adding an optional shared transport as the fix.

- **What changed.** `AI_R_MCP_TRANSPORT=http` runs a single long-lived
  `streamable-http` server (localhost, default `127.0.0.1:8756`, path `/mcp`)
  that every agent connects to over HTTP instead of spawning its own process.
  One process → one warm cache → the corpus is scanned once, not per agent.
- **Cache must hold the corpus.** The warm-scan win is real only if the
  body-search haystack cache can hold every session; a cap below the corpus
  size makes a full-corpus `scope="body"` search thrash the LRU and re-parse
  every file. Measured on a ~1492-session corpus: at the old 256 cap the
  "warm" repeat was as slow as cold (1x); with the cap above the corpus it is
  ~17× faster (~150 s → ~9 s). The cap default is raised to 2048 and tunable
  via `AI_R_HAYSTACK_CACHE_MAX`; per-entry size stays bounded by
  `_HAYSTACK_CHARS_CAP`. Independent of the cache, the transport's other win —
  N resident processes collapsing to 1 (measured: 4 cold servers ≈ +1.2 GB
  RSS) — holds unconditionally.
- **Idle-off + respawn.** The server self-exits after `AI_R_MCP_IDLE_SEC`
  (default 900 s) with no in-flight and no recent request, and it accepts a
  systemd socket-activation fd (`LISTEN_FDS`/`LISTEN_PID`), so a `.socket` unit
  keeps the listener and respawns the service on the next connection — zero
  resident processes when idle. Idle-exit never fires while a request is in
  flight (active-request counter).
- **Boundaries.** Bind is localhost-only and **fail-closed**: `resolve_host`
  refuses a non-loopback `AI_R_MCP_HOST` unless the operator sets
  `AI_R_MCP_ALLOW_REMOTE=1` to opt in deliberately. `uvicorn` is imported
  lazily and shipped as the optional `ai-r[http]` extra, so stdio users need
  nothing new. The activity wrapper is raw-ASGI (not Starlette
  `BaseHTTPMiddleware`) so it never buffers the long-lived streaming responses
  streamable-http relies on.
- **Transport auth (v0.3.0).** The bind guard is not the only defense — a
  loopback bind is still reachable by any co-resident user and by a browser
  page via DNS-rebinding, and transcripts carry secrets. Two SDK-native
  controls now apply: (1) the `mcp` SDK's DNS-rebinding/Origin protection is
  pinned to the resolved host/port allowlist (always on for the loopback
  default; when `AI_R_MCP_ALLOW_REMOTE=1` the real host:port + http/https
  origins are added, never a blanket `*`); (2) an opt-in bearer token
  (`AI_R_HTTP_TOKEN`, constant-time compared via `hmac.compare_digest`, `401`
  otherwise) — **required, fail-closed, for any non-loopback bind** (remote
  without a token is a hard refusal). This raises the SDK floor to
  `mcp>=1.9.0` (the version that ships `streamable_http_app` +
  `TransportSecuritySettings`).
- **Haystack cache correctness.** The warm-scan cache keys on
  `(agent, uuid, mtime)`; on an mtime change the stale key is now **purged**
  (one live version per session, no dead-key pileup / LRU thrash), and
  eviction is bounded by **both** entry count (`AI_R_HAYSTACK_CACHE_MAX`) and
  total characters (`AI_R_HAYSTACK_CACHE_CHARS_MAX`) so a long-lived shared
  server cannot grow unbounded RSS. A single oversize session stays servable.
- **Back-compat / fail-closed.** `stdio` stays the default — existing sessions
  are unaffected until they opt in, with no mid-session break. An unrecognized
  `AI_R_MCP_TRANSPORT` is a hard error, never a silent fallback to the wrong
  transport.

### ADR: `query` Phase-2/3 facets — `parent`/`group` landed, `kind` removed

- **What changed.** The `query` verb's stubbed forward-compat facets (which
  previously fail-loud-raised on any value) are resolved. `parent` (session
  subtree) and `group` (plan-task) are implemented; `kind` is removed from the
  core `query` and kept in the MCP wrapper **only as a fail-loud tombstone**
  (see below).
- **`parent`** — a session-level filter (like `noise`/`project_dir`, applied in
  `iter_events` before any message is read): keep events of every session that
  is a transitive `parent_uuid` descendant of the given uuid. Closure is built
  per-agent via `ai_r.events.model._descendant_uuids` (`parent_uuid` never
  crosses agents), cycle-safe, and the root session itself is excluded (its own
  events are reachable via `session=<uuid>`). An unknown uuid → honest empty
  result; an empty-string value fails loud.
- **`group`** — an event-level `plan_event` filter applied after collection:
  keep only plan_events whose `task_id` equals the value, reusing the SSOT
  `_assign_plan_kinds` grouping (no second grouper). Non-plan events never match
  a `group` filter; an empty-string value fails loud.
- **Why drop `kind`.** It 100 % duplicated `noise` (`noise="exclude"`≡top-level,
  `noise="only"`≡subagents; `is_noise()` = `kind=="subagent" or parent_uuid`).
  A second facet over the same signal violates the project's DRY rule, so the
  redundant facet was removed rather than implemented as an alias.
- **`kind` tombstone (fail-loud, not silent).** Simply deleting `kind` from the
  MCP wrapper is unsafe: the MCP transport silently drops a truly-unknown
  argument, so a stale client passing `kind="subagent"` would get an
  **unfiltered** result — a silent wrong answer, exactly what the project's
  "never silent" rule forbids. So the wrapper keeps a `kind` parameter that
  returns `invalid_argument` for any value, pointing the caller at `noise`. The
  core `query` (Python API) has no `kind` at all (a direct call raises
  `TypeError`).
- **Boundaries / invariants.** Both facets are ignored on the `relative_to`
  walk (the anchor pins one session), consistent with `noise`/`project_dir`;
  validation still runs up-front so a malformed value fails loud even there.
  `iter_events` gained a `parent` parameter. `plan()` and `incidents` keep their
  own independent `kind`/`group` parameters — different verbs, unaffected.
