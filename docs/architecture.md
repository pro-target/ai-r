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
Stdio JSON-RPC. Eighteen tools in three groups:
- 7 classic tools: `list_sessions`, `read_session`, `search_sessions`,
  `find_file_edits`, `find_tool_calls`, `session_stats`, `session_diff`.
- 5 event-core verbs: `query`, `get_body`, `aggregate`, `diff`,
  `detect_current`.
- 6 presets: `plan`, `incidents`, `network`, `quotes`, `audit_brief`,
  `locate`.

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
`diff`, `detect_current`) and the presets (`plan`, `incidents`,
`network`, `quotes`, plus the stage-4 auditor presets `audit_brief` —
`src/ai_r/audit_brief.py` — and `locate` — `src/ai_r/locate.py`) are thin
readers over that stream — `query` is the workhorse;
a preset wires a fixed chain of base verbs, never a second engine.

The **model dimension** rides on that same taxonomy, not beside it: parsers
extract the producing model where the format records one (`Message.model`,
rolled up per session as `Session.models`; Claude assistant
`message.model` with the `<synthetic>` stub mapped to `null`, Codex
`turn_context.model`, OpenCode `message.data.modelID`, Pi assistant
`message.model`; Antigravity has no structured signal), every event
inherits the model of the message behind it (a `tool_call`/`plan_event`
carries its assistant turn's model), and the surface reuses the existing
verbs — a `model` facet on `query`, `group_by="model"` on `aggregate`, a
`models` list on session summaries, the current model on
`detect_current`. No new event type, preset or classifier; absence is
honest (`null`/`[]`, the `"(unknown)"` aggregate bucket), never guessed.

Several cross-cutting modules sit beside the core:

| Module | Responsibility |
|---|---|
| `redact.py` | Secret redaction on every emitting surface (title, message content, intent, qa) as `[REDACTED_<TYPE>]`, with a `redactions` type→count report. On by default. |
| `activity.py` | A3 session **recency**: the pure `session_activity` classifier (`now` injected, never read) → `age_sec` + `activity` (`fresh`/`stale` vs `AI_R_STALL_SEC`). An honest statement about the last written record only — deliberately NOT a process-liveness claim (F1.1). |
| `liveness.py` | Session **process-liveness** (see ADR below): fuses the `claude agents --json` pid registry (TTL-cached, sampled once per `list_sessions`) with `/proc` probes into `session_liveness` (`fresh`/`paused`/`zombie`/`dead`, honest `null` without a pid signal). The OS-derived complement to `activity.py`'s recency — Claude only, best-effort, never fabricated. |
| `user_refs.py` | User-attachment classification (see ADR below): the `USER_REF_KIND` dictionary + the extractors that fold a `user_turn`'s structured/text references into `{kind, target, origin}` for the `user_ref` facet + `user_ref_kinds` aggregate. Marks the pointer only — never fetches or sanitizes the referenced content. |
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
- **Concurrency — sync tools must not block the loop (amendment).** A shared
  server only pays off if it serves N agents *at once*, but the tool functions
  are synchronous corpus scans and FastMCP runs a sync tool **inline on the
  event loop** (`func_metadata.py`: `return fn(**args)`). So a single in-flight
  read/search froze the loop and starved every other connection until uvicorn's
  keep-alive dropped it (`not connected` under N parallel readers) — the shared
  cache was already thread-safe (`_haystack_cache_lock`), but the tools never
  actually ran concurrently. Fix: `_StrictArgsFastMCP.call_tool` offloads a sync
  tool's dispatch to a worker thread (`anyio.to_thread.run_sync`), so concurrent
  calls run in parallel; and `timeout_keep_alive` is raised to
  `AI_R_MCP_KEEPALIVE_SEC` (default 120 s, above the max expected tool duration)
  so a briefly-idle connection is not dropped. The offload is gated on
  `not tool.is_async`; **stdio serves one request at a time, so the extra hop is
  a behavioral no-op there** (back-compat preserved).
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
- **Tombstone lifespan.** The `kind` tombstone is kept through the `0.5.x` line
  and removed in `0.6.0` — a bounded fail-loud window giving callers time to
  migrate to `noise`, after which the dead parameter leaves the hot path.
- **Boundaries / invariants.** Both facets are ignored on the `relative_to`
  walk (the anchor pins one session), consistent with `noise`/`project_dir`;
  validation still runs up-front so a malformed value fails loud even there.
  `iter_events` gained a `parent` parameter. `plan()` and `incidents` keep their
  own independent `kind`/`group` parameters — different verbs, unaffected.

### ADR: fail-loud on unknown MCP arguments (surface-wide)

- **What.** `mcp_server.mcp` is a `_StrictArgsFastMCP` subclass; its `call_tool`
  rejects any argument key absent from the called tool's declared schema with
  `{"error": "invalid_argument", …}` (listing the accepted parameters) *before*
  the tool runs. Unknown *tool names* fall through to the base class; declared
  arguments are never short-circuited.
- **Why.** FastMCP builds a pydantic model per tool from its signature and
  pydantic **silently drops** unknown keys, so an invented/mistyped parameter
  produced a successful-looking but **unfiltered** result — the same "never a
  silent wrong answer" failure the `kind` tombstone (ADR above) was created to
  avoid, but `kind` covered only one facet. This closes the class for every
  tool with one transport-seam check (`set(arguments) − schema.properties`),
  not a per-tool guard.
- **How it was found.** A self-referential usage audit — ai-r reading its own
  development history (`find_tool_calls tool_name_pattern="mcp__ai-r__"`, 236
  calls) — surfaced two real phantom-parameter calls (`plan(limit=…)`,
  `list_sessions(since=…)`) that had returned unfiltered results with no error.
  This is exactly the "why / was-it-needed" audit the project sells, applied to
  the project itself.
- **Boundaries.** Deterministic, no data read on rejection. Pure helper
  `_unknown_tool_args` is unit-tested; `tests/test_mcp_strict_args.py` covers
  the phantom cases and declared-argument passthrough.

### ADR: `user_ref` — a user-attachment dimension over `user_turn` (Q1)

"What the user attached to a turn" — files, urls, images, IDE-injected context —
is a new dimension recorded on the existing `user_turn` event, not a new event
type or a second classifier. This ADR records the addition and, critically, the
reader/consumer boundary it deliberately does NOT cross.

- **What changed.** Each `user_turn` event gains a `user_ref` entry in `refs`
  per attached thing: `{kind, target, origin}`. `kind` ∈
  `file|url|image|attachment|ide_context` (`USER_REF_KIND` in
  `ai_r/user_refs.py`); `origin ∈ structured|text` distinguishes a real
  content-part (`structured` — the user definitely attached it) from a
  prose-mined reference (`text` — a `<doc path>`/`<ide_*>` block, a bare URL, an
  `@mention`). The surface reuses the existing verbs — a `user_ref` facet on
  `query` (`any` / a kind / a `target` substring), plus hoisted `user_refs` /
  `user_ref_kinds` row fields so `aggregate(group_by="user_ref_kinds")` works
  directly. No new event type, verb or preset.
- **Why.** An audit for "what context did the human actually bring in" was
  previously invisible — attachments were flattened into prose or (OpenCode)
  miscounted as a tool_use. The dimension surfaces intent-of-input on the same
  taxonomy the rest of the reader speaks.
- **Per-agent honesty.** Structured signals only where the format records them:
  Claude `image` parts + text-mined refs; OpenCode a `file` content-part on the
  user role (a bug-fix — it used to be classified as `tool_use`); Codex
  `input_image`; Antigravity/Pi carry no structured attachment signal, so they
  contribute only the text path (honestly empty when the prose carries none,
  never fabricated). `ide_context` is tagged distinctly because an
  `<ide_opened_file>` / `<ide_selection>` injection is weaker evidence than a
  deliberate attachment. A URL inside a fenced code block is not extracted.
- **Boundary — mark, do not fetch or sanitize (the load-bearing decision).**
  `target` is a POINTER only. ai-r records THAT a reference exists; it never
  follows it, never reads the referenced file/url, never sanitizes its content.
  Following the pointer is the consumer's job — and a consumer that does so MUST
  wrap the fetched bytes through `ai_r.security.sanitize_session_text()`, because
  external content pulled from a `target` is the maximum-injection tier and
  compounds the untrusted-source chain (see [Security](security.md)). The
  `target` string itself is redacted on emission (F2.1 `redact_text`) like any
  other text/intent — that is the only transformation ai-r applies to it.
- **Invariants.** Additive to `refs`; the `user_turn` event `sha256`/identity is
  unaffected; a turn with no attachment keeps the base shape (no empty
  `user_ref`). SSOT `ai_r.user_refs`.

### ADR: thinking is opt-in, not silently in the searchable body (Q2)

Earlier, captured model reasoning (`Message.thinking`) was folded into the
body-search haystack automatically — "reasoning is now part of the searched
body". This ADR records the reversal to **opt-in** and why.

- **What changed.** Thinking is no longer in the default search or output.
  `search_sessions` / `query` (text haystack) / `read_session` / `get_body` take
  `include_thinking` (default `false`); `true` restores the old
  reasoning-in-search behaviour. A new `assistant_turn.has_thinking` boolean
  hint + a `query(has_thinking=…)` facet let a consumer find turns that HAVE
  reasoning and pull it on demand.
- **Why.** Two reasons. (1) **Separation of drafts from conversation** — an audit
  for what was actually said/done should not be polluted by a model's internal
  scratchpad; reasoning is a different kind of text and now stays a distinct
  field, opt-in. (2) **Budget** — the reasoning of a large session can dwarf its
  prose, so folding it into every default search inflated the haystack an auditor
  pays for. Reachable-on-request is the right default for a signal that is
  usually noise to the "what happened" question.
- **Boundaries.** `include_thinking=true` on `read_session`/`get_body` adds a
  SEPARATE `thinking` field alongside `content`/body — reasoning is never merged
  into the conversation text. Some agents stream reasoning as a separate, text-less
  record (common in Claude, the norm in Codex); rather than lose the per-turn hint
  there, the parser folds an orphan reasoning record's `thinking` onto the adjacent
  answer message (`fold_orphan_thinking` in `ai_r.parsers._common`), preserving
  `message_index`, so `has_thinking` / `read_session` / `get_body` surface reasoning
  uniformly for Claude / Codex / OpenCode / Pi. The one residual gap is a reasoning
  record with no following answer text before the next user turn (a rare tail),
  which stays capture- and search-only. Antigravity records no reasoning signal, so
  `has_thinking` is always `False`. `component_tokens.thinking` accounts for the
  volume regardless.
- **Fail-soft / invariants.** `has_thinking` is a hint, explicitly **not** part
  of the event `sha256` (adding it changes no identity), and the query row emits
  it only when `True`. The default (`include_thinking=false`) output is
  **byte-identical** to before the flag existed — `read_session` never inlined
  thinking into `content`, so the default projection is unchanged; only the
  opt-in path adds the extra field. SSOT `Message.thinking` + the
  `include_thinking` plumbing in `ai_r.events` / `ai_r.mcp_server`.

### ADR: subagent cost — a price on the existing `task` call, not a new taxonomy

ai-r could say a subagent was spawned (`tool_kind=task`, `resolve_tool`), but not
what it *cost*. The parent transcript carried the answer all along — a
record-level `toolUseResult` sidecar with the child's resolved model and its
exact billed usage — and the parser dropped it, keeping only
`{content, is_error, tool_use_id}` from the tool result. "Which subagent burned
the budget, and on which model" therefore required hand-parsing JSONL.

- **What changed.** The sidecar is normalised onto the EXISTING tool-result entry
  as `subagent` (`model` / `agent_type` / `tokens` / `status` / `duration_ms` /
  `tool_uses`) and surfaced by `find_tool_calls` alongside a new `tool_use_id`.
  `read_session(include_subagents=True)` children gained `tokens`, `models`, and
  a joined `subagent_type`/`status`. `session_stats` gained `group_by="model"`.
- **The child join is opt-in (`with_subagent_cost`), because it costs disk.** The
  parent-side sidecar answers "what did this spawn cost" for a COMPLETED spawn,
  but for a background spawn it was written at launch and names neither the
  persona nor the tokens — and background spawns are the majority (measured:
  701 of 981 sidecars `async_launched`, none carrying `agentType` or `usage`).
  Recovering those requires reading the child's own transcript + meta, one small
  file per spawn. On a single `read_session` that is already paid; on a
  cross-corpus `find_tool_calls` scan it would be a per-spawn I/O storm nobody
  asked for. So `find_tool_calls` keeps the join behind `with_subagent_cost=True`
  (default off = the parent sidecar verbatim, byte-identical to before, zero
  extra reads), and both callers route through ONE resolver —
  `session_stats.subagent_cost_facts` / `subagent_costs_by_spawn` — rather than a
  second, drifting copy of the persona/token join (the DRY rule this repo
  enforces). The child is the source; the parent sidecar is the fallback for a
  child that cannot be joined (not yet on disk, meta corrupt), and only a child's
  `exact` tokens are lifted into `find_tool_calls`' billing field — an estimate
  is never merged there.
- **Child tokens follow the honest three-tier `source` ladder — `exact` is NOT
  guaranteed per child.** `read_session` rollup children carry
  `session_tokens(child)`, which is `exact` only where the child's transcript
  records usage; a truncated or reference-only child yields a labeled `estimate`,
  and a signal-less one `source: null`. On the real corpus a single parent's
  rollup returned children `{exact: 8, estimate: 2}`. The emitted field was
  always honestly labeled — this ADR (and the docs) simply stopped *claiming*
  a blanket `exact` the code never promised, because a project selling honest
  measurement cannot ship a spec that reports `estimate` as `exact`. A
  regression test seeds a usage-less child and asserts the rollup marks it
  `source != "exact"` (not a fabricated `exact`, not a zero).
- **Why a dimension, not a second classifier.** The spawn is already classified
  by `resolve_tool`; cost is a new *measurement* over that call, exactly as
  `model` was a dimension over the event taxonomy rather than a parallel one.
  Building a separate "subagent registry" would have duplicated the classifier
  the project already trusts (the DRY rule this repo enforces).
- **Why `model` is the load-bearing field.** A subagent may resolve to a model
  DIFFERENT from its parent's — a persona can pin a cheaper tier. So the parent's
  model does not answer what a child cost, and a child still running the parent's
  model is precisely the signal that an unpinned persona is burning the expensive
  tier. This is the measurement that makes a model-pinning policy auditable
  instead of aspirational.
- **Honesty guards.** The sidecar is record-level while one record may carry
  several `tool_result` parts; with more than one, attribution would be a guess,
  so the sidecar is **dropped** rather than billed to the wrong subagent (fail
  closed). Ordinary tools also carry a `toolUseResult` (often a plain string) —
  only a subagent-shaped payload is lifted. `tokens.source` is always `"exact"`:
  billed usage, never an estimate mixed into the same field.
- **Background spawns (fail-soft) — and why the persona comes from the CHILD.**
  A background spawn writes its sidecar at launch (`status: async_launched`),
  before the run exists: it names a model but carries neither usage nor
  `agentType`. These are the MAJORITY of spawns in a real vault, so a design
  that read the persona from the parent's sidecar would leave most children
  anonymous — an adversarial review of this very change measured 0 of 54
  children named. The persona is therefore read from the child's own
  `agent-*.meta.json` (`extra.subagent_type`, alongside the `toolUseId` /
  `spawnDepth` that file already supplied), and the exact tokens from the
  child's own transcript. The parent sidecar is the FALLBACK, not the source.
  Absence still stays absence — a background spawn's `subagent.tokens` is
  omitted rather than reported as zero, because "free" and "not yet measured"
  are different claims. SSOT `_subagent_sidecar` + `_read_subagent_meta`
  (`ai_r.parsers.claude`) + the rollup in `ai_r.mcp_server`.
- **What is NOT claimed.** `total` is the harness's `totalTokens` where present.
  In the corpus checked it equals the sum of the `usage` components in every
  completed spawn (280 of 280), so no claim is made that it captures rounds
  `usage` misses — it is simply the number the harness bills, preferred over a
  locally recomputed sum. Where a sidecar records `usage` but no `totalTokens`
  (never seen in the corpus, but not forbidden by the format) the sum is the
  documented fallback, and where it records neither there is **no** token block
  at all. `tests/test_claude_subagent_cost.py` pins all three branches, so a
  later "simplification" to a locally recomputed sum turns the suite red instead
  of silently changing what every cost report means. Numbers are ints: a JSON
  `true` is not a token count (a bool is an `int` in Python — the parser rejects
  it explicitly, and a regression test seeds a bool-poisoned sidecar).
- **Measured, not assumed.** The two claims this design rests on were checked
  against the real corpus at the time of the change: background spawns are the
  majority (701 of 981 sidecars are `async_launched`, and none of them carries
  `agentType` or `usage`), and every child's own `agent-*.meta.json` names its
  persona (1191 of 1191). That is why the persona is read from the child and the
  parent sidecar is only the fallback — the reverse would leave ~72 % of spawns
  anonymous.

### ADR: `audit_brief` + `locate` — auditor presets (stage 4, token-lean reading)

Auditing a session through the raw surface costs a hand-built chain
(`read_session` + `query` + `plan` + token math) and an unbounded amount of
output; finding a half-remembered session costs a manual walk over five
agents' stores. Both are the exact shape the project preset rule exists for:
a frequent chain **with an algorithm inside** (deterministic selection + a
token budget), so each became one call rather than a documented example.

- **`audit_brief` (`src/ai_r/audit_brief.py`)** — a budgeted one-call session
  digest. The baked chain reuses only existing projections: ONE
  `query(session=…)` scan (user turns + tool rows), `aggregate` folds for the
  tool/file footprint, `plan`/`plan_feedback` for the decision trail,
  `ai_r.tokens` for the token breakdown — no new event taxonomy, no second
  classifier (the DRY rule this repo enforces). The algorithm inside is the
  **budget ladder**: the digest is measured on its ACTUAL serialized JSON and
  tightened in a fixed order (tool-error details → per-file list → plan
  bodies/feedback texts) until it fits `budget_chars`. **User turns are the
  auditor's ground truth and are never truncated** — when they alone exceed
  the budget the response says so (`over_budget: true` + a note naming the
  full projections) instead of silently clipping them. The session-scoped
  file footprint reads the edit/write rows' existing `file` refs rather than
  the `find_file_edits` core, because that core has no session facet (it
  scans the corpus by `path` substring) — the classifier is reused, not
  duplicated.
- **`locate` (`src/ai_r/locate.py`)** — session lookup across every agent by
  full uuid, id prefix, or case-insensitive title substring. A thin preset
  over the per-parser `list_sessions` inventory (zero new scanning code):
  deterministic match (uuid/path-stem prefix = `id`, title substring =
  `title`), rank by last activity descending, and per match the location
  facts plus the ready-to-run `read_command` / `resume_command` (F2.2 —
  text only, never executed). `readable` is an honest local-content claim:
  a reference-only stub (Desktop metadata without a transcript) is listed
  but marked not readable. Zero matches → closest-title suggestions +
  diagnostics, never a fabricated match.
- **`locate --web` v1 is honest-scope by decision.** A local tool cannot
  enumerate claude.ai's cloud store, so v1 reports only web sessions KNOWN
  LOCALLY: materialized hook-export files (`$SW_HOME/web-sessions`) and
  `~/.claude.json → projects[*].lastSessionId` teleport stubs — the latter
  explicitly marked `content_local: false` (the id is known, the transcript
  is not on this machine; absence is honest, never fabricated). The fuller
  source — a per-repo teleport-picker sweep — requires driving the CLI under
  a PTY and is a documented follow-up, deliberately NOT built into a read-only
  reader now.
### ADR: process-liveness as an OS-signal complement to recency, not a rewrite of it

`activity` (A3 recency) is deliberately silent about the *process*: a session
file cannot show whether its producer still runs, so F1.1 forbids ai-r from
turning "nothing written for a while" into "crashed". That honesty left a real
gap for a supervising poller — a *stale* session is either paused-but-alive or
dead, and the transcript cannot tell them apart. The consumer was told to
"correlate `activity == stale` with an OS pid check" themselves; a recurring
operational lesson ("stale ≠ dead") showed that inference kept getting
re-derived by hand instead of being answered once, at the source.

- **What changed.** A new `liveness` field on `list_sessions` summaries and
  `detect_current` answers exactly that, from a *verifiable* OS signal rather
  than the agent's own say-so (a self-declared status could lie; `/proc`
  cannot). It fuses two signals: the `claude agents --json` pid registry (a
  first-party map of running Claude sessions → pid) and `/proc` probes on that
  pid — process present (`/proc/<pid>/comm` readable) and holding open fds
  (`/proc/<pid>/fd` non-empty; the kernel closes every fd when a process goes
  defunct, so an empty fd table is the zombie signature). The verdict:
  `fresh`/`paused` (alive, recency fresh/stale), `zombie` (present, I/O dead),
  `dead` (registry named a pid `/proc` no longer shows), `null` (no pid signal).
- **Why a complement, not a replacement.** `activity` stays the pure recency
  classifier (F1.1 unchanged, `activity.py` untouched); `liveness` layers the
  process verdict on top and *reuses* the recency label to split `fresh` vs.
  `paused`. Building liveness by mutating `activity` would have re-fabricated
  the very claim F1.1 forbids. The pure core `session_liveness(activity,
  pid_alive, io_alive)` stays hermetically testable; only the resolver touches
  `/proc`/the registry (DRY: pid presence reuses `session._pid_comm_starts_with`
  with the empty prefix rather than a second `/proc` reader).
- **Why honest `null`, and why only Claude.** Only Claude exposes a pid
  registry, so every non-Claude session reports `null` — no signal, never a
  guess. Absence *from* the registry is also `null`, never `dead`: the registry
  is not assumed exhaustive, so `dead` is emitted only when a concrete pid
  turned out gone. Everything is best-effort — a missing `claude` CLI, a
  timeout, a non-zero exit or unparseable output all collapse to `null`, never
  an error — and the registry is sampled once per `list_sessions` call
  (TTL-cached ~2.5 s), not once per session, so a listing spawns the subprocess
  at most once. SSOT `ai_r.liveness.session_liveness` + `resolve_session_liveness`.
