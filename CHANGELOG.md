# Changelog

All notable changes to this project are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`network` preset: network-egress audit (F4.3)**: new MCP tool
  `network` (core `ai_r.network`) answers "where did an agent reach out
  to the network — and how risky did those requests look?" in one call.
  A preset over the existing core, not a second engine: ONE
  `query(type="tool_call", tool_kind="web")` scan supplies the candidates;
  the request target (`url`/`query`) is extracted from each call's own
  input (explicit `url`/`query` keys, or the first URL embedded in a
  `prompt` string — the Gemini `web_fetch` shape; nothing extractable →
  honest `null` fields and `kind: null`, never guessed from the tool
  name); a deterministic **risk dictionary** assesses each request —
  `plain_http`, `credentials_in_url`, `secret_in_url`/`secret_in_query`
  (the F2.1 redaction patterns double as the detector — one vocabulary,
  two uses), `ip_literal_host`, `private_or_local_host`,
  `punycode_host`. Each record carries the query event `id` (context
  on-demand via `relative_to` / `read_session`), derived `kind`
  (`fetch`/`search`), char-capped `url`/`query` (token budget; the cap is
  applied AFTER redacting the full string, so a boundary-sliced secret
  never leaks partially), `domain`, `risks` and tri-state `is_error`
  (`null` where the agent's format has no correlated outcome signal).
  Filters are all parameters: `agent`, `session` (uuid or list),
  `since`/`until`, `kind`, `risk` (`include`/`only`/`exclude`), `domain`
  (equals-or-subdomain), `noise`, `project_dir`;
  `count`/`risky_count`/`by_domain`/`by_risk` always reflect the FULL
  match set independent of `limit`; unknown `kind`/`risk` values fail
  loud; zero requests → empty-result `diagnostics` (F1.1). Documented
  boundary: MCP-mediated network access stays under `tool_kind="mcp"` —
  a name alone cannot prove an MCP server touches the network, so it is
  never guessed into the audit. Scenarios NET-1..4.

- **Codex web-search signal in the parser (F4.3 groundwork)**: Codex
  rollouts record native web access as `web_search_call` response items
  (an `action` object: `search` → `query`, `open_page`/`find_in_page` →
  `url`), not as `function_call` — previously invisible. The codex parser
  now surfaces each one as a `web_search` tool_use (input = the action
  object), so the F3.1 classifier marks it `tool_kind="web"` and Codex
  egress participates in `query`/`find_tool_calls`/`network` like every
  other agent's. No result record exists for these items, so `is_error`
  stays honestly unknown. The `google_web_search` name
  (Gemini/Antigravity family, verified against the vendored gemini-cli
  reference) was added to the web-name vocabulary alongside the existing
  `webfetch`/`web_fetch`/`websearch`/`web_search`; Pi records no web tool
  — honest absence, nothing fabricated.

- **`incidents` preset: dangerous command + regret reaction (F4.1)**: new
  MCP tool `incidents` (core `ai_r.incidents`) answers "where did an agent
  run something destructive — and did it then apologise?" in one call. A
  preset over the existing core, not a second engine: ONE
  `query(type="tool_call", tool_kind="bash")` scan supplies the candidates;
  a deterministic **danger dictionary** (19 patterns, `fs`/`git`/`db`/`net`,
  harvested from public agent-guardrail rule sets and calibrated on real
  host history 2026-07-04 — 297 candidates / 4 confirmed; `db.truncate`
  tightened after firing on English prose) selects dangerous commands from
  the extracted command field (a Bash `description` alone never fires;
  `--force-with-lease` is not force-push); a bilingual (ru+en) **regret
  dictionary** scans the next `reaction_window` messages (default 6) for an
  apology/rollback reaction — the two-step check behind `confirmed`, never
  guessed. Each record carries the query event `id` (context on-demand via
  `relative_to` / `read_session`), `patterns` + `categories`, a char-capped
  `command` fragment centred on the hit (token budget), tri-state
  `is_error` (`null` where the agent's format has no correlated outcome
  signal — honest, cross-agent) and `reaction` (marker labels + capped
  preview; `null` when unconfirmed). Filters are all parameters: `agent`,
  `session` (uuid or list), `since`/`until`, `category`, `confirmed`
  (`include`/`only`/`exclude`), `noise`, `project_dir`;
  `count`/`confirmed_count`/`by_pattern` always reflect the FULL match set
  independent of `limit`. Unknown `category`/`confirmed` values fail loud;
  emitted fields are redacted by default (F2.1) while matching runs on RAW
  text; zero incidents → empty-result `diagnostics` (F1.1). Documented
  caveat: the dictionary cannot tell mention from execution (an `echo`-ed
  dangerous string can match). Scenarios INC-1..4.

- **Plan iterations v2: draft numbering, quote→section anchoring, rounds
  (F3.4 v2)**: (a) every plan atom now carries `version` — its 1-based
  revision number within the task group in chronological `(ts, seq)`
  order (drafts are `v1…vN-1`, the final is `vN`; numbering restarts per
  task), so a draft stays a cheap «version + title + ref» reference;
  (b) every feedback pair carries `plan_version` (the answered revision's
  number, `null` without call-id correlation) and `section` — the heading
  of the plan section the quote anchors to: the user selects quotes from
  the RENDERED plan (the UI strips markdown markup), so both the quote
  and each section of the raw markdown source are compared through the
  same markup-stripping normalization (heading hashes, list/blockquote
  markers, checkboxes, emphasis asterisks, backticks, link targets;
  whitespace collapsed; fenced code blocks never start a section); a
  quote that matches NO section — or MORE than one — gets an honest
  `null` anchor, never a nearest guess; (c) pairs are grouped by
  `round` — the 1-based feedback-round number within the session (one
  round per user response that produced pairs) — and the new `rounds`
  parameter (`"all"` default, `"last"` keeps only each session's final
  round, anything else fails loud) filters them; (d) v1 boundary fixed:
  plan call-ids now come from the plan-signal SSOT, so a **rejected
  plan-file `Write`** with user words correlates to its revision exactly
  like an `ExitPlanMode` verdict (a successful Write's "File created…"
  result matches no recognised format and stays filtered). Plan atoms
  themselves are unaffected by `rounds`; historical fields are
  unchanged. Scenarios PLAN-9..11.

- **Plan iterations: final text + «quote → comment» pairs (F3.4 v1)**:
  `plan` now returns, by default, everything a consumer needs to replay a
  plan-approval iteration without inlining every draft (measured ≈×3.7
  cheaper than "all bodies"): (a) the **final** plan's full text inline —
  `body` + `body_source`, where the AUTHORITATIVE text is the user-edited
  plan carried by the approval response (`"approval_edited_by_user"` —
  the plan file on disk can diverge from what was actually approved),
  falling back to the plan signal (`"plan_signal"`), honest `null` for
  steps-only plans (Codex); drafts stay references (bodies via
  `get_body`); (b) a `feedback` list of ALL «plan quote → user comment»
  pairs extracted from the user's plan responses, chronological — each
  pair carries `plan_id` (the exact revision it answered, correlated by
  call id), `verdict` (`rejected` | `stay_in_plan_mode`), `quote`
  (`null` for a free-text comment), verbatim `comment`, `ts` and a
  `ref` (`"<session>:pf<N>"`) that `get_body` resolves to the FULL raw
  response blob (type `plan_feedback`) on demand. The recognised
  response formats (verified on real vaults): "On selected text:"
  quote blocks, stay-in-plan-mode `[Re: "…"]` comments, free-text
  rejections, approvals with/without an edited plan; technical failures
  (permission-stream errors) and bare no-comment rejections are
  filtered out. Only agents with an interactive plan-approval flow have
  the signal (today: Claude `ExitPlanMode`); others honestly contribute
  nothing — never fabricated. Redaction (F2.1) covers plan bodies,
  quotes, comments and raw responses. Backward-compat switches:
  `bodies="none"` restores reference-only atoms, `feedback=false` omits
  the pair list; historical fields are unchanged. New core
  `ai_r.events.plan_feedback`; scenarios PLAN-6..8, BODY-5. (The v2
  entry above adds draft version numbering, quote→section anchoring
  and `rounds`.)

- **Token usage in stats (F3.3)**: `session_stats` gains
  `with_tokens=true` and `aggregate` gains the `tokens` metric. Per
  session the usage is read from the agent's own files **at request
  time** (nothing background, no index): **exact** where the format
  records numbers — Claude per-call `message.usage` (streamed duplicates
  deduplicated by `(message.id, requestId)`), Codex last cumulative
  `token_count` event, OpenCode per-assistant-message
  `message.data.tokens`, Pi per-assistant-message `usage` — via a new
  per-parser `read_token_usage` (feature-for-all-where-signal); a
  session without a recorded signal (Antigravity, or older data) gets a
  transcript-volume **estimate**, labeled `source="estimate"` +
  `estimator`: tokenized by the **optional** `tiktoken` dependency
  (`pip install "ai-r[tokens]"` / `AI_R_EXTRAS=tokens bash install.sh` —
  documented in pyproject extras, install.sh and both READMEs) when
  installed, else a rough `chars/4` heuristic — degradation, never a
  crash; no signal at all stays honest `unknown`. The folded block per
  group/totals is `{input, output, reasoning, cache_read, cache_write,
  total, exact, estimated, unknown}` — sums are `null` when no row
  carried the field (never a fabricated 0) and the provenance counters
  always satisfy `exact + estimated + unknown == rows`. Only
  ai-r-computed integers and labels are emitted (no raw session text),
  so the block is outside the redaction surface by construction.
  Backward-compat: `with_tokens` defaults to `false` — byte-identical
  historical output. SSOT `ai_r.tokens`; scenarios AGG-5, STAT-4.
- **Session list as a `query` filter (F3.2)**: the `session` facet of
  `query` (core and MCP) now accepts a **list** of session uuids in
  addition to the single uuid string — one call returns the union of
  those sessions' events (e.g. the ids picked from a
  `search_sessions`/`list_sessions` result), in the usual chronological
  order across sessions. Duplicates collapse; an unknown uuid
  contributes nothing (the same honest empty-miss semantics as the
  single-uuid form). Fail-loud validation (SSOT
  `ai_r.events.model.normalize_session_filter`, shared by
  `iter_events`): an empty list or a non-string/blank item is a
  `ValueError` (`invalid_argument` over MCP) — never a silent
  full-corpus scan; the facet is validated even on the `relative_to`
  walk where it may otherwise go unused. Backward-compat: the scalar
  string form is byte-identical to before; empty-result diagnostics
  echo the list value under `filters.session`. Scenario QRY-12.
