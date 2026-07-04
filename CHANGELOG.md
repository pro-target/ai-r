# Changelog

All notable changes to this project are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