- **Real names under wrappers (F3.1)**: every tool call is now classified
  wrapper-aware. Each `tool_call` event (`query`) and every
  `find_tool_calls` record carries `tool_kind` — one of
  `edit|write|read|bash|task|skill|mcp|web|other` — and, when a wrapper's
  input names the real actor, `tool_resolved`: the subagent type under a
  spawn wrapper (Claude `Task`/`Agent` → `input.subagent_type`, OpenCode
  `task` → `subagent_type`, Codex `spawn_agent` → `agent_type`), the
  skill name under Claude `Skill` (`input.skill`) / OpenCode `skill`
  (`input.name`) / `SlashCommand` (`input.command`, reduced to the bare
  command token), or `"<server>:<tool>"` for a Claude-style
  `mcp__<server>__<tool>` name. Honest per-agent signals only: a wrapper
  whose input carries no name key gets no `tool_resolved` (never
  guessed); Codex/OpenCode/Pi record MCP calls under bare or
  underscore-joined names with no reliable server delimiter, so no `mcp`
  detection there. `query` gains a `tool_kind` facet (exact match,
  unknown value fails loud) and the `tool` facet now also matches
  resolved names (`tool="commit"` finds the SlashCommand that ran the
  `commit` skill); `tool_kind`/`tool_resolved` are hoisted to top-level
  event-dict fields so `aggregate(group_by="tool_kind")` works on query
  rows directly. Backward-compat: the event `type` keeps the base
  `tool_call(<sub>)` subtype (a Task call is still `tool_call(other)`) —
  no counts or existing filters change; `tool_resolved` passes the F2.1
  emission-time redaction on both surfaces. The `web` kind
  (WebFetch/WebSearch/webfetch, name-based) lays the groundwork for the
  network-audit phase. SSOT `ai_r.events._common.resolve_tool` +
  `TOOL_KIND`; scenarios QRY-11, FTC-5.
- **Session outcome classification (F2.3)**: `read_session` now carries
  an `outcome` block — `{status, signals, user_verdict, markers,
  tool_results, tool_errors, error_rate, error_rate_reliable}` with
  `status ∈ success|failure|mixed|unknown`. Two honest signals, never a
  guess: (1) **tool-call error rate** — the share of tool results the
  agent itself flagged as failed; a *real* source flag exists only for
  Claude (`tool_result.is_error`) and OpenCode (`state.status ==
  "error"`), so for Codex/Pi/Antigravity `tool_errors`/`error_rate` are
  `null` (`error_rate_reliable: false`) — mirrors
  `find_tool_calls.is_error_reliable`; (2) **user-verdict dictionary** —
  bilingual (ru+en) success/failure markers matched against the last 3
  *human* user turns only (assistant self-reports are never trusted;
  XML wrappers / `[...]` placeholders / `Caveat:` preambles skipped).
  Decision table: negative verdict → `failure`; positive → `success`
  (`mixed` when errors dominate); neutral + dominant errors → `failure`;
  otherwise `unknown` (empty `signals` ⇔ `unknown`). Thresholds and the
  dictionary are **calibrated on real history** (audit 2026-07-04, 107
  Claude + 48 OpenCode sessions: median error rate 0.09/0.02, p90
  0.22/0.08 → "dominant" = `rate ≥ 0.5` across `≥ 4` results; «повтори»
  dropped from the negative set — in real use it means "re-run after an
  accidental interrupt", not a failure verdict; seeded from the
  web-harvested `cass_memory` outcome dictionary). Validation run over
  150 real sessions: 125 unknown / 17 success / 8 failure — conservative
  by design. The block contains only ai-r-authored strings and
  dictionary labels (never raw session text), so it needs no redaction
  pass. SSOT `src/ai_r/outcome.py`; scenarios OUT-1…OUT-2.
- **Resume command in session summaries (F2.2)**: every session summary
  (`list_sessions` / `read_session` / `search_sessions` candidates) now
  carries `resume_command` — the ready-to-run shell one-liner that
  reopens the conversation in its agent's CLI, next to
  `project_dir`/`launch_surface`. Text only, never executed by ai-r.
  Shapes (verified against the installed CLIs' `--help`, not invented):
  Claude `cd <project_dir> && claude --resume <uuid>` (`--resume`
  resolves against the cwd's project store → `cd` prefix; bare command
  when `project_dir` is unknown), Codex `codex resume <uuid>`, OpenCode
  `opencode --session <id>`, Pi `pi --session <session-file-path>` (the
  path form is cwd-independent, the id lookup is not) — each
  `cd`-prefixed when `project_dir` is known, all values shell-quoted.
  `null` where no real command exists: Antigravity (IDE brain dirs have
  no CLI resume verb), subagent (sidechain) sessions, reference-only
  Claude Desktop sessions (transcript deleted). SSOT
  `src/ai_r/resume.py`; see `docs/methods.md` → *Resume command*;
  scenario RES-1.
- **Secret redaction on output (F2.1)**: every method that emits
  session-derived text now masks secrets **on output by default** —
  `query` (`text`/`intent`), `get_body`, `plan`, `diff`/`session_diff`,
  `read_session`, `search_sessions`, `list_sessions` (titles),
  `find_file_edits`, `find_tool_calls`. Each replacement is
  `[REDACTED_<TYPE>]` (types: `PRIVATE_KEY`, `AWS_KEY`/`AWS_SECRET`,
  `GITHUB_TOKEN`, `GITLAB_TOKEN`, `ANTHROPIC_KEY`, `OPENAI_KEY`,
  `SLACK_TOKEN`, `URL_CREDENTIALS`, `BEARER_TOKEN`, `GENERIC_SECRET`;
  pattern SSOT `src/ai_r/redact.py`); when anything was masked the
  response carries a per-type `redactions` count dict; `redact=false`
  returns the raw content. Redaction is **emission-time only**: filters
  and search always match the RAW stored text, so a literal secret is
  still findable (only the displayed output is masked). Value-shaped
  patterns require a digit and the generic catch-all requires an
  explicit secret-ish key name, so uuids/git hashes/`sk-learn`-style
  prose never trip. Empty-result diagnostics gained a redaction link:
  a filter value that is a `[REDACTED_*]` placeholder (can never match —
  placeholders don't exist in stored text) or that itself looks like a
  secret earns a hint explaining the semantics and suggesting
  `redact=false`. `session_stats`/`aggregate` emit only counts/labels
  (no session text) and deliberately take no `redact` parameter.
  See `docs/methods.md` → *Redaction*; scenarios RED-1…RED-3.
- **Session origin — `project_dir` + `launch_surface` (F1.4)**: every
  session summary now carries two first-class origin fields next to
  `kind`/`parent_uuid`, both `null` when the source format has no
  signal (absence is honest, never fabricated). `project_dir` — the
  project directory the session ran in: Claude record-level transcript
  `cwd` (fallback: Desktop metadata `cwd`, then a filesystem-verified
  decode of the `projects/<slug>` storage encoding), Codex
  `session_meta.payload.cwd`, OpenCode `session.directory` (legacy DBs
  without the column degrade to `null` via a legacy-SELECT fallback),
  Pi header `cwd`; Antigravity has no signal. `launch_surface` — where
  the session was driven from: Claude `"claude-cli"|"claude-desktop"`
  (from the F1.3 overlay signal), Codex the raw `originator` string
  verbatim (e.g. `"codex_vscode"`, `"Codex Desktop"`), Antigravity
  `"antigravity-ide"|"antigravity-cli"` (by brain root); OpenCode/Pi
  have no signal. `list_sessions` and `query` take a `project_dir`
  filter — exact match **or descendant**, path-boundary aware (`/a/b`
  never matches `/a/bc`), applied at the session level before any
  message is read, fail-loud on a blank value. Session summaries also
  pass the parser `extra` bag through (e.g. `extra["source_root"]`,
  `extra["cli_title"]`). See `docs/methods.md` → *Session origin*.
- **Claude Desktop source root (F1.3)**: the Claude parser now scans the
  Claude Desktop app's own session store
  (`~/.config/Claude/claude-code-sessions`, honouring `AI_R_HOME`) as a
  second root. The store holds per-session *metadata* JSONs (not
  transcripts) that reference the backing CLI JSONL via `cliSessionId`,
  so the two roots are merged with uuid-keyed deduplication: a session
  visible in both is returned once, enriched — the Desktop `title` wins
  (the CLI-derived title is kept in `extra["cli_title"]`), which makes
  Desktop-launched sessions findable by the title the user actually sees
  in the app. Origin is marked in `extra["source_root"]`
  (`"cli"`|`"desktop"` — a launch-surface signal, groundwork for F1.4
  `launch_surface`). A metadata-only session (transcript deleted)
  surfaces as a zero-message reference; a missing root is skipped, never
  an error; `source_roots()` reports both roots for empty-result
  diagnostics. See `docs/methods.md` → *Claude session sources (CLI +
  Desktop overlay)*.
- **Session-level `noise` filter (F1.2)**: `query`, `list_sessions` and
  `search_sessions` take `noise=exclude|include|only` (default `include`
  — fully backward-compatible). A session is *noise* when it is a spawned
  subagent/sidechain session (`kind == "subagent"` or `parent_uuid` set);
  criterion SSOT in `src/ai_r/parsers/_noise.py`. The filter applies
  before any message is read, composes with the other filters by AND, and
  fails loud (`invalid_argument`) on an unknown mode. See
  `docs/methods.md` → *Noise filter (session-level)*.
- **Cross-agent subagent detection**: `kind`/`parent_uuid` are now
  populated for OpenCode (`session.parent_id` — previously the parent was
  read but `kind` stayed `"agent"`), Codex
  (`session_meta.payload.thread_source == "subagent"` +
  `parent_thread_id`, incl. the nested
  `source.subagent.thread_spawn.parent_thread_id` fallback — previously
  ignored) and Pi (`parentSession` promoted from `extra` to the
  first-class fields). Claude was already covered; Antigravity's format
  carries no parent signal and always reports `kind="agent"`.
- **Empty-result diagnostics**: a zero-result response of `query` /
  `search_sessions` / `find_tool_calls` / `find_file_edits` /
  `list_sessions` now carries a `diagnostics` object (per-agent scan
  counts + date bounds + `source_found`, corpus totals, cause hints —
  e.g. a missing source directory or a `since`/`until` bound that
  excludes the entire corpus). Non-empty responses are unchanged and
  never pay for it. See `docs/methods.md` → *Empty results & session
  lookup*.
- **Event-core layer**: a unified event stream over every parser, exposing
  five verbs — `query`, `get_body`, `aggregate`, `diff`, `detect_current` —
  plus the `plan` preset. Reference-by-default: `query` returns lightweight
  event references and message bodies are pulled on demand via `get_body`.
- **MCP surface**: the event-core verbs and the `plan` preset are exposed as
  MCP tools, raising the MCP tool count from 7 to 13 (`list_sessions`,
  `read_session`, `search_sessions`, `find_file_edits`, `find_tool_calls`,
  `session_stats`, `session_diff`, `query`, `plan`, `get_body`, `aggregate`,
  `diff`, `detect_current`). See
  [docs/architecture.md](./docs/architecture.md).

### Changed

- **`read_session` no longer requires `agent`**: the parameter is optional —
  when omitted, the session id is resolved across every parser. A rare
  cross-agent id collision returns a `candidates` list (not an error); a
  miss names the `agents_scanned`.
- **`session_stats` / `session_diff`**: reduced to thin presets over the
  event-core verbs — `session_stats` maps to `aggregate(rank_by="stats",
  kind_split=True)`, `session_diff` to `diff` over an intent-carrying
  `query`. Output stays byte-identical on real data, so the MCP surface is
  backward compatible.
- **`find_file_edits`**: reference-by-default — the MCP tool now returns
  lightweight references (`input_sha256` + `input_chars`) instead of inlining
  full edit bodies; pass `include_input=true` for the full body. The core
  default is unchanged, so internal callers are unaffected.
- **`query` facets `kind`/`parent`/`group`**: now fail loud with a clear
  error instead of being silently ignored — an unimplemented filter can no
  longer mislead a caller into trusting an unfiltered result.
- **CI**: `ruff` and `mypy` are now enforced gates.

### Fixed

- **`query` events are reference-by-default again (QRY-1 contract)**: the
  MCP `query` response inlined each event's FULL `text` (measured up to
  ~12.5 KB per event), violating the "events carry references, never
  bodies" contract. The MCP wrapper now cuts every emitted event `text`
  to a ~160-char preview **at the output boundary, after emission-time
  redaction** (a secret at the head of a long body is masked in the
  preview too); a real cut is marked with a trailing `…` and
  `text_truncated: true` (flag absent when nothing was cut).
  `id`/`refs`/`sha256` are untouched, so `get_body(id)` still returns
  the full body on demand. In-process consumers of full event text
  (`plan`, `diff`, `session_stats`, `session_diff`, `find_*`, the
  `events.query` core) are unaffected — the cut lives only in the MCP
  projection, not in the core.
- **Transcript timestamps are tz-aware; Desktop-ghost sort no longer
  crashes**: the shared `_parse_iso_timestamp` (Claude/Codex/Antigravity)
  truncated to 23 chars *before* replacing the trailing `Z`, so every
  transcript-derived date came out naive (no timezone). Mixing those with
  the tz-aware dates of Desktop-only ghost sessions (F1.3 overlay, epoch
  ms) made Claude's `list_sessions` date sort raise `TypeError`. The full
  string is now parsed first (honouring `Z` and explicit offsets; the
  23-char truncation remains as a noise fallback) and naive values are
  pinned to UTC, so every parser date is tz-aware. Serialised transcript
  dates now render with an explicit `+00:00` offset instead of the
  legacy `Z` suffix — same instant, still ISO 8601.
- **Empty-result diagnostics no longer re-scan the corpus**: on a
  zero-result response, `query` / `search_sessions` / `find_tool_calls` /
  `find_file_edits` / `list_sessions` used to call `list_sessions()` a
  second time across every parser just to build the `diagnostics` block —
  on a large live corpus this doubled a multi-minute scan. The callers now
  pass the per-agent session lists they already enumerated to the
  diagnostics builder (`scanned_sessions`); a fresh re-scan remains only
  as a fallback when nothing was passed. Response shape is unchanged.
- **CLI never leaks a Python traceback**: an unexpected internal error now
  exits non-zero with one structured JSON line on stderr
  (`{"error": "internal_error", ...}`) instead of a stack dump, so consumer
  scripts get a parseable failure. `AI_R_DEBUG=1` re-raises for debugging.
- **Codex plan steps/status**: `update_plan` carries its step array under the
  `plan` key; the parser read a non-existent `steps` key, so every Codex plan
  surfaced `steps=null`/`status=null`. Now read from the correct key.

### Docs

- **LLM e2e acceptance scenarios**: `docs/scenarios.md` — 30 scenarios across
  the public surface, framed into the READMEs via `<!-- scenarios:start/end -->`.

### Scope / known limitations

- **Single-plan, no subagent tree**: this release does NOT implement
  subagent-tree filtering. The `query` facets `kind`/`parent`/`group` are
  reserved and rejected (fail-loud) rather than silently ignored. Their
  absence is a deliberate scope boundary, not a gap.
- **Confidence tags dropped**: the earlier `EXTRACTED`/`INFERRED`/`AMBIGUOUS`
  confidence-tag idea was replaced by reference-by-default (references are
  exact; bodies are pulled on demand), and is not planned.

## [0.2.0] - 2026-06-23

### Added

- **MCP `find_file_edits`**: new tool — find every file edit across
  sessions for a given path, cross-agent by default, optionally
  time-boxed with `since`/`until` (ISO 8601). Raises the MCP tool count
  from 3 to 4 (`list_sessions`, `read_session`, `search_sessions`,
  `find_file_edits`). See [docs/architecture.md](./docs/architecture.md).
- **CLI `ai-r find-file-edits`**: mirrors the MCP tool with
  `--agent`, `--since`, `--until`, `--limit`.
- **MCP `search_sessions`**: extended with `scope`, `operator`, `limit`
  parameters and a Google-style `-term` negative prefix in the query.
  The default values (`scope="title"`, `operator="AND"`, `limit=50`)
  preserve the historical title-substring behaviour, so existing
  callers are unaffected. New abilities:
  - `scope="body"` searches message text + `tool_use[*].input` +
    `tool_result[*].content` across every session.
  - `scope="all"` searches title OR body.
  - `operator` accepts `AND` (default), `OR`, or `NOT` for the
    combination of positive terms. Negative `-term` tokens are
    always excluded regardless of operator.
  - Quoted phrases (`"exact phrase"`) are supported via `shlex.split`.
  - When a `body`/`all` search hits, the result includes a `snippet`
    field with the first matching excerpt (up to 200 chars).
- **CLI `ai-r search`**: mirrors the new parameters as
  `--scope {title,body,all}` and `--operator {and,or,not}` (alias
  `--op`). Validation of `--limit` matches the MCP tool. Date
  filters (`--days`/`--from-date`/`--to-date`) compose with the
  new flags.
- **Tests**: added MCP/CLI coverage for backward-compat, all three
  operators, negative prefixes, quoted phrases, tool-call matching,
  snippets, `limit`, and validation error paths.

### Changed

- **claude**: `extract_title()` now uses the **first** user message instead
  of the last. Affects downstream summaries or UI that quoted the wrap-up
  turn. If you depended on the old behavior, filter `Session.title` against
  the original last user message via `read_session(...).messages[-1].content`
  until a migration shim is shipped.

### Security

- **codex**: `AI_R_DEDUP_KEY_LEN` is now re-read from the environment on
  every dedup-key call. Previously the value was captured at import time, so
  any runtime change to the environment (operator re-export, test using
  `monkeypatch.setenv` after import, long-running service restart) was
  silently ignored. New `parsers.codex.get_dedup_key_len()` accessor exposes
  the resolved value for callers that want to introspect it.

## [0.1.0] - 2026-06-14

First public alpha release.

### Added

- **Parsers** for 5 agents:
  - `claude` — JSONL at `~/.claude/projects/<project-slug>/<uuid>.jsonl`
  - `codex` — JSONL at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
  - `opencode` — SQLite at `~/.local/share/opencode/opencode.db` (auto-detects snap/flatpak variants under `~/snap/code/*/...` and `~/snap/opencode/*/...`)
  - `antigravity` — brain directories at `~/.gemini/antigravity/brain/` and `~/.gemini/antigravity-cli/brain/`
  - `pi` — JSONL at `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl`
- **`Session` data model** with `uuid`, `agent`, `title`, `date`, `path`, `message_count`, `parent_uuid`, `extra`
- **CLI** (`ai-r`): `list`, `read`, `search` subcommands with `--agent` filter and `--json` output
- **MCP server** (`ai-r-mcp`): 3 tools — `list_sessions`, `read_session`, `search_sessions`
- **install.sh**: idempotent, dual-mode (system-wide with sudo, or per-user), venv or `--break-system-packages` fallback
- **agent-configs.sh**: patches agent MCP configs (claude, codex, opencode, antigravity)
- **uninstall.sh**: clean removal of binaries and MCP entries
- **Tests**: 184 tests, 87% coverage
- **2-layer architecture**: Public API / Core parsers — a read-only reader with no access-control layer in front of the parsers
- **MIT license**

### Notes

- This is an **alpha**. APIs may change before `0.2.0`.
- `ai-r` is a reader, not a guard. Any caller that can reach the CLI, the MCP server, or the package can read any session. See [docs/architecture.md](./docs/architecture.md).
