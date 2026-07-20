# ai-r — LLM e2e acceptance scenarios (SSOT)

> Single source of truth for **LLM-driven end-to-end acceptance scenarios** of the ai-r service.
> These are natural-language scenarios an LLM agent executes against the **live MCP tools**
> (`mcp__ai-r__*`) on a real vault, to validate the whole public surface. They **complement** the
> Python pytest suite (`tests/`): pytest proves the internals byte-for-byte and hermetically; these
> scenarios prove the *deployed* MCP surface behaves correctly and semantically end-to-end.
> English SSOT. READMEs link to this file; the summary table below is the in-doc menu. Update on every functionality change.

## How to run

An LLM agent runs each scenario by **calling the MCP tools** listed in *Steps* against a live ai-r
server (real `~/.claude`, `~/.codex`, … vault, unless the scenario is marked `[hermetic-ok]`), then
checks the **semantics** of the result — not merely "no error was raised". The agent inspects shapes,
field presence/absence, ordering, cross-checks one tool against another, and confirms the *meaning*
(e.g. "this is the preceding user turn", "this file order is chronological"). A scenario that returns
data but with the wrong shape/order/semantics is a **failure**, not a pass.

## Pass / fail convention

Each scenario resolves to one of:

- **GO** — every *Pass criteria* item holds; the surface behaves exactly as specified.
- **GO-with-caveats** — the core behaviour holds, but a documented, expected limitation applies
  (e.g. a known blind spot such as `tee`/`sed -i` in codex `session_diff`, or a degenerate `kind`
  split on a vault with no subagents). The caveat MUST match a limitation already documented here or
  in `docs/methods.md`; an *undocumented* deviation is NO-GO.
- **NO-GO** — a *Pass criteria* item fails: wrong shape, wrong ordering, a body leaked when a
  reference was expected, a silent result where a fail-loud error was required, or a semantic error.

## Legend

- `[hermetic-ok]` — the scenario runs on synthetic or empty data and needs no host vault; it is
  reproducible anywhere (including empty `HOME`).
- `[needs-real-vault]` — the scenario needs a live vault (`~/.claude`, `~/.codex`, …) with real
  sessions; on a bare host it is **skipped, not failed** (mirrors the pytest host-marker convention).
- Scenarios with no tag are `[hermetic-ok]` by default.


## Acceptance summary

109 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| [`query`](#query) | 15 | Facet filters return correct event shape (references, no body inlined — `text` is a ~160-char preview, a real cut flagged `text_truncated: true`, full body via `get_body`); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; `tool_call` events carry an `is_error` outcome (cross-agent best-effort) without changing counts; every `tool_call` event carries a wrapper-aware `tool_kind` (`edit\|write\|read\|bash\|task\|skill\|mcp\|web\|other`) and — when the wrapper's input names the real actor — `tool_resolved` (subagent type under Task/Agent/spawn_agent, skill name under Skill/SlashCommand, `server:tool` under `mcp__*`; no signal → no field, never guessed), the `tool_kind` facet filters by it (unknown value fails loud) and the `tool` facet also matches resolved names; session-level `noise=exclude\|include\|only` drops/isolates subagent sessions before any message is read, an unknown mode fails loud; the `parent` facet scopes to a session's subagent subtree (transitive `parent_uuid` descendants, root excluded, per-agent; unknown uuid → honest empty) and the `group` facet scopes `plan_event`s to one plan-task `task_id` (non-plan events never match) — the former `kind` facet was removed as a duplicate of `noise` (`noise="exclude"`≡top-level, `noise="only"`≡subagents) and now fails loud pointing at `noise` (a fail-loud tombstone, so the transport never silently drops it into an unfiltered scan); session-level `project_dir` filter scopes events to one project (exact-or-descendant, path-boundary aware); the `session` facet accepts a single uuid OR a list of uuids — the union of those sessions' events in one call (duplicates collapse, an unknown uuid contributes nothing, an empty list or non-string item fails loud — never a silent full-corpus scan). |
| [`get_body`](#get_body) | 6 | Body fetched on-demand by id (turn text / plan text / codex steps / a `tool_call` id → its full call `input` under `body`, the same payload `find_file_edits` references as `input_sha256`, reproducing its sha256/length); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated; a plan-feedback `ref` (`"<session>:pf<N>"`) resolves to the FULL raw plan response (type `plan_feedback`, redacted, capped), out-of-range/unknown refs are `not_found`. |
| [`aggregate`](#aggregate) | 6 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash; the `tokens` metric folds per-row `tokens` blocks into `{input, output, reasoning, cache_read, cache_write, total, exact, estimated, unknown}` — sums stay `null` when no row carried the field (never a fabricated 0) and `exact + estimated + unknown == len(rows)` always holds; `group_by="model"` over query rows buckets events by the producing model (rows without a model signal fold into the honest `"(unknown)"` bucket, never a guessed model). |
| [`diff`](#diff) | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand; the MCP response is size-bounded exactly like `session_diff` (shared cap, per-file `truncated_fields` + `output_truncated`). |
| [`detect_current`](#detect_current) | 2 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`); carries a process-`liveness` verdict (`fresh`/`paused`/`zombie`/`dead`, honest `null` without a pid signal — Claude only). |
| [`plan`](#plan) | 12 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by **file (append) order** (`seq`), so non-monotonic timestamps still bind body/steps/version to the right revision and `plan` agrees with `get_body`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal; F3.4 default schema — the **final** plan's full text inlined (`body` + `body_source`, the user-edited approval text is authoritative over the signal/file body; honest `null` for steps-only plans), drafts stay references, and `feedback` carries ALL «plan quote → user comment» pairs (chronological, `plan_id`-bound, `verdict ∈ rejected\|stay_in_plan_mode`, `quote=null` for free-text comments, raw response on-demand via `ref`); technical failures filtered; agents without an approval flow contribute an honest empty `feedback`; `bodies="none"`/`feedback=false` restore the historical shape; v2 — every atom carries `version` (v1…vN per task, chronological, final = vN), every pair carries `plan_version` + `round` + `section` (the quote anchored to its source-markdown section through markup-stripping normalization — miss or multi-section ambiguity is an honest `null`, never a nearest guess; a rejected plan-file `Write` correlates like an `ExitPlanMode` verdict), and `rounds=all\|last` filters to each session's final feedback round (unknown value fails loud). |
| [`session_stats` (preset)](#session_stats-preset) | 5 | All 5 dims (agent/dir/date/kind/model) give sensible counts — `model` buckets sessions by the model that produced them, with `"(mixed)"` for a session that used several and `"(unknown)"` without a signal (neither guessed); degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot; `with_tokens=true` (F3.3) reads token usage at request time and adds a folded `tokens` block to every group + totals — **exact** where the agent's files record usage (Claude `message.usage` deduped per API call, Codex last cumulative `token_count`, OpenCode `message.data.tokens`, Pi `usage`), a labeled `estimate` otherwise (optional tiktoken, else a rough chars/4 heuristic — degradation, never a crash), honest `unknown` without any signal; default `false` is byte-identical to the historical output. |
| [`session_diff` (preset)](#session_diff-preset) | 3 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped; MCP output is size-bounded like `find_file_edits` (over-long `intent`/hunk bodies/per-file `diff` cut with `…[truncated]` + named in the per-file `truncated_fields` as indexed paths, whole-file emission stops at a byte budget → `output_truncated` while `count` keeps the true total; full body on demand via `get_body`/`read_session`) — the `diff` verb shares the same bound. |
| [`incidents` (preset)](#incidents-preset) | 4 | One call finds dangerous shell commands + regret reactions (F4.1) via a baked chain — ONE `query(type=tool_call, tool_kind=bash)` scan → deterministic danger dictionary on the extracted command (a Bash `description` alone never fires; `--force-with-lease` is not force-push) → bilingual (ru+en) regret-marker scan over the next `reaction_window` messages (default 6) — the two-step `confirmed` verdict, never guessed; each record carries the query event `id` (context on-demand via `relative_to`), `patterns`+`categories`, a char-capped `command` fragment centred on the hit, tri-state `is_error` (`null` where the agent's format has no correlated outcome signal) and `reaction` (marker labels + capped preview, `null` when unconfirmed); `count`/`confirmed_count`/`by_pattern` reflect the FULL match set independent of `limit`; `category`/`confirmed` filters fail loud on unknown values; emitted fields are redacted by default while matching runs on RAW text; zero incidents → `diagnostics`; documented dictionary caveat: quoting a dangerous string (echo/grep/test payloads) can still match — mention vs execution is not decidable by regex. |
| [`network` (preset)](#network-preset) | 4 | One call audits network egress (F4.3) via a baked chain — ONE `query(type=tool_call, tool_kind=web)` scan (Claude `WebFetch`/`WebSearch`, OpenCode `webfetch`, Codex `web_search` surfaced from `web_search_call` rollout records, Gemini/Antigravity `web_fetch`/`google_web_search`; Pi records no web tool — honest absence) → the request target (`url`/`query`) extracted from the call's own input (never guessed from the tool name; no target → honest `null` fields, `kind: null`) → a deterministic **risk dictionary** (`plain_http`, `credentials_in_url`, `secret_in_url`/`secret_in_query` — the redaction patterns double as the detector, `ip_literal_host`, `private_or_local_host`, `punycode_host`); each record carries the query event `id` (context on-demand via `relative_to`), derived `kind` (`fetch`\|`search`), char-capped `url`/`query`, `domain`, `risks` and tri-state `is_error`; `count`/`risky_count`/`by_domain`/`by_risk` reflect the FULL match set independent of `limit`; `kind`/`risk` filters fail loud on unknown values, `domain` matches equals-or-subdomain; risk assessment runs on RAW strings while emitted fields are redacted by default (cap applied AFTER redaction — a boundary-sliced secret never leaks); zero requests → `diagnostics`; documented caveat: MCP-mediated network access stays under `tool_kind="mcp"` — never guessed into the audit. |
| [`quotes` (preset)](#quotes-preset) | 3 | One call finds «user quote → user comment» pairs (F5.2) cross-agent — when a user selects a chunk of a prior message and comments on it, the quoted text is embedded VERBATIM in their turn (no client records a marker), so `query(type=user_turn)` commenters are matched against `query(type=assistant_turn)` sources: each user turn's full text (`read_messages`) is diffed against its preceding assistant turns with `difflib.SequenceMatcher` over normalized text (`_normalize_rendered_text`), the longest run ≥40 chars is the quote and the rest is the comment; each record carries the user_turn `id`, `source_id` (the quoted turn), `source_kind`, `quote_chars`, char-capped `quote`+`comment`; `count`/`by_source_kind` reflect the FULL set independent of `limit`; `source_kind` fails loud on unknown; emitted fields redacted by default; external paste (no in-session source) → no quote; zero quotes → `diagnostics`; the cross-agent generalization of `plan(feedback)` beyond Claude plans. |
| [`audit_brief` (preset)](#audit_brief-preset) | 2 | One call = a token-lean, budgeted session digest for auditors (stage 4) via a baked chain — ONE `query(session=…)` scan → user turns VERBATIM (the auditor's ground truth, NEVER truncated by the budget) + tool footprint folded by `aggregate(group_by=tool_kind)` (counts + notable `is_error` rows, not dumps) + file footprint from the edit/write rows' `file` refs + the `plan`/`plan_feedback` decision trail + the `ai_r.tokens` breakdown; deterministic budget ladder on the ACTUAL serialized JSON (`budget_chars`, default 15000, `0`=unlimited): (1) tool-error details → (2) per-file list → (3) plan bodies/feedback texts, counts/references always stay; still over after the full ladder ⇒ honest `budget.over_budget: true` + note naming the full projections; unknown session → `not_found`; bad args fail loud; emitted title/user texts/plan bodies redacted by default. |
| [`locate` (preset)](#locate-preset) | 2 | One call finds a session across all agents by full uuid, id-prefix (8-hex head) or case-insensitive title substring — a thin preset over the per-parser `list_sessions` inventory (zero new scanning code): matches ranked by last activity (mtime) desc, each carrying path/agent/`project_dir`/date/`size_bytes`, the honest `readable` local-content claim (`false` for a reference-only stub) and the ready-to-run `read_command` (`ai-r read <uuid> --agent <agent>`) + `resume_command`; `limit` bounds the list while `count` keeps the full total; zero matches → honest empty + closest-title `suggestions` + `diagnostics`, never a fabricated match; `web=true` (v1 honest scope) adds ONLY locally-known web traces — `$SW_HOME/web-sessions` hook exports (readable files) and `~/.claude.json` teleport stubs (`content_local: false` — id known, transcript NOT local); the per-repo teleport-picker sweep is a documented PTY follow-up, not guessed. |
| [`find_file_edits`](#find_file_edits) | 4 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`; records are size-bounded like `find_tool_calls` (`intent`/`assistant` capped with `…[truncated]` + per-record `truncated_fields`, total byte budget → `output_truncated`; the opt-in full `input` body is never field-capped) and a fully-unscoped call (no `agent`/`since`/`until`) is narrowed to the last 7 days with `default_since` + `note` in the response. |
| [`list_sessions`](#list_sessions) | 6 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid` (subagent detection: Claude/OpenCode/Codex/Pi; Antigravity has no signal); `agent` filter narrows the set; `noise=exclude\|include\|only` splits the inventory into top-level vs subagent sessions and composes with `kind` by AND; the Claude parser merges the CLI transcript root with the Claude Desktop metadata root — dedup by uuid, Desktop title wins (CLI title kept in `extra["cli_title"]`), origin marked `extra["source_root"]="cli"\|"desktop"`, a metadata-only session stays visible as a zero-message reference; each summary carries top-level `project_dir`+`launch_surface` (null when the format has no signal) and `project_dir` filters the inventory exact-or-descendant; each summary also carries the A3 recency signal `last_activity`+`age_sec`+`activity` (`fresh`/`stale` vs `AI_R_STALL_SEC`, default 600s) — record recency only, never a process-liveness claim; the honest process verdict is the separate `liveness` field (`fresh`/`paused`/`zombie`/`dead` fused from the `claude agents` pid registry + `/proc`, `null` when no pid signal — non-Claude sessions always `null`). |
| [`outcome` (read_session field)](#outcome-read_session-field) | 2 | `read_session` carries `outcome` — `status ∈ success\|failure\|mixed\|unknown` from two honest signals: tool-call error rate (real flag only for Claude/OpenCode — `tool_errors`/`error_rate` are `null` elsewhere, `error_rate_reliable` says which) and a calibrated bilingual (ru+en) success/failure dictionary over the last 3 *human* user turns (assistant self-reports never trusted); every deciding reason spelled out in `signals` (empty ⇔ `unknown`); no raw session text in the block; nothing guessed — no signal is `unknown`, never a fabricated verdict. |
| [`resume_command` (summary field)](#resume_command-session-summary-field) | 2 | Every session summary carries `resume_command` — the ready-to-run CLI one-liner (`cd <project_dir> && claude --resume <uuid>` / `codex resume <uuid>` / `opencode --session <id>` / `pi --session <path>`), shell-quoted, `cd`-prefixed when `project_dir` is known; `null` exactly where no real command exists (Antigravity, subagent sessions, reference-only Desktop sessions) — text only, never executed. `detect_current` reports the current session's `resume_command`; the CLI `ai-r list --json` summary carries the same field. |
| [`find_tool_calls`](#find_tool_calls) | 5 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; the name is optional when a content filter carries the selection, but setting **both** names OR passing **no filter at all** **fails loud** (`invalid_argument`), never a silent empty result; each record surfaces the correlated `is_error` outcome + char-capped `output` (authoritative for Claude/OpenCode, best-effort elsewhere) + `is_error_reliable`; each record also carries the wrapper-aware `tool_kind` + `tool_resolved` (the real name under a Skill/Task/MCP wrapper, `null` without a signal); `input_contains`/`output_contains`/`output_excludes`/`is_error` filters compose by AND (domain × error without a special verb); adaptive `output_mode` (`smart` for errors) keeps a trailing error line that `head` would drop. |
| [`read_session`](#read_session) | 5 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices; `agent` is **optional** — an id resolves across every parser, a rare cross-agent id collision returns a `candidates` list (not an error), a miss names `agents_scanned`; `with_tokens=true` (F3.3) attaches `tokens` (flat exact-or-estimate) + `component_tokens` — a per-component estimate over ai-r's existing event taxonomy (`user_turn`/`assistant_turn`/`thinking`/`plan`/`tool_call.<kind>`, `total`, always `source="estimate"`, plan-authoring calls under `plan` not `tool_call`, `total == sum(scalars)+sum(tool_call.values())`, empty transcript → `null`) plus per-message EXACT `tokens` blocks where the format records per-message usage (Claude deduped per API call before pagination, OpenCode, Pi; Codex/Antigravity/user turns carry no key — absent, not null); each message also names its producing `model` where the format records one (absent — never null — for user turns / `<synthetic>` stubs / no-signal formats); `include_subagents=true` attaches `subagent_rollup` (parent + one child per spawned subagent via `children_of(parent_uuid)` + folded `total`); CLI `ai-r read --with-tokens` prints a `COMPONENT \| TOKENS \| SOURCE` table; integers-and-labels only → outside redaction; default `false` is byte-identical to the historical output. |
| [`search_sessions`](#search_sessions) | 4 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort; `noise=exclude` removes subagent matches before scanning, `noise=only` searches only the subagent tree. |
| [empty-result diagnostics (cross-cutting)](#empty-result-diagnostics-cross-cutting) | 2 | A zero-result `query`/`search_sessions`/`find_tool_calls`/`find_file_edits`/`list_sessions` response carries `diagnostics` (per-agent scan counts + date bounds + `source_found`, corpus totals, cause hints: missing source dir / all-excluding `since`/`until` / remaining filters); a non-empty response never carries it. |
| [secret redaction (cross-cutting)](#secret-redaction-cross-cutting) | 5 | Every text-emitting method masks secrets on output as `[REDACTED_<TYPE>]` by default and carries a `redactions` type→count dict; `redact=false` returns the raw content; matching always runs on the RAW stored text (searching a literal secret finds its session, only the display is masked); a `[REDACTED_*]` placeholder or secret-looking filter value on an empty result earns a diagnostics hint suggesting `redact=false`; vendor formats an `sk-`/`AWS`-only table missed — Stripe `sk_`/`rk_`, `eyJ…` JWT, Google `AIza…` — are masked with their own type labels without over-masking look-alikes; and the **CLI** honours the same default (`ai-r read`/`export`/`list` mask on output, `--no-redact`/`--raw` opts out) so a secret never leaks through a default CLI print. |
| [MCP transport auth (cross-cutting)](#mcp-transport-auth-cross-cutting) | 2 | The opt-in shared `streamable-http` transport carries access control the stdio path does not need: the `mcp` SDK's DNS-rebinding/Origin allowlist (always on for the loopback default) plus an opt-in bearer token (`AI_R_HTTP_TOKEN`, constant-time compared) — a missing/wrong token is a `401` that never reaches a tool, a correct token passes through; a non-loopback bind with no token is a **fail-closed hard refusal** naming `AI_R_HTTP_TOKEN`, while loopback-without-token still runs behind the rebinding allowlist. |
| [semantic sort (cross-cutting)](#semantic-sort-cross-cutting-f51) | 3 | `sort="semantic"` on the text-search surface (`query` text facet, `search_sessions`) re-ranks the BM25 top-50 candidates by meaning with a local multilingual embedding model (`intfloat/multilingual-e5-small`, int8 ONNX, direct onnxruntime+tokenizers, mandatory `query:`/`passage:` prefixes applied internally, no persistent index); blended candidate score = 75 % meaning + 25 % word match (min–max normalized within the pool), no similarity cut-off — results are re-ordered, never dropped, the tail keeps BM25 order; the response carries a `semantic` report (`active: true` + model/candidates/weight, or `active: false` + plain-words `reason` + `fallback: "bm25"`); without the optional `ai-r[semantic]` deps/model files the order honestly falls back to plain BM25 — never a crash — and the default sorts never touch the module; cross-lingual ru↔en retrieval works both ways. |
| [CLI error contract](#cli-error-contract) | 1 | A failing `ai-r` CLI invocation exits non-zero with a structured error on stderr (single `ai-r: …` line, or one JSON `internal_error` line for unexpected failures) — never a Python traceback; `AI_R_DEBUG=1` re-raises for debugging. |
| [unknown-argument fail-loud (cross-cutting)](#unknown-argument-fail-loud-cross-cutting) | 1 | An undeclared tool argument is rejected with `invalid_argument` (naming it, listing the accepted parameters) **before** the tool runs — the transport would otherwise silently drop it and return an unfiltered result; a fully-declared call passes through untouched. |
| [subagent cost (cross-cutting)](#subagent-cost-subagent-sidecar--exact-child-tokens) | 2 | A spawn is priced by the model it ACTUALLY resolved to: `find_tool_calls` carries `tool_use_id` + a `subagent` sidecar (`model` / `agent_type` / `status` / `duration_ms` / `tool_uses` and, on a completed spawn, `tokens` with `source="exact"`, never an estimate) — a persona pinned to a cheaper tier reports the pinned model, not the parent's; a background spawn (`status="async_launched"`, sidecar written before the run exists) reports a model with NO token block (absence, never a fabricated zero). Opt-in `with_subagent_cost=True` JOINS each spawn to the child's own files (persona, `models`, exact `tokens`, `child_uuid`) so background spawns become named + priced too. `read_session(include_subagents=True)` → `subagent_rollup.children` reports each child's cost on an HONEST `source` ladder (exact where the child's transcript records usage, labeled estimate / `null` where it does not) — never a fabricated exact or zero. |


---

## `query`

The workhorse verb: filters the unified, agent-neutral event stream
(`user_turn` / `assistant_turn` / `tool_call(<sub>)` / `plan_event`) by facets. All behaviour is
parameters. Events carry **references** (`refs`), never inlined bodies: the emitted `text` is a
~160-char preview (a real cut ends with `…` and sets `text_truncated: true`); the full body is
fetched on demand via `get_body(id)`.

<details>
<summary>Show 16 scenarios (QRY-1…QRY-16)</summary>

### QRY-1 — filter by agent + type
- **Function:** `query`
- **Goal:** A facet-filtered listing returns the correct event shape with no body inlined.
- **Preconditions:** A vault with at least one `claude` session. `[needs-real-vault]` for non-empty output; `[hermetic-ok]` for the empty-vault shape check.
- **Steps:** `mcp__ai-r__query(agent="claude", type="user_turn", limit=20)`.
- **Expected:** `{events:[…], count:N}`; every event has `type == "user_turn"`, an `id`, a timestamp, and `refs`; no event carries a full `body`/`text` payload inlined — `text` is a preview cut to ~160 chars (applied **after** redaction); a real cut ends with `…` and sets `text_truncated: true` (flag absent on short texts).
- **Pass criteria:** GO when all returned events match `type` and `agent`, each has an `id` usable by `get_body`, and no message body is inlined in the event (a long text arrives as a flagged `…`-preview; `get_body(id)` returns it whole).

### QRY-2 — filter by session → chronological single session
- **Function:** `query`
- **Goal:** Restricting to one session returns only that session's events, in chronological order.
- **Preconditions:** A known session uuid. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__query(session="<uuid>", limit=0)`.
- **Expected:** All events belong to `<uuid>`; timestamps are non-decreasing (ascending, `sort=date` default).
- **Pass criteria:** GO when every event is from the one session and the sequence is chronologically ordered.

### QRY-3 — cross-agent (codex) same shape
- **Function:** `query`
- **Goal:** A different agent's events normalize into the *same* event shape.
- **Preconditions:** A vault with `codex` sessions. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__query(agent="codex", type="user_turn", limit=20)`.
- **Expected:** Same `{events, count}` contract as QRY-1; each event has `type == "user_turn"`, `id`, ts, `refs`; agent differences are hidden by normalization.
- **Pass criteria:** GO when codex events are shape-identical to claude events (only values differ), confirming cross-agent unification.

### QRY-4 — intent walk (`relative_to`, `direction=prev`)
- **Function:** `query` (the `intent` preset expansion)
- **Goal:** The preceding user turn of a given event is returned and matches the real transcript.
- **Preconditions:** A known event `id` (e.g. a `tool_call` from QRY-1/QRY-2). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__query(relative_to="<event-id>", direction="prev", n="1")`; then cross-check with `mcp__ai-r__read_session(<uuid>)`.
- **Expected:** Exactly one `user_turn` — the turn that immediately precedes `<event-id>` in the stream.
- **Pass criteria:** GO when the returned turn is the same user message that precedes the event in `read_session` (semantic cross-check, not just "one event returned").

### QRY-5 — reaction walk (`direction=next`)
- **Function:** `query` (the `reaction` preset expansion)
- **Goal:** The following turn after a given event is returned.
- **Preconditions:** A known event `id`. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__query(relative_to="<event-id>", direction="next", n="1")`.
- **Expected:** Exactly one `user_turn` — the turn immediately *after* `<event-id>`.
- **Pass criteria:** GO when the returned turn is the next user turn in transcript order (cross-checked vs `read_session`).

### QRY-6 — text search, `sort=relevance` (BM25)
- **Function:** `query`
- **Goal:** Free-text search returns BM25-ranked results with a meaningful top hit.
- **Preconditions:** A vault whose sessions contain a distinctive term. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__query(text="<distinctive term>", sort="relevance", limit=10)`.
- **Expected:** Survivors are ranked by BM25 (same scorer as `search_sessions`), not by date; the top event is genuinely the most relevant to the term.
- **Pass criteria:** GO when the top-ranked event is clearly the strongest textual match (relevance ordering, not chronological).

### QRY-7 — `parent` facet → a session's subagent subtree
- **Function:** `query`
- **Goal:** `parent=<uuid>` returns the events of every session spawned under `<uuid>` (transitive `parent_uuid` descendants — direct children AND nested), with the `<uuid>` session itself excluded.
- **Preconditions:** A vault (or hermetic seed) with a session that spawned subagents, ideally nested (a child that itself spawned a grandchild). `[hermetic-ok]` (subagent fixtures).
- **Steps:** `mcp__ai-r__query(agent="<a>", parent="<root_uuid>")`; also removed-`kind` sanity: `mcp__ai-r__query(kind="subagent")` must return `{error:"invalid_argument"}` naming `noise` (the facet was removed; it survives only as a fail-loud tombstone so the transport never silently drops it — `noise="only"` is the supported way to isolate subagents).
- **Expected:** Events come only from descendant sessions of `<root_uuid>` (direct + nested), none from `<root_uuid>` itself; passing `kind` returns the `invalid_argument` error pointing at `noise`, never a silent (unfiltered) events list.
- **Pass criteria:** GO when the returned session set equals the transitive descendant set (root absent) and `kind=` returns the loud `noise`-pointing error. A missing nested grandchild, `<root_uuid>`'s own events leaking in, or `kind` silently returning events is NO-GO.

### QRY-8 — `tool_call` events carry an `is_error` outcome (cross-agent best-effort)
- **Function:** `query`
- **Goal:** A `tool_call` event surfaces whether the call succeeded or failed, without changing the bare `tool_call` filter/counts.
- **Preconditions:** A claude (or opencode) session containing at least one FAILED tool call — e.g. a `Bash` that exited non-zero or an errored tool. For Claude this includes failures recorded **without** the explicit `is_error` flag (a `tool_result` whose content starts with `<tool_use_error>`, or a record-level `toolUseResult: "Error: …"`), which the parser derives as errors. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__query(agent="claude", type="tool_call", session="<uuid>")`; inspect `is_error` on the events; cross-check the failed one against `read_session` (it should render `[tool_result ERROR: …]`).
- **Expected:** `tool_call` events carry an `is_error` ref — `True` for the known-failed call, `False`/absent for succeeded ones; the bare `type="tool_call"` filter still returns EVERY tool call (the outcome ref does not add/drop events or change `count`).
- **Pass criteria:** GO when `is_error` reflects the real outcome for Claude/OpenCode and the bare `tool_call` count is unchanged by the ref. Codex/Pi always reporting `is_error=False` (no source flag) and Antigravity emitting no tool results are **documented** cross-agent limitations (see `docs/methods.md` → *Output bounds & tool-call outcome*), not failures.

### QRY-9 — session-level `noise` filter (subagent sessions)
- **Function:** `query`
- **Goal:** `noise=exclude` drops every event coming from a subagent session; `noise=only` returns exclusively those; an unknown mode fails loud.
- **Preconditions:** One top-level session + one subagent session for the same agent (any of claude/codex/opencode/pi). `[hermetic-ok]` (seed a fake parent + subagent pair under `AI_R_HOME`).
- **Steps:** `mcp__ai-r__query(agent="<agent>")` (default `noise="include"`); then the same call with `noise="exclude"`; then `noise="only"`; then `noise="bogus"`.
- **Expected:** `include` returns events of both sessions; `exclude` returns only events whose `session_id` is the top-level session; `only` returns only the subagent session's events; `set(exclude) ∪ set(only) == set(include)` and the two are disjoint; `noise="bogus"` returns `{"error": "invalid_argument", …}` naming `noise`.
- **Pass criteria:** GO when the three modes partition the event stream exactly by session kind and the unknown mode is a loud error, never a silently unfiltered result.

---

### QRY-10 — session-level `project_dir` filter (events of this project)
- **Function:** `query`
- **Goal:** `project_dir` keeps only events of sessions whose `project_dir` equals the given path or is a descendant of it (path-boundary aware); sessions without a signal never match; an empty value fails loud.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` three Claude sessions with record-level `cwd` values `/home/u/dev/x`, `/home/u/dev/x/sub` and `/home/u/dev/xy`.
- **Steps:** `mcp__ai-r__query(agent="claude", type="user_turn", project_dir="/home/u/dev/x")`; then `project_dir="/nowhere"`; then `project_dir=""`.
- **Expected:** The first call returns only events of the `/home/u/dev/x` and `/home/u/dev/x/sub` sessions — the sibling `/home/u/dev/xy` is excluded (prefix ≠ subpath); `/nowhere` returns `count=0` with `diagnostics.filters.project_dir` echoed; `""` returns `{"error": "invalid_argument", …}`.
- **Pass criteria:** GO when descendant sessions are included, the path boundary excludes the sibling, absence of signal never matches, and the blank filter is a loud error.

### QRY-11 — wrapper-aware `tool_kind` + real names under wrappers (`tool_resolved`)
- **Function:** `query`
- **Goal:** A `tool_call` event exposes WHAT actually ran under a Skill/Task/MCP wrapper: every tool call carries `tool_kind`, wrappers whose input names the real actor also carry `tool_resolved`; the `tool_kind` facet filters by kind and the `tool` facet finds calls by their resolved name.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` one Claude session with a `Task` call (`input.subagent_type="Explore"`), a `Skill` call (`input.skill="ai-local-reader"`), a `SlashCommand` call (`input.command="/commit -m fix"`), an `mcp__ai-r__query` call, a `WebFetch` call, a plain `Bash` call and one `Task` call WITHOUT `subagent_type`; plus one Codex rollout with a `spawn_agent` call (`arguments.agent_type="explorer"`). `[needs-real-vault]` variant: any real session with subagent spawns / MCP calls.
- **Steps:** `mcp__ai-r__query(agent="claude", session="<uuid>", type="tool_call")` — inspect `tool_kind`/`tool_resolved` on each event; then `tool_kind="task"`, `tool_kind="mcp"`, `tool="commit"`; then `tool_kind="banana"`; then `mcp__ai-r__query(agent="codex", session="<codex-uuid>", tool_kind="task")`.
- **Expected:** Every `tool_call` event carries `tool_kind` ∈ `edit|write|read|bash|task|skill|mcp|web|other` (in `refs` and hoisted top-level); `Task`→`("task","Explore")`, `Skill`→`("skill","ai-local-reader")`, `SlashCommand`→`("skill","commit")` (bare command token), `mcp__ai-r__query`→`("mcp","ai-r:query")`, `WebFetch`→`("web", no resolved)`, `Bash`→`("bash", no resolved)`; the signal-less `Task` keeps `tool_kind="task"` with NO `tool_resolved` (honest, never guessed); the codex `spawn_agent` resolves to `"explorer"`; the event `type` stays the base subtype (a Task call is still `tool_call(other)` — counts/filters unchanged); `tool_kind="banana"` → `{error:"invalid_argument", …}` naming `tool_kind`; `tool="commit"` returns the SlashCommand event (resolved-name match).
- **Pass criteria:** GO when kinds and resolved names match the table above for BOTH agents, the no-signal wrapper carries no `tool_resolved`, the unknown kind fails loud, and pre-existing `type="tool_call"` counts are unchanged by the new refs.

### QRY-12 — `session` accepts a list of uuids (session batch)
- **Function:** `query`
- **Goal:** Passing a **list** of session uuids returns the union of those sessions' events in one call; the scalar form is unchanged; an empty list fails loud.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` three Claude sessions with distinct uuids `<A>`, `<B>`, `<C>` (each with at least one user turn). `[needs-real-vault]` variant: any three known session uuids.
- **Steps:** `mcp__ai-r__query(agent="claude", type="user_turn", session=["<A>", "<B>"])`; then the scalar `session="<A>"`; then `session=["<A>", "<A>", "no-such-uuid"]`; then `session=[]`; then `session=["no-such-1", "no-such-2"]`.
- **Expected:** The list call returns exactly the events of `<A>` and `<B>` — `<C>` never leaks in — in chronological order across both sessions; the scalar call behaves exactly as before (backward compat); duplicates collapse and the unknown uuid contributes nothing (the result equals the scalar `<A>` call — honest empty-miss semantics, no error); `session=[]` → `{error: "invalid_argument", …}` naming `session` (an empty list is ambiguous, never a silent full-corpus scan); the all-unknown list returns `count=0` with the list echoed in `diagnostics.filters.session`.
- **Pass criteria:** GO when the union is exact (no third-session leak), the scalar form is unchanged, dedup + unknown-uuid semantics hold, the empty list is a loud error, and the zero-match diagnostics echo the list value.

### QRY-13 — `parent` unknown uuid → honest empty
- **Function:** `query`
- **Goal:** An unknown `parent` uuid is an honest empty result, never an error and never an unfiltered scan.
- **Preconditions:** none. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__query(parent="no-such-uuid")`; then the empty-string form `mcp__ai-r__query(parent="")`.
- **Expected:** The unknown uuid returns `{"events": [], "count": 0}` (with zero-result `diagnostics`), no `error`; the **empty string** fails loud (`{error:"invalid_argument", …}`).
- **Pass criteria:** GO when an unknown uuid yields `count=0` with no error and no leak, while the empty string is a loud argument error.

### QRY-14 — `group` facet → one plan-task's revisions
- **Function:** `query`
- **Goal:** `group=<task_id>` returns only the `plan_event`s of that plan-task (all its draft+final revisions), excluding other tasks.
- **Preconditions:** A session with ≥2 plan tasks (distinct `task_id`s). `[hermetic-ok]` (multi-task plan fixture) / `[needs-real-vault]` variant.
- **Steps:** read a `task_id` from `mcp__ai-r__plan(session="<s>")`; then `mcp__ai-r__query(type="plan_event", session="<s>", group="<task_id>")`.
- **Expected:** Every returned event is a `plan_event` whose `task_id` equals `<task_id>` (its draft + final revisions); a second task's plan_events are absent. `task_id` is derived from the SSOT plan grouping, not hard-coded.
- **Pass criteria:** GO when the result is exactly that task's plan_events and no other task's ids appear. Leaking another task, or dropping a revision of the requested one, is NO-GO.

### QRY-15 — `group` with a non-plan type → honest empty
- **Function:** `query`
- **Goal:** `group` is a plan-only facet; combined with a non-plan `type` it yields an honest empty result, not an error and not an unfiltered scan.
- **Preconditions:** none. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__query(type="user_turn", group="<any>")`; then the empty-string form `mcp__ai-r__query(group="")`.
- **Expected:** The non-plan `type` returns `{"events": [], "count": 0}` (no `plan_event` can match a group filter); the **empty string** fails loud.
- **Pass criteria:** GO when a non-plan type under `group` returns `count=0` with no error, and the empty string is a loud argument error.

### QRY-16 — `user_ref` facet + `group_by="user_ref_kinds"` aggregate (Q1) `[hermetic-ok]`
- **Function:** `query` (the `user_ref` dimension) + `aggregate`
- **Goal:** A `user_turn` carries a `user_ref` dimension for the files/urls/images/IDE-context the user attached; the `user_ref` facet filters turns by it (`any` / a kind / a `target` substring) and `aggregate(group_by="user_ref_kinds")` buckets turns by attachment kind — one dimension over the existing `user_turn` event, no second classifier.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` one Claude session with (a) a user turn with an `image` content-part (`origin="structured"`, no filename → `target=null`), (b) a user turn whose prose carries a `<doc path>` block and a bare `https://example.com/x` URL (`origin="text"`), (c) a user turn whose prose has a URL only inside a ```fenced``` code block, (d) a plain user turn with no attachment; plus one OpenCode session with a `file` content-part on the user role (`origin="structured"`, `target=<filename>`). `[needs-real-vault]` variant: any session where the user attached an image / pasted a doc path.
- **Steps:** `mcp__ai-r__query(agent="claude", type="user_turn", session="<uuid>", limit=0)` → inspect `user_refs`/`user_ref_kinds` on each event; then `mcp__ai-r__query(agent="claude", type="user_turn", user_ref="any")`; then `user_ref="image"`; then `user_ref="example.com"` (target substring); then `user_ref=""`; then `mcp__ai-r__query(agent="claude", type="tool_call", user_ref="any")`; finally collect the turns and `mcp__ai-r__aggregate(rows=<events>, group_by="user_ref_kinds", metrics=["count"])`.
- **Expected:** Turn (a) carries `user_refs=[{kind:"image", target:null, origin:"structured"}]` and `user_ref_kinds=["image"]`; turn (b) carries a `file`/`url` ref pair with `origin:"text"`; turn (c) carries NO url ref (a fenced-code URL is not an attachment); turn (d) carries no `user_ref` (base shape). `user_ref="any"` returns turns a+b (and the OpenCode file turn), never d; `user_ref="image"` returns only the image turn; `user_ref="example.com"` matches by `target` substring; `user_ref=""` → `{error:"invalid_argument", …}`; `type="tool_call", user_ref="any"` returns `count=0` (the facet matches only `user_turn`). The aggregate returns one bucket per kind (a multi-kind turn counts under EACH kind — the list value is exploded; the attachment-less turn carries no `user_ref_kinds` field and folds into the `(unknown)` bucket).
- **Pass criteria:** GO when each turn's `user_ref_kinds` matches its seeded attachments, the fenced-code URL is excluded, `origin` reflects structured-vs-text correctly, the facet matches only user turns and fails loud on the empty string, and the aggregate buckets a multi-kind turn under every kind (list exploded) while an attachment-less turn folds into `(unknown)`. A fabricated ref on a plain turn, a fenced-code URL surfacing as a ref, or the facet matching a tool_call, is NO-GO.

</details>

---

## `get_body`

Bodies are deliberately kept off the event stream; this verb fetches them on demand by id.

<details>
<summary>Show 6 scenarios (BODY-1…BODY-6)</summary>

### BODY-1 — turn text by id
- **Function:** `get_body`
- **Goal:** A `user_turn`/`assistant_turn` id resolves to its plain text.
- **Preconditions:** A turn `id` from `query`. `[needs-real-vault]` (or `[hermetic-ok]` with a synthetic session).
- **Steps:** `mcp__ai-r__get_body(id="<turn-id>")`.
- **Expected:** `{type:"user_turn"|"assistant_turn", text:"…"}` with the real turn text.
- **Pass criteria:** GO when `type` matches the source event and `text` is the actual message content.

### BODY-2 — plan body by id
- **Function:** `get_body`
- **Goal:** A `plan_event` id resolves to the full plan text.
- **Preconditions:** A plan `id` from `plan(...)`. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__get_body(id="<plan-id>")`.
- **Expected:** `{type:"plan_event", body:"…"}` with the plan's full text.
- **Pass criteria:** GO when the body is the plan text for that revision.

### BODY-3 — `shallow=true` on a draft id → final body + `dropped_drafts`
- **Function:** `get_body`
- **Goal:** Asking for a *draft* id with `shallow=true` returns the task's **final** plan and elides draft bodies (the S6 "subagent gets one clean plan" case).
- **Preconditions:** A task with ≥1 draft + 1 final. `[needs-real-vault]`.
- **Steps:** get a draft id via `mcp__ai-r__plan(session="<uuid>", kind="draft")`; call `mcp__ai-r__get_body(id="<draft-id>", shallow=true)`.
- **Expected:** The returned `id` is the task's **final** plan id; `body` is the final revision's text; `dropped_drafts` lists every elided draft id.
- **Pass criteria:** GO when `id == final.id`, the body is the final plan, and `dropped_drafts` covers all draft ids (no draft body surfaced).

### BODY-4 — codex plan steps/status populated `[needs-real-vault]`
- **Function:** `get_body`
- **Goal:** Regression guard — a codex plan's `steps`/`status` are carried through (codex `update_plan` nests them under the `plan` key).
- **Preconditions:** A codex session with `update_plan`. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<codex-uuid>", agent="codex", kind="final")` → take `id`; `mcp__ai-r__get_body(id="<id>")`.
- **Expected:** `status` is set (e.g. `"completed"`) and `steps` is a non-empty list, each step with its own `status`.
- **Pass criteria:** GO when `steps` is populated and `status` is present — proving the `plan`-key nesting is parsed, not dropped.

### BODY-5 — raw plan response by feedback ref (F3.4)
- **Function:** `get_body`
- **Goal:** A `ref` from a `plan` feedback pair (`"<session>:pf<N>"`) resolves to the FULL raw user response the pair was extracted from.
- **Preconditions:** A `ref` taken from `plan(session=…).feedback`. `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__get_body(id="<session>:pf<N>")`.
- **Expected:** `{type:"plan_feedback", verdict, plan_id, text, pairs, ts}` — `text` is the verbatim response blob (boilerplate included), redacted by default and `max_chars`-capped; `pairs` mirrors the extracted quote/comment tuples. An out-of-range ordinal or unknown session returns `{"error":"not_found"}`.
- **Pass criteria:** GO when the raw blob round-trips (the default `plan` response carried only the pairs; the blob arrives ONLY on this demand) and bad refs fail honest `not_found`.

### BODY-6 — `tool_call` body by id → full call input (FFE-3 on-demand route)
- **Function:** `get_body`
- **Goal:** A `tool_call` id resolves to the full call `input` (not the bare tool name), giving the on-demand route the reference-by-default `find_file_edits` promises.
- **Preconditions:** A `tool_call(edit|write)` id from `query`, and the same edit visible to `find_file_edits`. `[hermetic-ok]` (synthetic Edit session).
- **Steps:** `mcp__ai-r__find_file_edits(path="widget.py")` → take `input_sha256`+`input_chars`; `mcp__ai-r__query(type="tool_call", session="<uuid>")` → take the edit event `id`; `mcp__ai-r__get_body(id="<id>", redact=false)`.
- **Expected:** `{type:"tool_call(edit)", tool:"Edit", body:{…}}` — `body` is the full edit input; its JSON-canonical sha256 == the `input_sha256` and its length == the `input_chars` `find_file_edits` emitted; `max_chars` caps an over-long body (`body_truncated`); secrets in `body` are masked by default.
- **Pass criteria:** GO when the returned `body` round-trips the `find_file_edits` reference (sha256 + length match) and is the input dict, NOT the tool name. Returning only the tool name is NO-GO.

</details>

---

## `aggregate`

Rolls up rows (from `query` / `find_file_edits` / session inventory) → `{groups, totals}`.

<details>
<summary>Show 6 scenarios (AGG-1…AGG-6)</summary>

### AGG-1 — `group_by=agent`, `metrics=[count, edits]`
- **Function:** `aggregate`
- **Goal:** Grouping partitions rows correctly; `count` sums to the row total.
- **Preconditions:** A row set (e.g. from `find_file_edits`). `[hermetic-ok]` (rows may be synthetic).
- **Steps:** `mcp__ai-r__aggregate(rows=<rows>, group_by="agent", metrics=["count","edits"])`.
- **Expected:** One group per distinct agent; each group has `count` and `edits`.
- **Pass criteria:** GO when `sum(group.count for group in groups) == len(rows)` and every row lands in exactly one group.

### AGG-2 — `rank_by=stats` ordering
- **Function:** `aggregate`
- **Goal:** `rank_by=stats` reproduces the session-stats rank `(-sessions, -edits, label)`.
- **Preconditions:** Rows with `sessions`/`edits`. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__aggregate(rows=<rows>, group_by="agent", metrics=["sessions","edits"], rank_by="stats")`.
- **Expected:** Groups ordered by descending sessions, then descending edits, then label ascending as tiebreak.
- **Pass criteria:** GO when the group order is exactly `(-sessions, -edits, label)`.

### AGG-3 — `kind_split=true`
- **Function:** `aggregate`
- **Goal:** `kind_split=true` surfaces the `kind_split_available` flag + `note`.
- **Preconditions:** Rows carrying a `kind`. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__aggregate(rows=<rows>, group_by="kind", metrics=["sessions","edits"], kind_split=true)`.
- **Expected:** Result includes `kind_split_available` (bool); a `note` is present **only when the split is degenerate** (`kind_split_available=false` — e.g. no subagent sessions in scope), explaining the Claude-only detection (RISK-4).
- **Pass criteria:** GO when `kind_split_available` is present and correct for the data, and a `note` appears exactly in the degenerate case. A non-degenerate split with no `note` is correct behavior, not a failure.

### AGG-4 — empty rows → empty result, no crash
- **Function:** `aggregate`
- **Goal:** Empty input yields an empty, well-formed result rather than an error.
- **Preconditions:** none. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__aggregate(rows=[], group_by="agent", metrics=["count","sessions"])`.
- **Expected:** `groups == []`; `totals.sessions == 0`; `totals.agents == 0`; `totals.agents_list == []`.
- **Pass criteria:** GO when the empty result is returned with no crash and the zeroed totals shape.

### AGG-5 — `metrics=["tokens"]` folds token blocks with honest provenance `[hermetic-ok]`
- **Function:** `aggregate`
- **Goal:** The `tokens` metric (F3.3) sums per-row token blocks and never fabricates numbers or provenance.
- **Preconditions:** Synthetic rows carrying `tokens` blocks in the `session_tokens` shape — at least one `source="exact"` row with full sub-fields, one `source="estimate"` row (total only), one bare-int `tokens`, and one row with no `tokens` at all. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__aggregate(rows=<rows>, group_by="agent", metrics=["count","tokens"])`.
- **Expected:** Each group and `totals` carry a `tokens` block `{input, output, reasoning, cache_read, cache_write, total, exact, estimated, unknown}`; sums cover only rows that carried each field as an int; a field no row carried is `null` (not `0`); the bare-int row contributes to `total` but counts under `unknown` (provenance not claimed); the no-tokens row counts under `unknown`.
- **Pass criteria:** GO when `exact + estimated + unknown == len(rows)` in every block, the sums match hand-computation, and no absent field surfaces as a fabricated `0`. A block claiming `exact` for an unlabeled total is NO-GO.

### AGG-6 — `group_by="model"` splits events by the producing model `[hermetic-ok]`
- **Function:** `aggregate` (over `query` rows — the model dimension)
- **Goal:** Query rows carry the producing `model`, and `group_by="model"` buckets them per model with an honest `"(unknown)"` bucket for rows without a signal — «which model did what» in one chain, no second classifier.
- **Preconditions:** A session recorded by TWO models — e.g. a fixture transcript whose assistant records carry `message.model` `"model-alpha-1"` for one turn (a text turn + an `Edit` tool call) and `"model-beta-2"` for another, plus at least one user turn (which never carries a model). `[hermetic-ok]` (a temp `AI_R_HOME` fixture suffices; on a real vault use any session listed with ≥2 `models` in `list_sessions`).
- **Steps:** `mcp__ai-r__query(session="<uuid>", limit=0)` → collect the events; then `mcp__ai-r__aggregate(rows=<events>, group_by="model", metrics=["count"])`.
- **Expected:** Assistant-side events (`assistant_turn` / `tool_call(*)` / `plan_event`) carry a top-level `model` matching the transcript record behind each; user turns carry NO `model` key. The aggregate returns one group per model, plus a `"(unknown)"` group holding exactly the no-signal rows; `sum(group.count) == len(rows)`.
- **Pass criteria:** GO when each model group's `count` matches a hand-count of that model's events, the `"(unknown)"` bucket equals the number of model-less rows (user turns), no event carries a model its transcript record does not name (never guessed), and the counts sum to the row total. A fabricated model on a user turn or a missing `"(unknown)"` bucket is NO-GO.

</details>

---

## `diff`

Stitches edit rows into a per-file unified diff; bodies fetched on demand.

<details>
<summary>Show 1 scenario (DIFF-1)</summary>

### DIFF-1 — rows → per-file unified diff
- **Function:** `diff`
- **Goal:** Edit/write rows fold into a per-file unified diff, bodies on-demand.
- **Preconditions:** Edit rows for a session (e.g. `query(type="tool_call(edit)", session=<uuid>)`). `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__diff(rows=<edit-rows>, per_file=true, format="unified")`.
- **Expected:** `{files:[{file, edits, diff, hunks}], count, caveats}`; one entry per touched file; `diff` is a unified diff; rows without a file `ref` produce no phantom file.
- **Pass criteria:** GO when each touched file has a unified `diff` and `hunks`, `count` matches the file count, and no body is inlined beyond the diff itself.

</details>

---

## `detect_current`

<details>
<summary>Show 2 scenarios (DET-1…DET-2)</summary>

### DET-1 — runtime identity
- **Function:** `detect_current`
- **Goal:** Returns a sensible runtime identity of the calling agent/session.
- **Preconditions:** Running inside an agent session (env/fs signals present). `[hermetic-ok]` (empty env → null identity is still valid).
- **Steps:** `mcp__ai-r__detect_current()` (optionally `agent="<hint>"`).
- **Expected:** `{session_id, agent, model, liveness, candidates:[…], verified, self}`; when env carries a session id, `session_id`/`agent` are filled and `candidates[0].source` names the winning env var; empty env → all-null/false.
- **Pass criteria:** GO when the reported identity is internally consistent (candidates cascade explains the chosen `session_id`/`agent`, `verified` reflects whether the id was confirmed). An unknown `agent` hint must error.

### DET-2 — process-liveness (`liveness`), honest `null` without a pid signal `[hermetic-ok]`
- **Function:** `detect_current` (and the `list_sessions` summary surface)
- **Goal:** `liveness` is present and honestly `null` when there is no pid signal — never a fabricated live/dead verdict. Only Claude exposes a pid registry (`claude agents --json`), so a non-Claude or unregistered session must report `null`.
- **Preconditions:** `[hermetic-ok]` — empty/synthetic env (the detected session is absent from the `claude agents` registry, or the CLI is unavailable); optionally seed one synthetic non-Claude (e.g. `codex`) session under `AI_R_HOME`.
- **Steps:** `mcp__ai-r__detect_current()`; then `mcp__ai-r__list_sessions()`.
- **Expected:** the `detect_current` result carries a `liveness` key whose value is one of `fresh`/`paused`/`zombie`/`dead`/`null`, and is `null` when the session is not in the pid registry (or the CLI is absent); every non-Claude `list_sessions` summary carries `liveness: null`.
- **Pass criteria:** GO when `liveness` is present and equals `null` for a session with no pid signal (never a fabricated verdict); a Claude summary/identity whose pid is live is `fresh`/`paused` (or `null` when the `claude` CLI is unavailable). NO-GO if a non-Claude session ever reports a non-`null` liveness.

</details>

---

## `plan`

Normalized plan atoms of a session; agent differences hidden. Task grouping is by stable
`task_id` (plan-file slug), not title.

<details>
<summary>Show 12 scenarios (PLAN-1…PLAN-12)</summary>

### PLAN-1 — grouped by slug, not title `[needs-real-vault]`
- **Function:** `plan`
- **Goal:** A task whose title drifts across drafts stays ONE task, with zero false `completed_major`.
- **Preconditions:** A real claude session that redrafts one plan-file with drifting titles (e.g. `proud-snacking-ritchie`, uuid `d61def2a-…`). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<uuid>", agent="claude")`.
- **Expected:** All plan atoms share one `task_id` (the slug `plans/<slug>.md`); exactly 1 `final`, the rest `draft`, `0` `completed_major`.
- **Pass criteria:** GO when `len({p.task_id}) == 1`, `count(final) == 1`, and `count(completed_major) == 0` despite the drifting titles.

### PLAN-2 — kinds: N draft + 1 final by file (append) order
- **Function:** `plan`
- **Goal:** Within one task, the last plan_event in **file/append order** (`seq`) is `final`; earlier ones are `draft`.
- **Preconditions:** A session with a redraft chain. `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; inspect `kind` per atom.
- **Expected:** Exactly one `final` (the latest by `seq`, i.e. the last-written revision), the rest `draft`.
- **Pass criteria:** GO when the single `final` is the last-written revision and all earlier revisions are `draft`. (Ordering is file order, not timestamp — the non-monotonic-`ts` case is PLAN-12.)

### PLAN-3 — cross-agent codex `update_plan` normalized
- **Function:** `plan`
- **Goal:** Codex `update_plan` runs normalize into the same Plan atom shape.
- **Preconditions:** A codex session with `update_plan`. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<codex-uuid>", agent="codex")`.
- **Expected:** Plan atoms with `agent == "codex"`, the same `{id, title, task_id, kind, steps?, status?}` fields; the last `update_plan` is `final`.
- **Pass criteria:** GO when codex atoms are shape-identical to claude atoms and the final carries rolled-up `steps`/`status`.

### PLAN-4 — no false positive from a quoted `update_plan`
- **Function:** `plan`
- **Goal:** An `update_plan` string appearing **only quoted inside prompt text** must NOT emit a plan atom.
- **Preconditions:** A session where "update_plan" occurs only as quoted text, with no real tool call. `[hermetic-ok]` (synthetic) or `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`.
- **Expected:** No plan atom is emitted for the quoted mention.
- **Pass criteria:** GO when the quoted mention produces zero plan atoms (signal comes from the tool call, not prompt text).

### PLAN-5 — empty (not error) for agents with no plan signal
- **Function:** `plan`
- **Goal:** Agents that have no plan signal (opencode, pi) return an empty result, not an error.
- **Preconditions:** An opencode and/or pi session. `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__plan(session="<opencode-or-pi-uuid>", agent="opencode")`.
- **Expected:** An empty plan list, no error dict.
- **Pass criteria:** GO when the result is an empty list and no error is raised.

### PLAN-6 — final body inlined by default, drafts stay references (F3.4)
- **Function:** `plan`
- **Goal:** The default response carries the FINAL plan's full text inline; draft/major bodies are never inlined.
- **Preconditions:** A session with a multi-revision plan iteration (claude redraft chain). `[hermetic-ok]` (synthetic) or `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; then `mcp__ai-r__plan(session="<uuid>", bodies="none")`.
- **Expected:** Default: the `final` atom carries `body` (full plan text) + `body_source` (`"approval_edited_by_user"` when the user's approval carried an edited plan — that text overrides the signal/file body — else `"plan_signal"`; honest `null` body for a steps-only codex plan); every `draft`/`completed_major` atom has NO `body` key. `bodies="none"`: no atom carries `body`/`body_source` (the historical reference-only shape plus the v2 `version` field).
- **Pass criteria:** GO when exactly the final is inlined with a truthful `body_source`, drafts stay references, and `bodies="none"` carries no body on any atom (historical fields unchanged; `version` is the only v2 addition).

### PLAN-7 — «plan quote → user comment» pairs with refs (F3.4)
- **Function:** `plan`
- **Goal:** Every pair the user produced while iterating a plan (selection rejections, stay-in-plan-mode `[Re: "…"]` comments, free-text rejections) is extracted, chronological, bound to the revision it answered.
- **Preconditions:** A claude session whose plan went through rejections with selected-text comments (e.g. a real plan-review session). `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; inspect `feedback`/`feedback_count`.
- **Expected:** Each pair carries `plan_id` (a `plan_event` id from the same response, `null` only when the transcript records no call id), `verdict ∈ rejected|stay_in_plan_mode`, `quote` (the plan excerpt, `null` for a free-text comment), the verbatim `comment`, `ts` and a `ref` of the form `"<session>:pf<N>"`. Technical failures (permission-stream errors) and bare no-comment rejections produce NO pairs. Secrets in quotes/comments are redacted by default.
- **Pass criteria:** GO when all user pairs are present in chronological order with correct verdicts and revision binding, filtered garbage is absent, and `feedback=false` omits the list.

### PLAN-8 — honest empty feedback where no approval flow exists
- **Function:** `plan`
- **Goal:** Agents without an interactive plan-approval flow (codex, antigravity, opencode, pi) contribute an honest empty `feedback` — never fabricated pairs.
- **Preconditions:** A codex session with `update_plan` revisions. `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__plan(session="<codex-uuid>", agent="codex")`.
- **Expected:** `feedback == []`, `feedback_count == 0`; the plan atoms themselves are unchanged.
- **Pass criteria:** GO when feedback is empty (not an error, not invented) and the atoms match the historical output.

### PLAN-9 — draft numbering v1…vN per task (F3.4 v2)
- **Function:** `plan`
- **Goal:** Every plan atom carries `version` — its 1-based revision number within the task group, chronological by `(ts, seq)`; drafts are `v1…vN-1`, the final is `vN`; numbering restarts per task.
- **Preconditions:** A session with a multi-revision plan iteration (claude redraft chain) and, ideally, a second task. `[hermetic-ok]` (synthetic) or `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; inspect `version` per atom; cross-check a multi-task session.
- **Expected:** Within one task the versions are `1..N` in chronological order and the `final` atom holds the highest number; a second task numbers from `1` again; feedback pairs carry the matching `plan_version` (the answered revision's number, `null` when the transcript records no call-id correlation).
- **Pass criteria:** GO when versions are dense, chronological, per-task, the final is `vN`, and each pair's `plan_version` equals the `version` of the atom its `plan_id` points at.

### PLAN-10 — quote anchored to its plan section through rendered markup (F3.4 v2)
- **Function:** `plan`
- **Goal:** A feedback pair's `quote` (selected from the RENDERED plan — the UI strips markdown markup) anchors to the heading of the ONE source-markdown section that contains it; a miss or an ambiguous match is an honest `null`, never a nearest guess.
- **Preconditions:** A claude plan session whose plan body has markdown sections and markup (bold/backticks/lists) and whose rejections quote rendered text. `[hermetic-ok]` (synthetic) or `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; inspect `section` on each feedback pair.
- **Expected:** A quote whose source sentence carries markup (`**bold**`, `` `code` ``, list markers) still anchors — both sides are compared through the same markup-stripping normalization; a quote present in TWO sections gets `section: null` (ambiguity is not resolved by picking one); a quote absent from the plan gets `section: null` (miss stays a miss); a free-text pair (`quote: null`) has `section: null`; fenced code blocks never start a section.
- **Pass criteria:** GO when markup-bearing quotes anchor to the correct heading and every miss/ambiguity/free-text case is `null` — any "nearest" guess is NO-GO.

### PLAN-11 — feedback rounds: grouping + `rounds="last"` (F3.4 v2)
- **Function:** `plan`
- **Goal:** Pairs are grouped by `round` (1-based per session, one round per user response that produced pairs) and `rounds="last"` keeps only each session's final round.
- **Preconditions:** A claude session with ≥2 feedback rounds (e.g. a rejection then a stay-in-plan-mode). `[hermetic-ok]` (synthetic) or `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; then `mcp__ai-r__plan(session="<uuid>", rounds="last")`; then an invalid `rounds="first"`.
- **Expected:** Default: every pair carries `round`, numbers are dense `1..R` in chronological order and all pairs from one response share one round. `rounds="last"`: only round-`R` pairs remain; the plan atoms and `count` are unaffected. `rounds="first"` fails loud (`invalid_argument`), even with `feedback=false`.
- **Pass criteria:** GO when round numbering is dense and response-aligned, `"last"` returns exactly the final round, atoms are untouched, and the invalid value errors instead of being silently ignored.

### PLAN-12 — non-monotonic timestamps bind signals by file order, not `ts` `[hermetic-ok]`
- **Function:** `plan`
- **Goal:** When a session's plan revisions are written in an order that does NOT match their timestamps (a resumed / clock-skewed session — a later-written revision carries an earlier `ts`), `plan()` still binds each revision's body/steps/version to the correct revision, and `plan()` agrees with `get_body`.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` one Claude session with two plan revisions where the **file-later** revision (rev2) has an **earlier** `timestamp` than the file-earlier revision (rev1).
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; note the `final` body and its `version`/feedback ref; then `mcp__ai-r__get_body(id="<the plan/feedback ref>")`; compare the resolved body against the `plan()` body.
- **Expected:** The `final` revision is the last one in file order (rev2's successor by append, not the higher-`ts` one), its inlined body/steps/version come from that same revision, and `get_body` on the ref returns the identical body. `plan_feedback` correlates to the same revision `plan()` reported.
- **Pass criteria:** GO when body/steps/version all come from one consistent revision and `plan()` and `get_body` agree. NO-GO if the body is from one revision while the version/steps are from another (the pre-fix `ts`-ordinal bug), or if `plan()` disagrees with `get_body`.

</details>

---

## `session_stats` (preset)

Thin preset: builds per-session inventory rows → `aggregate(rank_by=stats, kind_split=true)` →
projected to the legacy totals shape.

<details>
<summary>Show 5 scenarios (STAT-1…STAT-5)</summary>

### STAT-1 — all 5 dims give sensible counts
- **Function:** `session_stats`
- **Goal:** Each grouping dimension (agent/dir/date/kind/model) returns sensible non-zero counts.
- **Preconditions:** A non-empty vault. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__session_stats(group_by="agent")`, then `"dir"`, `"date"`, `"kind"`, `"model"`. Cross-check the `model` buckets against the `models` field of a handful of `list_sessions` summaries.
- **Expected:** For each dim: a `groups` list and `totals` with `sessions`/`edits`/`agents`/`agents_list`; counts are non-zero and plausible for the vault. Under `group_by="model"` the bucket labels are concrete model ids (e.g. `claude-haiku-4-5`) plus, where they apply, `"(mixed)"` (a session that used several models — attributed to none of them) and `"(unknown)"` (no model signal in the transcript). `sum(groups.sessions) == totals.sessions`.
- **Pass criteria:** GO when all five dimensions return well-formed, non-zero, plausible stats AND every `model` bucket matches the sessions' own `models` field — a multi-model session filed under one of its models, or a signal-less session filed under a concrete model, is NO-GO (a guessed attribution).

### STAT-2 — degenerate kind split → flag + note
- **Function:** `session_stats`
- **Goal:** On a vault with no subagents, the kind split is degenerate and says so.
- **Preconditions:** A vault whose sessions are all one kind. `[hermetic-ok]` or `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__session_stats(group_by="kind")`.
- **Expected:** `kind_split_available == false` plus an explanatory `note`.
- **Pass criteria:** GO when the degenerate split is flagged (`kind_split_available=false`) with a note rather than silently emitting a misleading split.

### STAT-3 — byte-parity with manual aggregate on a FROZEN snapshot `[needs-real-vault]`
- **Function:** `session_stats`
- **Goal:** The preset is byte-identical to the explicit `aggregate(rank_by=stats, kind_split=true)`.
- **Preconditions:** A **frozen** snapshot of the vault (the live vault mutates during a run → false mismatches; measure on a snapshot). `[needs-real-vault]`.
- **Steps:** compute `mcp__ai-r__session_stats(group_by="<dim>")` and the manual `mcp__ai-r__aggregate(rows=<per-session inventory rows>, group_by="<dim>", rank_by="stats", kind_split=true)` on the same frozen snapshot; compare.
- **Expected:** `groups` and shared totals (`sessions`/`edits`/`agents`/`agents_list`) are identical.
- **Pass criteria:** GO when the projection matches the manual aggregate byte-for-byte on the frozen snapshot. (Divergence caused only by live-vault mutation between the two calls is a measurement artifact, not a defect — re-measure on a true snapshot.) **MCP-surface scope note:** the *enriched* totals (`edits`/`intents`/`messages`) fold an internal per-session inventory that no read-only MCP verb emits as `rows`, so the live MCP check can only prove parity of the **projection** (rank order + `kind_split` + `note` + `sessions` count); full enriched byte-parity is a pytest-internal guarantee. A GO-with-caveats at the MCP level (projection verified, enriched totals not feedable) is the expected verdict.

### STAT-4 — `with_tokens=true`: request-time token usage, exact vs labeled estimate `[needs-real-vault]`
- **Function:** `session_stats`
- **Goal:** Token usage (F3.3) is read from the sessions' own files at request time — exact where the format records it, a labeled estimate otherwise, honest `unknown` without signal; the default output is untouched.
- **Preconditions:** A vault with sessions from at least Claude or Codex (formats with recorded usage). `[needs-real-vault]`.
- **Steps:** (1) `mcp__ai-r__session_stats(agent="claude", group_by="agent", with_tokens=true)`; (2) the same call without `with_tokens`; (3) if Antigravity sessions exist, `mcp__ai-r__session_stats(agent="antigravity", group_by="agent", with_tokens=true)`.
- **Expected:** (1) every group and `totals` carry a `tokens` block; for Claude/Codex/OpenCode/Pi vault data the `exact` counter dominates and `total` is a plausible positive sum; (2) **no** `tokens` key anywhere — byte-identical historical shape; (3) Antigravity sessions count under `estimated` (transcript estimate — tiktoken when the optional extra is installed, chars/4 otherwise) or `unknown`, never under `exact`.
- **Pass criteria:** GO when the counters are honest (`exact + estimated + unknown == sessions` per group, Antigravity never `exact`), sums that no session carried stay `null`, the block contains only numbers/labels (no raw session text), and omitting `with_tokens` changes nothing. A fabricated exact number for a format without recorded usage, or a crash on a host without tiktoken, is NO-GO.

### STAT-5 — `with_tokens` unscoped over corpus limit → `scope_required` refusal, never hangs `[hermetic-ok]`
- **Function:** `session_stats`
- **Goal:** An unscoped `with_tokens=true` over a corpus larger than `token_scan_limit` refuses fast with a structured `scope_required` error BEFORE reading any usage file. Guards the historical hang (the token counter re-globbed + parsed every session and never returned). A scope narrows it; `token_scan_limit=0` opts out; a large *permitted* scan attaches a `warning`.
- **Preconditions:** A corpus whose match count exceeds the default `token_scan_limit=400` (a real large vault, or a fixture over the limit). `[hermetic-ok]` — the refusal is deterministic once the count exceeds the limit, host-independent.
- **Steps:** (1) `mcp__ai-r__session_stats(with_tokens=true)` with NO `agent`/`since`/`until`/`session` scope — **run with a timeout; a hang (not a refusal) is the regression this scenario guards**; (2) the same with `agent="claude"` (or any `since=`); (3) `mcp__ai-r__session_stats(with_tokens=true, token_scan_limit=0)`; (4) a scoped call whose permitted scan still exceeds the warn threshold.
- **Expected:** (1) returns `{"error":"scope_required", "matched_sessions":N, "token_scan_limit":400, "scoped":false}` fast, with NO usage file read (no hang); (2) runs normally, `tokens` block present; (3) cap disabled, scan runs; (4) result carries a `warning` about the large scan.
- **Pass criteria:** GO when the unscoped over-limit call REFUSES fast with a structured `scope_required` (never hangs, never returns a partial token total), scope / `token_scan_limit=0` let it through, and a large permitted scan warns. A hang, a silent partial total, or a crash is NO-GO.

</details>

---

## `session_diff` (preset)

Thin preset: `diff(query(edit|write, session=<uuid>, with_intent=true))` for non-codex; codex keeps
the legacy shell-scan branch.

<details>
<summary>Show 3 scenarios (SDIFF-1…SDIFF-3)</summary>

### SDIFF-1 — claude session → per-file hunks, chronological, intent attached `[needs-real-vault]`
- **Function:** `session_diff`
- **Goal:** A claude session diffs into per-file hunks in chronological order, each with the driving intent.
- **Preconditions:** A claude session with ≥1 edit. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__session_diff("<uuid>", "claude")`; cross-check one hunk with `mcp__ai-r__read_session("<uuid>")`.
- **Expected:** `{files:[{file, edits:[…]}], …}`; edits per file are chronological; each edit carries an `intent`; the `Read`-only files are excluded (edit|write filter).
- **Pass criteria:** GO when the file/edit order is chronological, `intent` is attached, and a spot-checked hunk matches the transcript in `read_session`.

### SDIFF-2 — codex session, shell-redirect reconstruction + documented blind spots `[needs-real-vault]`
- **Function:** `session_diff`
- **Goal:** A codex session reconstructs edit targets from shell redirects, with the known blind spots skipped.
- **Preconditions:** A codex session that writes files via shell-exec (RISK-3). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__session_diff("<codex-uuid>", "codex")`.
- **Expected:** Targets recovered from `printf … > path` and `cat > path <<EOF`; edits via `tee` / `sed -i` / `cp` / `mv` are **silently skipped** (documented blind spots).
- **Pass criteria:** GO when `printf >` / `cat > <<EOF` targets appear correctly. GO-with-caveats is the expected verdict when the session also contains `tee`/`sed -i`/`cp`/`mv` edits — their absence is a documented limitation, not a defect. An undocumented missing edit is NO-GO.

### SDIFF-3 — size-bounded MCP output: one big `Write` cannot blow the response `[hermetic-ok]`
- **Function:** `session_diff` (and the `diff` verb — same shared bound)
- **Goal:** A session with one oversized `Write` (the observed 89 KB HTML → 145K-char response) returns a bounded response with every cut named; the core data stays reachable on demand.
- **Preconditions:** A session containing a `Write` whose `content` exceeds 4000 chars, driven by a user intent over 1000 chars. `[hermetic-ok]` (a synthetic session under `AI_R_HOME` suffices).
- **Steps:** (1) `mcp__ai-r__session_diff("<uuid>", "<agent>")`; (2) fetch the full body via `mcp__ai-r__get_body(id="<the edit's tool_call id>")` (or `read_session`); (3) `mcp__ai-r__session_diff` on a small-edit session.
- **Expected:** (1) the write hunk's `content` and the stitched per-file `diff` end with `…[truncated]`; the file entry's `truncated_fields` names each cut as an indexed path (e.g. `edits[0].intent`, `edits[0].hunks[0].content`, `diff`); the response carries `output_truncated` (`false` unless the whole-file byte budget was hit; when it is hit, `count` still reports the true file total); (2) the on-demand body is the FULL, uncut content; (3) the small session's fields are byte-identical to the core (`truncated_fields: []`, nothing cut).
- **Pass criteria:** GO when over-long fields are cut **and named**, `output_truncated` is present, the full body remains fetchable on demand, and under-cap sessions pass through untouched. A silent unbounded response (or a cut that is not named) is NO-GO.

</details>

---

## `incidents` (preset)

The F4.1 preset: dangerous shell command + regret reaction, one call. A baked chain over the
existing core (never a second engine): ONE `query(type="tool_call", tool_kind="bash")` scan supplies
the candidates → the deterministic **danger dictionary** (harvested from public agent-guardrail rule
sets, calibrated on real host history 2026-07-04) selects dangerous commands → the bilingual (ru+en)
**regret dictionary** scans the following `reaction_window` messages (default 6) for an
apology/rollback reaction — the two-step check behind `confirmed`. Zero LLM: no dictionary hit → no
incident, no reaction → `confirmed: false`, never inferred.

<details>
<summary>Show 4 scenarios (INC-1…INC-4)</summary>

### INC-1 — dangerous command surfaces as an incident (two-step record shape) `[needs-real-vault]`
- **Function:** `incidents`
- **Goal:** A real dangerous shell call comes back as an incident record with the full two-step shape.
- **Preconditions:** A vault whose history contains at least one dangerous shell command (e.g. `git reset --hard`, `rm -rf`). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__incidents(limit=10)`; pick one record; cross-check its `id` via `mcp__ai-r__query(relative_to="<id>", direction="prev")` and `mcp__ai-r__read_session(<session_id>)`.
- **Expected:** `{incidents:[…], count, confirmed_count, by_pattern, truncated, reaction_window}`; every record carries `id` (a query event id), `agent`, `session_id`, `ts`, `tool`, non-empty `patterns` (ids like `git.reset_hard`) + `categories` (`fs`/`git`/`db`/`net`), a `command` fragment containing the matched text (char-capped, `command_truncated` flagged on a real cut), `confirmed` and `reaction`; records are chronological (ts ascending); the `id` resolves to the same call via `get_body`/`query`; `message_index` is the raw parser index — `read_session` offsets are projected (tool-role entries dropped), so the same call may sit at a shifted offset there.
- **Pass criteria:** GO when the record's `command` really matches its `patterns`, the `id` walks back to the true preceding user turn, and `by_pattern` sums match the per-record pattern counts over the FULL match set (independent of `limit`).

### INC-2 — two-step check: `confirmed` verdict + `confirmed`/`category` filters `[needs-real-vault]`
- **Function:** `incidents`
- **Goal:** The regret reaction drives `confirmed`, and the filters compose honestly.
- **Preconditions:** A vault with at least one confirmed incident (dangerous command followed by an apology/rollback within the window). `[needs-real-vault]`; on a vault without confirmed incidents the confirmed-shape check is skipped (GO-with-caveats), the subset algebra still runs.
- **Steps:** `mcp__ai-r__incidents(confirmed="only")`, `mcp__ai-r__incidents(confirmed="exclude")`, `mcp__ai-r__incidents()`; then `mcp__ai-r__incidents(category="git")`.
- **Expected:** `only` ∪ `exclude` = `include` (counts add up); every `only` record has `reaction` (`message_index`, `offset` ≤ `reaction_window`, `role`, marker labels — e.g. `извинение`/`apology` — and a capped `preview`); every `exclude` record has `reaction: null` and `confirmed: false`; `category="git"` keeps only records with ≥1 `git.*` pattern.
- **Pass criteria:** GO when the subset algebra holds, a confirmed record's `reaction.preview` really contains regret wording, and no record is ever confirmed without a dictionary hit in the window (never guessed).

### INC-3 — fail-loud validation + empty-result diagnostics `[hermetic-ok]`
- **Function:** `incidents`
- **Goal:** Unknown parameter values fail loud; an empty result is explainable.
- **Preconditions:** None (empty vault is fine). `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__incidents(category="network")`, `mcp__ai-r__incidents(confirmed="maybe")`, `mcp__ai-r__incidents(reaction_window=-1)`; then a valid call on an empty/filtered-to-zero corpus.
- **Expected:** Each invalid call returns `{"error": "invalid_argument", "message": …}` naming the offending parameter — never a silent empty result; the valid zero-match call returns `count: 0` **plus** `diagnostics` (scanned agents, corpus bounds, cause hints).
- **Pass criteria:** GO when all three invalid calls fail loud and the zero-result response carries `diagnostics` (a non-empty response never does).

### INC-4 — cross-agent honesty: tri-state `is_error`, RAW matching, redacted emission `[needs-real-vault]`
- **Function:** `incidents`
- **Goal:** All agents participate on equal terms and honesty rules hold on real data.
- **Preconditions:** A vault with shell calls from ≥2 agents (e.g. claude + codex/opencode). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__incidents(limit=0)`; inspect per-agent records; compare one record with `redact=false`.
- **Expected:** Records from every agent whose history has dangerous shell calls (not only Claude); `is_error` is `true`/`false` only where a correlated result outcome exists and `null` elsewhere (e.g. codex — no per-result flag), never fabricated; with `redact=true` (default) any secret in `command`/`reaction.preview`/`session_title` is masked as `[REDACTED_<TYPE>]` with a `redactions` type→count dict, while the SAME record is found either way (matching ran on RAW text).
- **Pass criteria:** GO when non-Claude agents appear (given signal), no `is_error` is invented for formats without the flag, and redaction changes only the emitted fields — never the match set.

</details>

---

## `network` (preset)

The F4.3 preset: network-egress audit, one call. A baked chain over the existing core (never a
second engine): ONE `query(type="tool_call", tool_kind="web")` scan supplies the candidates —
Claude `WebFetch`/`WebSearch`, OpenCode `webfetch`, Codex `web_search` (surfaced from
`web_search_call` rollout records), Gemini/Antigravity `web_fetch`/`google_web_search`; Pi records
no web tool (honest absence) → the request target (`url`/`query`) is extracted from each call's own
input → the deterministic **risk dictionary** (`plain_http`, `credentials_in_url`, `secret_in_url`/
`secret_in_query` — the F2.1 redaction patterns double as the detector, `ip_literal_host`,
`private_or_local_host`, `punycode_host`). Zero LLM: no extractable target → honest `null` fields;
a risk fires only on parse/regex evidence. MCP-mediated network access (browser-automation servers
etc.) stays under `tool_kind="mcp"` — a name alone cannot prove an MCP server touches the network,
so it is never guessed into this audit (documented boundary).

<details>
<summary>Show 4 scenarios (NET-1…NET-4)</summary>

### NET-1 — web calls surface as request records (target extraction + rollups) `[needs-real-vault]`
- **Function:** `network`
- **Goal:** Real web-tool calls come back as request records with extracted targets and honest rollups.
- **Preconditions:** A vault whose history contains web-tool calls (e.g. Claude `WebFetch`/`WebSearch`). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__network(limit=10)`; pick one record; cross-check its `id` via `mcp__ai-r__query(relative_to="<id>", direction="prev")` and `mcp__ai-r__read_session(<session_id>)`.
- **Expected:** `{requests:[…], count, risky_count, by_domain, by_risk, truncated}`; every record carries `id` (a query event id), `agent`, `session_id`, `ts`, `tool`, derived `kind` (`fetch` when a `url` was extracted, `search` when a `query` was, `null` when neither — never guessed from the tool name), char-capped `url`/`query` (`*_truncated` flagged on a real cut), `domain` (`null` for searches), a `risks` list (possibly empty) and tri-state `is_error`; records are chronological (ts ascending); `by_domain` counts only records with a URL; the `id` walks back to the true preceding user turn.
- **Pass criteria:** GO when a fetch record's `url`/`domain` really match the transcript call, `by_domain`/`by_risk` sums match the per-record fields over the FULL match set (independent of `limit`), and no record has a `kind` its extracted fields don't justify.

### NET-2 — risk dictionary + `risk`/`kind`/`domain` filters compose honestly `[needs-real-vault]`
- **Function:** `network`
- **Goal:** Risk labels fire only on evidence, and the filters compose as documented.
- **Preconditions:** A vault with ≥1 risky request (e.g. a plain-`http://` fetch or a URL with a token in the query string); on a vault without one the risky-shape check is skipped (GO-with-caveats), the subset algebra still runs. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__network(risk="only")`, `mcp__ai-r__network(risk="exclude")`, `mcp__ai-r__network()`; then `mcp__ai-r__network(kind="search")` and `mcp__ai-r__network(domain="<a domain seen in by_domain>")`.
- **Expected:** `only` ∪ `exclude` = `include` (counts add up); every `only` record has ≥1 label from the fixed vocabulary (`plain_http`/`credentials_in_url`/`secret_in_url`/`secret_in_query`/`ip_literal_host`/`private_or_local_host`/`punycode_host`) and every label is backed by the visible URL/query shape; `kind="search"` returns only query-target records; `domain="github.com"`-style filter keeps the host itself and its subdomains, and never matches a URL-less search record.
- **Pass criteria:** GO when the subset algebra holds, every emitted risk label is justified by the record's own fields, and no label is ever fabricated for a clean request.

### NET-3 — fail-loud validation + empty-result diagnostics `[hermetic-ok]`
- **Function:** `network`
- **Goal:** Unknown parameter values fail loud; an empty result is explainable.
- **Preconditions:** None (empty vault is fine). `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__network(kind="download")`, `mcp__ai-r__network(risk="high")`, `mcp__ai-r__network(limit=-1)`; then a valid call on an empty/filtered-to-zero corpus.
- **Expected:** Each invalid call returns `{"error": "invalid_argument", "message": …}` naming the offending parameter — never a silent empty result; the valid zero-match call returns `count: 0` **plus** `diagnostics` (scanned agents, corpus bounds, cause hints).
- **Pass criteria:** GO when all three invalid calls fail loud and the zero-result response carries `diagnostics` (a non-empty response never does).

### NET-4 — cross-agent honesty: RAW assessment, redacted emission, honest boundaries `[needs-real-vault]`
- **Function:** `network`
- **Goal:** All agents participate on equal terms and the honesty rules hold on real data.
- **Preconditions:** A vault with web calls from ≥2 agents (e.g. claude + opencode/codex). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__network(limit=0)`; inspect per-agent records; compare one URL-bearing record with `redact=false`; check `tool_kind="mcp"` browser calls via `mcp__ai-r__query(tool_kind="mcp")` are absent from the audit.
- **Expected:** Records from every agent whose history has web calls (not only Claude; Codex `web_search` records appear when the rollouts contain `web_search_call`); `is_error` is `true`/`false` only where a correlated result outcome exists and `null` elsewhere, never fabricated; with `redact=true` (default) any secret in `url`/`query`/`session_title` is masked as `[REDACTED_<TYPE>]` with a `redactions` type→count dict, while the SAME record is found either way (assessment ran on RAW strings); MCP-mediated network calls (e.g. browser-automation servers) do NOT appear — they stay visible under `tool_kind="mcp"` in `query`, a documented boundary, not a blind spot.
- **Pass criteria:** GO when non-Claude agents appear (given signal), no `is_error` is invented for formats without the flag, redaction changes only the emitted fields — never the match set — and no MCP call was guessed into the audit.

</details>

---

## `quotes` (preset)

The F5.2 preset: «user quote → user comment» pairs, chat-wide and cross-agent. A baked chain over
the core (never a second engine): `query` scans supply user + assistant turns, and a deterministic
verbatim match (`difflib.SequenceMatcher` over `_normalize_rendered_text`) recovers the quoted span —
no client records a quote marker, so it is reconstructed from the text itself. The cross-agent,
chat-wide generalization of `plan(feedback)` (which is Claude-plan-only).

<details>
<summary>Show 2 scenarios (QUO-1…QUO-2)</summary>

### QUO-1 — a chat quote surfaces as «quote → comment» (record shape) `[hermetic-ok]`
- **Function:** `quotes`
- **Preconditions:** A session where a user turn embeds a verbatim ≥40-char span of a preceding assistant turn plus their own comment (the "attach selection as context" flow). Synthetic Claude session is sufficient.
- **Steps:** `mcp__ai-r__quotes(session="<uuid>")`; take a record; cross-check `id` and `source_id` via `mcp__ai-r__query(relative_to="<id>", direction="prev")` / `mcp__ai-r__read_session("<uuid>")`.
- **Expected:** `{quotes:[…], count, by_source_kind, truncated}`; each record carries `id` (the user_turn event), `source_id` (the quoted assistant turn, `!= id`), `source_kind="assistant"`, `quote_chars` (≥40), and char-capped `quote` + `comment` (the user's turn with the quote elided); the quote text appears verbatim (normalized) in the `source_id` turn; secrets in `quote`/`comment` are masked by default.

### QUO-2 — cross-agent + honest non-matches `[hermetic-ok]`
- **Function:** `quotes`
- **Steps:** run over a **codex** session where a user quotes an assistant line; then a session where the user pastes text NOT present earlier; then `mcp__ai-r__quotes(source_kind="tool")`.
- **Expected:** the codex quote is found (all agents equal — the match runs on the normalized event stream, not client markup); the external paste yields NO record (a quote with no in-session source is never fabricated); an unknown `source_kind` fails loud with `invalid_argument`; a filtered-to-zero corpus returns `count=0` + `diagnostics`.

</details>

---

## `audit_brief` (preset)

The stage-4 auditor preset: a token-lean, budgeted one-call session digest. A baked chain over the
existing projections (never a second engine): ONE `query(session=…)` scan supplies the user turns
(VERBATIM — never truncated by the budget) and the tool/file footprint (`aggregate` folds +
existing `file` refs), `plan`/`plan_feedback` supply the decision trail, `ai_r.tokens` the token
breakdown; a deterministic budget ladder tightens the digest until it fits `budget_chars`.

<details>
<summary>Show 2 scenarios (AB-1…AB-2)</summary>

### AB-1 — one call digests a session inside the budget (section shape) `[hermetic-ok]`
- **Function:** `audit_brief`
- **Preconditions:** A session with ≥2 user turns, ≥1 tool call (one with a correlated `is_error` result) and ≥1 file edit. Synthetic Claude session is sufficient.
- **Steps:** `mcp__ai-r__audit_brief(session="<uuid>")`; cross-check `user_turns` against `mcp__ai-r__query(type="user_turn", session="<uuid>")` and `tools.by_kind` against `mcp__ai-r__aggregate` over the session's `tool_call` rows.
- **Expected:** Sections `session` / `user_turns` / `plans` / `tools` / `files` / `tokens` / `component_tokens` / `budget`; every user turn's text is VERBATIM (byte-equal to the full projection, no `…` cut); `tools.by_kind` counts match the aggregate fold and `tools.errors` names the `is_error` row (id + tool + kind, no full dumps); `files.edited` lists the edited path with its edit count; `tokens.source` is honest (`exact`/`estimate`/`null`, never fabricated); `budget.used_chars ≤ budget.budget_chars`, `dropped=[]`, `over_budget=false`; secrets in emitted texts masked by default; an unknown session id → `{"error": "not_found"}`, a negative `budget_chars` → `invalid_argument`.

### AB-2 — the budget ladder tightens deterministically, user turns survive whole `[hermetic-ok]`
- **Function:** `audit_brief`
- **Steps:** run the same session with a shrinking `budget_chars` (e.g. full-size → mid → tiny) and compare the responses.
- **Expected:** Detail disappears in the FIXED ladder order — `tool_error_details` first, then `file_details`, then `plan_bodies` — with `budget.dropped` listing exactly what was removed (counts/references stay: `errors_count`/`files.count` unchanged, dropped sections flagged, plan bodies reachable via `get_body`); at every budget the user turns stay byte-identical to the unbudgeted run (NEVER truncated); when the remaining digest still exceeds the budget, `budget.over_budget=true` and `budget.note` names the full projections (`query(type='user_turn', …)` / `read_session`) — an honest marker, not a silent clip; `budget_chars=0` disables the ladder entirely.

</details>

---

## `locate` (preset)

The stage-4 lookup preset: find a session across every agent by full uuid, id prefix or
case-insensitive title substring — where it lives, whether it is readable locally, and the
ready-to-run read/resume commands. A thin preset over the per-parser `list_sessions` inventory
(zero new scanning code); `web=true` adds the v1 honest-scope block of locally-known web traces.

<details>
<summary>Show 2 scenarios (LOC-1…LOC-2)</summary>

### LOC-1 — uuid / prefix / title lookup with ranked, ready-to-run matches `[hermetic-ok]`
- **Function:** `locate`
- **Preconditions:** ≥2 sessions with distinct dates whose titles share a word; one session id with a known 8-hex head. Synthetic sessions are sufficient.
- **Steps:** `mcp__ai-r__locate(needle="<full-uuid>")`; then `locate(needle="<8-hex prefix>")`; then `locate(needle="<TITLE SUBSTRING in different case>")`; then a needle matching nothing.
- **Expected:** Full uuid and prefix return the session with `match="id"`; the title substring matches case-insensitively with `match="title"`; multiple matches are ranked by last activity (mtime) DESC with `count` = the full total and `truncated` honest under `limit`; each match carries `path`/`agent`/`project_dir`/`date`/`size_bytes`/`message_count`, an honest `readable` (a 0-message reference-only stub is `false`), a ready-to-run `read_command` (`ai-r read <uuid> --agent <agent>`) and the F2.2 `resume_command`; the zero-match call returns `count=0` + closest-title `suggestions` + `diagnostics` — never a fabricated match; an empty needle fails loud with `invalid_argument`.

### LOC-2 — `web=true` reports only locally-known web traces (honest scope) `[hermetic-ok]`
- **Function:** `locate`
- **Preconditions:** A fake `$SW_HOME/web-sessions` dir with an export file whose name contains the needle, and a `~/.claude.json` (under the test home) whose `projects[*].lastSessionId` starts with the needle.
- **Steps:** `mcp__ai-r__locate(needle="<id-prefix>", web=true)`; repeat with `web=false`; repeat with no web sources present.
- **Expected:** With `web=true` the response carries a `web` block: the hook-export file under `exports` (`source="hook_export"`, `readable=true`, path+size+mtime) and the teleport id under `stubs` (`source="teleport_stub"`, `content_local=false` — the id is known, the transcript is NOT on this machine) plus a `scope_note` naming the per-repo teleport-picker sweep as the documented follow-up; `web=false` carries no `web` key (byte-identical to before); missing dir/file are skipped honestly (`exports_dir_found`/`claude_json_found` say which), an unreadable `~/.claude.json` degrades to `claude_json_error` — never a crash, never a fabricated web session.

</details>

---

## `find_file_edits`

Cross-agent file-edit inventory. The MCP surface is **reference-by-default**.

<details>
<summary>Show 4 scenarios (FFE-1…FFE-4)</summary>

### FFE-1 — default MCP call is reference-by-default
- **Function:** `find_file_edits`
- **Goal:** The default call keeps the listing small — records carry a body **reference**, not the full body.
- **Preconditions:** A vault with file edits. `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__find_file_edits(path="/", limit=50)` (default `include_input=false`).
- **Expected:** Each record carries `input_sha256` (hash) + `input_chars` (length) and does **not** carry the full `input` body.
- **Pass criteria:** GO when every record has `input_sha256` + `input_chars` and NONE has an inlined `input`. A leaked full body is NO-GO.

### FFE-2 — `include_input=true` restores the full body
- **Function:** `find_file_edits`
- **Goal:** Opting in inlines the full edit body.
- **Preconditions:** same as FFE-1. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__find_file_edits(path="/", limit=50, include_input=true)`.
- **Expected:** Each record carries the full `input` body (and no longer needs the `input_sha256`/`input_chars` reference).
- **Pass criteria:** GO when records carry the full `input` body, confirming the opt-in path.

### FFE-3 — body fetched on-demand via `get_body`
- **Function:** `find_file_edits` + `get_body`
- **Goal:** From a reference-by-default record, the body is retrievable on demand.
- **Preconditions:** A record from FFE-1 (carrying `session_uuid` + `message_index`). `[needs-real-vault]`.
- **Steps:** take a record's referenced event id (via `session_uuid` + `message_index`, or the matching `query` event id) and call `mcp__ai-r__get_body(id="<id>")`.
- **Expected:** The full edit body is returned, and its size matches the earlier `input_chars` for that record.
- **Pass criteria:** GO when the on-demand body matches the reference (size/hash) from FFE-1 — proving reference-then-fetch works end-to-end.

### FFE-4 — size-bounded records + default window on a fully-unscoped call `[hermetic-ok]`
- **Function:** `find_file_edits`
- **Goal:** No call can blow the response past a sane size (the 3.2M-char-response regression), and an unscoped call does not silently dump months of history.
- **Preconditions:** A session whose edit is driven by an over-long user request (intent > 1000 chars), plus at least one edit older than 7 days. `[hermetic-ok]` (synthetic sessions under `AI_R_HOME` suffice).
- **Steps:** (1) `mcp__ai-r__find_file_edits(path="<edited-file>", agent="<agent>")` on the long-intent edit; (2) `mcp__ai-r__find_file_edits(path="<old-file>")` with NO `agent`/`since`/`until`; (3) the same call with `since="1970-01-01"`.
- **Expected:** (1) the record's `intent` ends with `…[truncated]` and `truncated_fields` names `intent` (an `include_input=true` body is NEVER field-capped — the FFE-2 promise holds); (2) the >7-day-old edit is absent and the response carries `default_since` + a `note` naming the 7-day window; (3) the explicit bound disables the window (no `default_since`/`note`) and the old edit returns.
- **Pass criteria:** GO when over-long fields are cut and named, the unscoped call is windowed **loudly** (never a silent full-corpus dump), and any explicit scope (`agent`/`since`/`until`) restores full control. A silent unbounded response is NO-GO.

</details>

---

## `list_sessions`

Cross-agent session inventory: newest-first, paginated, each summary self-describing.

<details>
<summary>Show 6 scenarios (LIST-1…LIST-6)</summary>

### LIST-1 — paginated, date-sorted, agent-filterable inventory
- **Function:** `list_sessions`
- **Goal:** Enumerate discoverable sessions without dumping the whole vault; each summary carries enough identity to drill in.
- **Preconditions:** A vault with sessions from at least one agent. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__list_sessions(limit=5)`; then `mcp__ai-r__list_sessions(agent="claude", limit=5)`.
- **Expected:** At most 5 summaries, sorted by date newest-first; each carries a session id, `agent`, date, `kind` (`"agent"`/`"subagent"`) and `parent_uuid`; a `truncated` flag is set when more sessions remain. The `agent="claude"` call returns only Claude sessions.
- **Pass criteria:** GO when results honor `limit`, are date-descending, the agent filter narrows the set, and every summary carries `kind` + `parent_uuid`. (Subagent detection covers Claude/OpenCode/Codex/Pi; Antigravity has no parent signal and always reports `kind="agent"` — a documented format boundary, not a NO-GO.)

### LIST-2 — `noise` filter splits top-level vs subagent sessions
- **Function:** `list_sessions`
- **Goal:** `noise=exclude|include|only` partitions the inventory by the noise criterion (subagent sessions), composes with `kind` by AND, and fails loud on an unknown mode.
- **Preconditions:** One top-level + one subagent session for the same agent. `[hermetic-ok]` (seed a fake parent + subagent pair under `AI_R_HOME`; for OpenCode use a fixture DB with `session.parent_id`).
- **Steps:** `mcp__ai-r__list_sessions(agent="<agent>")`; then `noise="exclude"`; then `noise="only"`; then the contradictory `kind="agent", noise="only"`; then `noise="bogus"`.
- **Expected:** Default (`include`) lists both sessions; `exclude` lists only the top-level one; `only` lists only the subagent (its summary carries `kind="subagent"` and the correct `parent_uuid`); `exclude`+`only` partition `include` (disjoint, union == all); the contradictory combination returns `total == 0` **with** `diagnostics`; `noise="bogus"` returns `{"error": "invalid_argument", …}` naming `noise`.
- **Pass criteria:** GO when the three modes partition the inventory exactly, `kind` and `noise` AND together, and the unknown mode is a loud error.

### LIST-3 — Claude Desktop overlay: dedup by uuid + `source_root` origin
- **Function:** `list_sessions` (Claude CLI + Desktop source roots)
- **Goal:** The Claude parser merges the CLI transcript root and the Claude Desktop metadata root into ONE inventory: no duplicate uuids, the Desktop title wins on the merged session, origin is marked in `extra["source_root"]`, and a metadata-only session is still visible as a reference.
- **Preconditions:** `[hermetic-ok]` — under a fake `AI_R_HOME` seed (a) one CLI JSONL transcript whose uuid is referenced by a Desktop metadata JSON (`cliSessionId` + a distinct `title`), (b) one Desktop metadata JSON whose `cliSessionId` has NO backing transcript, (c) one plain CLI transcript.
- **Steps:** `mcp__ai-r__list_sessions(agent="claude")`; then `mcp__ai-r__read_session(session_id=<desktop-only uuid>)`; then `mcp__ai-r__search_sessions(query=<a word unique to the Desktop title>, agent="claude", scope="title")`.
- **Expected:** Exactly 3 sessions, all uuids unique; the merged session carries the Desktop `title` (CLI-derived title preserved as `extra["cli_title"]`) and `source_root="desktop"`; the plain CLI session carries `source_root="cli"`; the Desktop-only session appears with `message_count=0` and reading it yields zero messages (not an error); the title search finds the merged session by its Desktop title.
- **Pass criteria:** GO when dedup holds (no uuid twice), both origin marks are correct, the Desktop title is searchable, and the metadata-only session reads as an empty reference. NO-GO on a duplicated session or a crash on the missing transcript.

### LIST-4 — live Claude Desktop store visible `[needs-real-vault]`
- **Function:** `list_sessions` (real `~/.config/Claude/claude-code-sessions`)
- **Goal:** On a host where the Claude Desktop app has been used, Desktop-launched sessions appear in the inventory marked `source_root="desktop"` and are findable by their Desktop-app title (the motivating bug: a Desktop session was invisible to title search because only its raw first-message title existed CLI-side).
- **Preconditions:** Real `~/.config/Claude/claude-code-sessions` with at least one `local_*.json`. `[needs-real-vault]` — skip (not fail) when absent.
- **Steps:** `mcp__ai-r__list_sessions(agent="claude", limit=50)`; pick one Desktop metadata `title` from the store; `mcp__ai-r__search_sessions(query=<its distinctive words>, agent="claude", scope="title")`.
- **Expected:** At least one session with `extra["source_root"]="desktop"`; no uuid appears twice; the search by the Desktop-app title returns that session.
- **Pass criteria:** GO when a desktop-marked, dedup-clean, title-searchable session is found; NO-GO on duplicates or a Desktop title that search cannot find.

### LIST-5 — origin fields + `project_dir` filter
- **Function:** `list_sessions` (F1.4 session origin)
- **Goal:** Every summary carries top-level `project_dir` and `launch_surface` (null when the format has no signal — e.g. Antigravity `project_dir`, OpenCode/Pi `launch_surface`), and the `project_dir` filter narrows the inventory exact-or-descendant, path-boundary aware.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` (a) a Claude transcript with record-level `cwd="/home/u/dev/x"`, (b) a Claude transcript with `cwd="/home/u/dev/x/sub"`, (c) a Claude transcript with `cwd="/home/u/dev/xy"`, (d) an Antigravity brain under `.gemini/antigravity-cli/brain/`.
- **Steps:** `mcp__ai-r__list_sessions()`; then `mcp__ai-r__list_sessions(agent="claude", project_dir="/home/u/dev/x")`; then `project_dir="   "`.
- **Expected:** Claude summaries carry `project_dir` = the seeded cwd and `launch_surface="claude-cli"`; the Antigravity summary carries `project_dir=null` and `launch_surface="antigravity-cli"` (fields present, null where no signal, nothing fabricated); the filtered call returns exactly the `/home/u/dev/x` and `/home/u/dev/x/sub` sessions (sibling `/home/u/dev/xy` excluded); the blank filter returns `{"error": "invalid_argument", …}`.
- **Pass criteria:** GO when both fields are top-level on every summary, null exactly where the format has no signal, and the filter is boundary-exact with a loud blank-value error.

### LIST-6 — recency signal (`last_activity` / `age_sec` / `activity`), honest not-liveness
- **Function:** `list_sessions` (A3 session recency)
- **Goal:** Every summary carries `last_activity` (== `date`, kept), `age_sec` (whole seconds, clamped ≥ 0) and `activity` (`"fresh"`/`"stale"`); the fresh/stale cut honors `AI_R_STALL_SEC`; the verdict is about record recency only, never process liveness.
- **Preconditions:** `[hermetic-ok]` — seed one Claude transcript under `AI_R_HOME` whose last record timestamp is well in the past (fixture default), so its age relative to the test-run clock is deterministic-ish (large).
- **Steps:** `mcp__ai-r__list_sessions(agent="claude")` with `AI_R_STALL_SEC` unset (default 600); then re-run with `AI_R_STALL_SEC="999999999"`; then with `AI_R_STALL_SEC="0.001"`; then with a blank `AI_R_STALL_SEC=""`.
- **Expected:** Every summary carries `last_activity` (equal to `date`), an integer `age_sec >= 0`, and `activity ∈ {"fresh","stale"}`; `age_sec > threshold ⇔ activity=="stale"`. With the huge threshold the session reads `"fresh"`; with `0.001` it reads `"stale"` (`age_sec > 0`); the blank threshold behaves as the 600 default (no crash). `date` is unchanged (backward compatible).
- **Pass criteria:** GO when all three fields are present on every summary, `activity` is consistent with `age_sec` and the active threshold, the env override and default both take effect, and a clock-skew future timestamp would clamp `age_sec` to 0 (never negative). The verdict is a recency statement, not a liveness claim — no field asserts the process is alive.

</details>

---

## `resume_command` (session summary field)

Every session summary (`list_sessions` / `read_session` / `search_sessions` candidates) carries
`resume_command` (F2.2): the ready-to-run shell one-liner that reopens the session in its agent's
CLI, or `null` where no real command exists. The CLI `ai-r list --json` / `ai-r read --json`
summaries carry the same field, and `detect_current` reports the **current** session's
`resume_command`. Text only — **the scenario never executes the
command**; it validates the string shape. Semantics SSOT: `src/ai_r/resume.py`;
spec: `docs/methods.md` → *Resume command*.

<details>
<summary>Show 2 scenarios (RES-1…RES-2)</summary>

### RES-1 — per-agent command shape + honest nulls
- **Function:** `list_sessions` (F2.2 `resume_command` in every summary)
- **Goal:** Each agent's summary carries the correct resume command text — `cd`-prefixed when `project_dir` is known — and `null` exactly where no command exists (Antigravity always; subagent sessions; a reference-only Claude Desktop session). Nothing is executed.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME`: (a) a Claude transcript with record-level `cwd="/home/u/dev/x"`, (b) a Claude subagent (sidechain) session, (c) a Codex session with `session_meta.payload.cwd`, (d) an OpenCode DB row with `session.directory`, (e) a Pi session with a header `cwd`, (f) an Antigravity brain dir, (g) a Desktop-only Claude metadata JSON with no backing transcript.
- **Steps:** `mcp__ai-r__list_sessions()`; inspect `resume_command` on every summary. Do NOT run any of the returned commands.
- **Expected:** Claude (a) → `cd /home/u/dev/x && claude --resume <uuid>`; Codex (c) → `cd <cwd> && codex resume <uuid>`; OpenCode (d) → `cd <directory> && opencode --session <id>`; Pi (e) → `cd <cwd> && pi --session <session-file-path>` (path form, not id); Antigravity (f) → `null`; the subagent session (b) → `null`; the Desktop-only reference (g) → `null`. A session without `project_dir` gets the bare command (no fabricated `cd`).
- **Pass criteria:** GO when every non-null command matches its agent's documented shape with shell-quoted values, and `null` appears exactly on Antigravity / subagent / reference-only summaries — never an invented command. NO-GO if a command is fabricated where the CLI has no resume verb.

### RES-2 — `detect_current` + CLI surface, honest null on incomplete identity
- **Function:** `detect_current` (F2.2 `resume_command` for the current session) + CLI `ai-r list --json`
- **Goal:** `detect_current` reports the resume command of the session it just detected (same SSOT string as that session's summary), and honestly `null` when identity is incomplete or the detected id has no transcript in the store; the CLI `ai-r list --json` summary carries the same `resume_command` field as the MCP summary — `null` projected, never omitted. Nothing is executed.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` a Claude transcript with a UUID-shaped session id and record-level `cwd="/tmp/work-x"`; set `CLAUDE_CODE_SESSION_ID` to that id (and blank the other detect env vars).
- **Steps:** `mcp__ai-r__detect_current()`; then unset the env var and call `detect_current()` again; then set `CLAUDE_CODE_SESSION_ID` to a UUID-shaped id absent from the store and call a third time; finally run `ai-r list --agent claude --json` and inspect the seeded session's row. Do NOT run any returned command.
- **Expected:** First call → `resume_command == "cd /tmp/work-x && claude --resume <uuid>"` (identical to the session's `list_sessions` summary field). Second call (no identity) → `resume_command` present and `null`. Third call (id not in store) → `null`, no error. The CLI row carries the same `resume_command` string as the MCP summary.
- **Pass criteria:** GO when the detected command string equals the summary's field byte-for-byte, both no-identity and not-in-store cases yield `null` (key present, never omitted, never a fabricated command), and the CLI JSON mirrors the MCP value. NO-GO if `detect_current` invents a command without a matching stored session.

</details>

---

## `outcome` (read_session field)

Every `read_session` response carries `outcome` (F2.3): a session-outcome classification from two
honest signals — the tool-call error rate (real per-result flag only for Claude/OpenCode) and a
calibrated bilingual (ru+en) success/failure word dictionary over the closing *human* user turns.
`status` is `success` / `failure` / `mixed` / `unknown`; with no signal the status is an honest
`unknown` — never a guess. Every deciding reason is spelled out in `signals` (empty ⇔ `unknown`).
The block carries only ai-r-authored strings and dictionary marker labels, never raw session text.
Semantics SSOT: `src/ai_r/outcome.py`.

<details>
<summary>Show 2 scenarios (OUT-1…OUT-2)</summary>

### OUT-1 — decision table: words × error rate, honest unknown
- **Function:** `read_session` (F2.3 `outcome` block)
- **Goal:** The four decision-table rows classify correctly and every deciding reason is named in `signals`; a no-signal session is `unknown` with empty `signals`, never a fabricated verdict.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` four Claude sessions: (a) closing user turn «Отлично, работает!» and no failed tool calls; (b) closing user turn «Не работает, откати»; (c) 4 tool results of which 3 carry `is_error: true` and a *neutral* closing user turn; (d) a plain hello-world exchange with no tool calls and no verdict words.
- **Steps:** `mcp__ai-r__read_session(uuid=<a|b|c|d>, agent="claude")` for each; inspect `outcome`.
- **Expected:** (a) `status="success"`, `user_verdict="positive"`, a `signals` entry naming the matched markers; (b) `status="failure"`, `user_verdict="negative"`, markers listed under `markers.negative`; (c) `status="failure"` with a `signals` entry naming the error rate (`0.75 (3/4)`), `tool_results=4`, `tool_errors=3`, `error_rate=0.75`, `error_rate_reliable=true`; (d) `status="unknown"` with `signals=[]`. In every case the block contains no raw transcript text (only dictionary labels and ai-r-authored strings).
- **Pass criteria:** GO when all four statuses match, `signals` is empty exactly on the unknown case and names each deciding reason otherwise, and no raw session text appears inside `outcome`. A verdict on the no-signal session is NO-GO.

### OUT-2 — honest nulls for agents without an error flag
- **Function:** `read_session` (F2.3 `outcome` block, unreliable-flag agents)
- **Goal:** For Codex/Pi/Antigravity — whose formats carry no per-result error flag — the error-rate fields are `null` (never derived from guesswork), while the word dictionary still classifies; with no verdict words the status stays `unknown`.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` a Codex session with several `function_call`/`function_call_output` records and a closing user message without verdict words; optionally a second Codex session whose closing user message is «спасибо, работает».
- **Steps:** `mcp__ai-r__read_session(uuid=<codex-uuid>, agent="codex")`; inspect `outcome`; repeat for the verdict-word variant.
- **Expected:** `error_rate_reliable=false`, `tool_errors=null`, `error_rate=null` while `tool_results` still counts the outputs; the wordless session is `status="unknown"` (`signals=[]`); the «спасибо, работает» variant is `status="success"` on the word signal alone.
- **Pass criteria:** GO when the error fields are `null` exactly for the unreliable-flag agent (no invented error rate), `tool_results` is still counted, and the status changes only on the word signal. A non-null `error_rate` for Codex/Pi/Antigravity is NO-GO.

</details>

---

## `find_tool_calls`

Cross-agent tool-call search by exact name or substring pattern; the name filter is optional (content filters can carry the selection), but a call with no filter at all fails loud.

<details>
<summary>Show 5 scenarios (FTC-1…FTC-5)</summary>

### FTC-1 — exact vs pattern search, cross-agent, fail-loud arg contract
- **Function:** `find_tool_calls`
- **Goal:** Locate tool invocations across every agent by exact name or substring, and reject a fully unfiltered call instead of returning a misleading empty list.
- **Preconditions:** A vault where at least one agent recorded tool calls (e.g. `Read`, an edit tool). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__find_tool_calls(tool_name="Read", limit=20)`; then `mcp__ai-r__find_tool_calls(tool_name_pattern="edit", limit=20)`; then the invalid `mcp__ai-r__find_tool_calls()` (NO name, NO pattern, and NONE of `input_contains`/`output_contains`/`output_excludes`/`is_error`).
- **Expected:** The exact call returns only `Read` calls (case-insensitive), spanning whichever agents recorded them; the pattern call returns calls whose tool name contains `edit` (case-insensitive); the fully-unfiltered call returns `{"error": "invalid_argument", "message": …}`. Setting BOTH `tool_name` and `tool_name_pattern` is likewise rejected; omitting the name while passing at least one content filter is valid (see FTC-3).
- **Pass criteria:** GO when exact and pattern searches both return correct cross-agent matches AND the no-filter-at-all call returns the `invalid_argument` error shape — never a silent empty result.

### FTC-2 — each record surfaces the correlated `is_error` outcome + `output`
- **Function:** `find_tool_calls`
- **Goal:** A tool-call record carries whether the call succeeded or failed and the correlated tool-result content, without changing the exact/pattern match set.
- **Preconditions:** A claude (or opencode) session with BOTH a known-succeeded and a known-failed call of the same tool — e.g. a `Bash` that exited zero and another that exited non-zero. For Claude the failed one may carry the error only in format (`<tool_use_error>` content prefix or `toolUseResult: "Error: …"`) with no explicit `is_error` flag — the parser derives it. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__find_tool_calls(tool_name="Bash", limit=50)`; inspect `is_error` and `output` on the returned records; cross-check the failed one against `read_session` (it should render `[tool_result ERROR: …]`).
- **Expected:** Every record carries `is_error` — `True` for the known-failed call, `False` for the succeeded one — and an `output` field holding the correlated tool-result content (char-capped at 2000; when sliced, `output` is listed in that record's `truncated_fields`). Correlation is by tool_use_id (Claude `tool_use.id` / OpenCode `callID`); the returned match set (which records) is unchanged by the two fields.
- **Pass criteria:** GO when `is_error` reflects the real outcome and `output` carries the correlated result for Claude/OpenCode, and the exact-name match set is identical with or without inspecting the fields. Codex/Pi always reporting `is_error=False` (no source flag), Antigravity emitting no tool results, and an uncorrelated call defaulting to `is_error=False`/empty `output` are **documented** best-effort limitations (see `docs/methods.md` → *Output bounds & tool-call outcome*), not failures.

### FTC-3 — flexible connective filtering (domain × error, minus noise)
- **Function:** `find_tool_calls`
- **Goal:** Composing `input_contains` + `is_error` (+ `output_excludes` for noise) returns only the real command failures of a chosen domain, not raw `is_error` noise — proving there is no need for a special "error + domain" verb.
- **Preconditions:** A claude/opencode vault with failed calls of some domain (e.g. `git`) AND some failures whose `output` is harness noise carrying a stable marker (e.g. a security-gate line). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__find_tool_calls(input_contains="git", is_error=True, limit=50)`; note the count; then add the noise filter: `mcp__ai-r__find_tool_calls(input_contains="git", is_error=True, output_excludes="BOUNDARY_CHECKED", limit=50)` (substitute whatever harness marker the vault uses).
- **Expected:** The filters intersect by AND: the first call returns only records whose input contains `git` **and** whose `is_error` is `True` — a count far below the raw `is_error=True` total (which spans every domain). Adding `output_excludes` drops the records whose `output` carries the marker, shrinking the set further. `git` is only an example domain, not a hard-coded case; the same holds for any `input_contains` value.
- **Pass criteria:** GO when the composition yields the "domain × error" pairing (strictly fewer than either filter alone) AND `output_excludes` removes the marked noise records — never a special verb, never a hard-coded marker list.

### FTC-4 — adaptive smart output truncation keeps a trailing error
- **Function:** `find_tool_calls`
- **Goal:** A long output with the error at the **end** must not lose the error to a head-only cut; the default adaptive mode (or explicit `output_mode="smart"`) surfaces the error line even when `output` is truncated.
- **Preconditions:** A claude/opencode session with a failing call (`is_error=True`) whose tool result is longer than the 2000-char cap and whose error line (`error`/`fatal`/`traceback`) sits near the end. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__find_tool_calls(is_error=True, limit=20)`; pick a record whose `output` is in `truncated_fields`; inspect its `output` under the default (adaptive); then re-fetch with `mcp__ai-r__find_tool_calls(is_error=True, output_mode="head", limit=20)` and compare the same record's `output`.
- **Expected:** Under the default (adaptive → `smart` for `is_error==True`) or explicit `output_mode="smart"`, the truncated `output` still contains the trailing error line, and `output` is listed in that record's `truncated_fields`. The same record under `output_mode="head"` may cut before the error line, losing it (head keeps only the first cap chars). Codex/Pi records (always `is_error=False`) fall to the `head` legacy path — expected, not a failure.
- **Pass criteria:** GO when the adaptive/`smart` mode preserves the trailing error line for a failing call while `head` on the identical output drops it.

### FTC-5 — records carry wrapper-aware `tool_kind` + `tool_resolved`
- **Function:** `find_tool_calls`
- **Goal:** Every record classifies the call (`tool_kind`) and names the real actor under a Skill/Task/MCP wrapper (`tool_resolved`) — honest `null` when the input carries no name signal.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` a Claude session with a `Skill` call (`input.skill="ai-local-reader"`), an `mcp__ai-r__query` call and a plain `Bash` call. `[needs-real-vault]` variant: any vault with subagent spawns (Claude `Task`/`Agent`, Codex `spawn_agent`).
- **Steps:** `mcp__ai-r__find_tool_calls(tool_name="Skill", agent="claude")`; then `mcp__ai-r__find_tool_calls(tool_name_pattern="mcp__", agent="claude")`; then `mcp__ai-r__find_tool_calls(tool_name="Bash", agent="claude")`.
- **Expected:** The Skill record carries `tool_kind="skill"`, `tool_resolved="ai-local-reader"`; the MCP record `tool_kind="mcp"`, `tool_resolved="ai-r:query"` (`<server>:<tool>` from the `mcp__<server>__<tool>` name); every Bash record `tool_kind="bash"`, `tool_resolved=null` (nothing to resolve); the fields are additive — the pre-F3.1 record shape (`tool`/`input`/`is_error`/…) is unchanged.
- **Pass criteria:** GO when both wrapper records resolve to the real names, non-wrapper records carry `tool_resolved=null` (never a guessed value), and existing record fields/counts are unaffected.

</details>

---

## `read_session`

Read one session by `uuid`+`agent`, projected to the compact `{role, content}` MCP shape, paginated.

<details>
<summary>Show 6 scenarios (READ-1…READ-6)</summary>

### READ-1 — read by uuid+agent → projected shape + pagination echo
- **Function:** `read_session`
- **Goal:** A single session reads into the compact `{role, content}` projection with correct metadata and pagination echo.
- **Preconditions:** A known session uuid + its agent (e.g. the newest from `list_sessions`). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__read_session(uuid="<uuid>", agent="claude", offset=0, limit=20)`.
- **Expected:** `{uuid, agent, title, date, message_count, kind, parent_uuid, messages:[{role, content, timestamp?}], total, offset, limit, messages_truncated}`; each message `role` is `user`/`assistant`; assistant tool-call turns surface a `[tool_use: <name> …]` summary in `content`; tool results render as `[tool_result ok: <snippet>]` or `[tool_result ERROR: <snippet>]` (not a bare `[tool_result]`); `messages` is the slice `[offset:offset+limit]` and `total` is the full projected count.
- **Pass criteria:** GO when the metadata block is present, every message role is `user`/`assistant`, tool results render with an `ok`/`ERROR` outcome (never the bare `[tool_result]` placeholder), the slice honors `offset`/`limit`, and `total >= len(messages)`.

### READ-2 — pagination slice + `total` invariance
- **Function:** `read_session`
- **Goal:** `offset`/`limit` page through the same projected list without changing `total`.
- **Preconditions:** A session with more than `limit` projected messages. `[needs-real-vault]`.
- **Steps:** call `read_session(uuid, agent, offset=0, limit=5)`, then `read_session(uuid, agent, offset=5, limit=5)`; compare.
- **Expected:** The two pages are disjoint, consecutive slices of one ordered message list; `total` is identical across both calls (independent of the slice); the pagination echo (`offset`/`limit`) mirrors the request.
- **Pass criteria:** GO when page 2 continues page 1 (no overlap, no gap), `total` is stable across both calls, and each response echoes the requested `offset`/`limit`.

### READ-3 — agent-free lookup by id (+ collision → candidates)
- **Function:** `read_session`
- **Goal:** Omitting `agent` resolves a session by id across every parser; a cross-agent id collision returns a disambiguation list, never an error.
- **Preconditions:** A known uuid from `list_sessions`. `[needs-real-vault]` for the live lookup; the collision branch is `[hermetic-ok]` (synthetic duplicate id under two agents).
- **Steps:** `mcp__ai-r__read_session(uuid="<uuid>")` (no `agent`); compare with `mcp__ai-r__read_session(uuid="<uuid>", agent="<its agent>")`; then `mcp__ai-r__read_session(uuid="no-such-id-zzz")`; (hermetic) seed the same id under two agents and call without `agent`.
- **Expected:** The agent-free result is identical to the explicit-agent result; the miss returns `{error:"not_found", agent:null, agents_scanned:[all 5 parsers]}`; the synthetic collision returns `{ambiguous:true, candidates:[…], count:2}` where each candidate carries its `agent` — and NO `error` key.
- **Pass criteria:** GO when agent-free == explicit-agent byte-for-byte, the miss names every scanned parser, and a collision yields `candidates` instead of an error. An `error` on a resolvable collision is NO-GO.

### READ-4 — `with_tokens=true`: session `component_tokens` estimate + per-message exact blocks (Claude) `[needs-real-vault]`
- **Function:** `read_session` (F3.3 follow-up `with_tokens`)
- **Goal:** A Claude session yields `tokens` (flat exact-or-estimate) AND `component_tokens` — a per-component estimate over ai-r's existing event taxonomy whose scalars + `tool_call` map sum to the total, plus per-message EXACT `tokens` blocks on assistant messages, deduplicated per API call.
- **Preconditions:** A Claude session that records per-call `message.usage` (multiple assistant API calls, ideally with streamed duplicates sharing a `(message.id, requestId)`), ideally including a plan-authoring call (`ExitPlanMode` / `Write plans/*.md`). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__read_session(uuid="<uuid>", agent="claude", with_tokens=true)`; inspect `tokens`, `component_tokens` and per-message `tokens`.
- **Expected:** `tokens` is the flat `session_tokens` exact-or-estimate block; `component_tokens` = `{user_turn, assistant_turn, thinking, plan, tool_call: {<tool_kind>: n}, total, source:"estimate", estimator}` over ai-r's existing taxonomy (reused classifiers — `resolve_tool`, the plan-signal detector, the user/assistant role — not a second classifier); `total == user_turn + assistant_turn + thinking + plan + sum(tool_call.values())`; a plan-authoring call (`ExitPlanMode` / `Write plans/*.md`) counts under `plan`, NEVER under `tool_call` (no double count); every other call's `input` + its correlated `tool_result` content is bucketed by `tool_kind`; ONE estimator drives every surface, so `component_tokens.source == "estimate"` even when `tokens` is exact (tiers never merged); an empty transcript would give `component_tokens: null`. Assistant message entries where the format records per-message usage carry an EXACT `tokens` block; the same `(message.id, requestId)` API call is counted ONCE (dedup on absolute positions BEFORE pagination), never double-counted across streamed duplicates; user turns carry NO `tokens` key (absent, not null). Every emitted token value is an integer + ai-r label (no raw session text).
- **Pass criteria:** GO when `component_tokens.total == sum(user_turn, assistant_turn, thinking, plan) + sum(tool_call.values())`, a plan-authoring call lands under `plan` and not `tool_call`, `component_tokens.source == "estimate"` regardless of the `tokens` tier, per-message exact blocks appear only on assistant messages the format records usage for and each API call is deduped (no inflated per-message total), and no `tokens` key leaks onto user turns. A plan-authoring call double-counted into `tool_call`, a `component_tokens` tier merged into an exact `tokens`, or a double-counted streamed call, is NO-GO.

### READ-5 — default byte-identical + `include_subagents` rollup + Codex per-message absent `[hermetic-ok]`
- **Function:** `read_session` (F3.3 follow-up `with_tokens` / `include_subagents`)
- **Goal:** The default `with_tokens=false` is byte-identical to the historical output; `include_subagents=true` on a parent folds its subagents into a `subagent_rollup`; and a Codex session (cumulative-only usage) carries NO per-message `tokens` keys.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` (a) a small Claude parent session with ONE spawned subagent child (child's `parent_uuid` set to the parent), (b) a Codex session whose usage is recorded only cumulatively (`token_count`), with no per-message usage.
- **Steps:** `mcp__ai-r__read_session(uuid="<parent-uuid>", agent="claude")` (default) and the same call with `with_tokens=false` — compare; then `mcp__ai-r__read_session(uuid="<parent-uuid>", agent="claude", include_subagents=true)`; then `mcp__ai-r__read_session(uuid="<codex-uuid>", agent="codex", with_tokens=true)`; inspect message entries.
- **Expected:** The default call and the explicit `with_tokens=false` call are byte-identical, and neither carries a `tokens`/`component_tokens` block nor any per-message `tokens` key (historical shape unchanged). The `include_subagents=true` call carries `subagent_rollup` = `{parent, children:[{uuid, agent, component_tokens}], total}` — exactly ONE child (the seeded subagent, resolved via `children_of(parent_uuid)`) and a `total` that folds the parent's `component_tokens` with the child's; a childless parent — or Antigravity, which records no `parent_uuid` — would yield an honest empty `children` list, never a fabricated child. The Codex `with_tokens=true` call carries `tokens` + `component_tokens` (`source="estimate"`) but NO message entry carries a `tokens` key — Codex is cumulative-only, so per-message exact blocks are absent (not null), exactly like Antigravity and user turns.
- **Pass criteria:** GO when omitting `with_tokens` (or passing `false`) reproduces the pre-feature output byte-for-byte, `include_subagents=true` returns a `subagent_rollup` with the single seeded child and a folded `total`, and the Codex session exposes zero per-message `tokens` keys. A per-message `tokens` key on a Codex message, a fabricated child on a childless parent, or any diff in the default output, is NO-GO.

### READ-6 — thinking opt-in: default byte-identical, `include_thinking=true` adds a separate field (Q2) `[hermetic-ok]`
- **Function:** `read_session` + `get_body` (Q2 `include_thinking`)
- **Goal:** Reasoning is captured but kept out of the default output; `read_session(include_thinking=false)` is byte-identical to before, `include_thinking=true` adds a SEPARATE `thinking` field (never merged into `content`), and `get_body` mirrors it; `has_thinking` is a hint that does not change event identity.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME` one Claude session with an assistant turn that carries a reasoning/thinking block plus its normal answer text, and at least one assistant turn with NO reasoning. `[needs-real-vault]` variant: any Claude/Codex/OpenCode/Pi session with recorded reasoning.
- **Steps:** `mcp__ai-r__read_session(uuid="<uuid>", agent="claude")` (default) and the same with `include_thinking=false` — compare; then `mcp__ai-r__read_session(uuid="<uuid>", agent="claude", include_thinking=true)`; then `mcp__ai-r__query(agent="claude", session="<uuid>", type="assistant_turn")` → inspect `has_thinking`; take the reasoning turn's `id` and call `mcp__ai-r__get_body(id="<id>")` then `mcp__ai-r__get_body(id="<id>", include_thinking=true)`.
- **Expected:** The default call and the explicit `include_thinking=false` call are byte-identical, and NEITHER carries a `thinking` field on any entry (historical shape — reasoning was never inlined into `content`). The `include_thinking=true` call adds a `thinking` STRING field next to `content` only on entries whose turn carries reasoning — kept separate, never concatenated into `content`. The `assistant_turn` events carry `has_thinking` (`true` on the reasoning turn, `false`/absent otherwise); the reasoning turn's `get_body` default returns the body with NO `thinking`, while `include_thinking=true` returns the additional `thinking` field. Antigravity turns always report `has_thinking=false` (no signal).
- **Pass criteria:** GO when the default output is byte-identical to the pre-flag shape (no `thinking` anywhere), `include_thinking=true` surfaces reasoning as a distinct field on exactly the reasoning-bearing entries (never fused into `content`), `has_thinking` reflects presence, and `get_body` opt-in mirrors it. Reasoning leaking into the default `content`, or a `thinking` field on a no-reasoning turn, is NO-GO.

</details>

---

## `search_sessions`

Case-insensitive cross-agent session search: `title`/`body`/`all` scope, `AND`/`OR`/`NOT` + negative `-term` + quoted phrases, BM25 or date sort.

<details>
<summary>Show 4 scenarios (SRCH-1…SRCH-4)</summary>

### SRCH-1 — title scope, AND default, relevance sort
- **Function:** `search_sessions`
- **Goal:** A multi-word query defaults to AND over titles, ranked by BM25 relevance.
- **Preconditions:** A vault with sessions whose titles share distinctive words. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__search_sessions(query="<word-a> <word-b>", scope="title", sort="relevance", limit=10)`.
- **Expected:** The call returns `{"results": [...], "count": N}`; `count` equals `len(results)`; every item in `results` has a title containing BOTH terms (AND default); order is BM25 relevance, not date; each summary carries the session identity fields (`uuid`, `agent`, `title`, `date`, `kind`).
- **Pass criteria:** GO when the wrapper carries `results`/`count`, all survivors in `results` satisfy the AND-of-terms over the title, and the top hit is the strongest textual match (relevance ordering, not chronological).

### SRCH-2 — body scope returns a snippet
- **Function:** `search_sessions`
- **Goal:** `scope="body"` matches message text / tool input / tool result — not the title — and returns a matching `snippet`.
- **Preconditions:** A vault with a distinctive term occurring in message bodies but NOT in any title. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__search_sessions(query="<body-only term>", scope="body", limit=10)`; then the same term with `scope="title"` as a control.
- **Expected:** The call returns `{"results": [...], "count": N}`; body-scope finds the term and each item in `results` carries a `snippet` (≤200 chars) containing it; the `scope="title"` control returns a wrapper with fewer/no `results`.
- **Pass criteria:** GO when body-scope's `results` find the term, every match carries a snippet with the term, and the title control confirms the match came from the body (not the title).

### SRCH-3 — operators: OR widens, negative `-term` excludes, quoted phrase is contiguous
- **Function:** `search_sessions`
- **Goal:** `operator` and the Google-style prefixes change the result set exactly as specified.
- **Preconditions:** A vault with overlapping terms. `[needs-real-vault]`.
- **Steps:** run the same two terms with `operator="AND"` then `operator="OR"`; then a query with a `-<term>` negative prefix; then a `"quoted phrase"`.
- **Expected:** Each call returns `{"results": [...], "count": N}`; comparing the `results` lists, `OR` never returns fewer than `AND` (`set(AND) ⊆ set(OR)`); a `-term` excludes every session containing that term regardless of operator; a quoted phrase matches only the contiguous phrase, not the words scattered.
- **Pass criteria:** GO when, over the `results` of each wrapper, `set(AND) ⊆ set(OR)`, the negative term removes all its matches, and the quoted phrase matches contiguously.

### SRCH-4 — `noise` filter: exclude/only the subagent tree
- **Function:** `search_sessions`
- **Goal:** A term that matches only inside a subagent session disappears under `noise="exclude"` and survives under `noise="only"`; an unknown mode fails loud.
- **Preconditions:** One top-level + one subagent session for the same agent, where a distinctive term occurs only in the subagent's body. `[hermetic-ok]` (seed a fake parent + subagent pair under `AI_R_HOME`).
- **Steps:** `mcp__ai-r__search_sessions(query="<term>", agent="<agent>", scope="body")` (default include); then the same with `noise="exclude"`; then `noise="only"`; then `noise="bogus"`.
- **Expected:** Default and `only` return the subagent session; `exclude` returns zero results (plus `diagnostics` echoing `noise`); `noise="bogus"` returns `{"error": "invalid_argument", …}` naming `noise`.
- **Pass criteria:** GO when the subagent match is present under include/only, absent under exclude, and the unknown mode errors loudly instead of silently ignoring the filter.

</details>

---

## Empty-result diagnostics (cross-cutting)

A zero-result response of a scanning method (`query` / `search_sessions` / `find_tool_calls` /
`find_file_edits` / `list_sessions`) must explain itself: which agents were scanned (session
counts, date bounds, `source_found`), the corpus totals, and cause hints. Non-empty responses
never carry `diagnostics`.

<details>
<summary>Show 2 scenarios (DIAG-1…DIAG-2)</summary>

### DIAG-1 — zero-result response carries diagnostics; non-empty does not `[needs-real-vault]`
- **Function:** `query` + `search_sessions` (representative of all scanning methods)
- **Goal:** An empty result is explainable — never a bare empty list — while a non-empty result stays unchanged.
- **Preconditions:** A non-empty vault. `[needs-real-vault]` (the same shape holds hermetically on an empty vault).
- **Steps:** `mcp__ai-r__query(text="zzz-improbable-needle-19cf", limit=10)` (expect 0 hits); inspect `diagnostics`; then `mcp__ai-r__query(type="user_turn", limit=1)` (expect ≥1 hit) and confirm NO `diagnostics` key; repeat the pair with `mcp__ai-r__search_sessions(query="zzz-improbable-needle-19cf")`.
- **Expected:** The empty responses carry `diagnostics` with: `scanned` (one entry per agent — `sessions`, `date_min`/`date_max`, `source_found`, per-agent `hint` for empty/missing sources), `corpus` (total sessions + overall date bounds, plausible for the vault), `filters` (echoing the call's filters, e.g. `text`), and non-empty `hints`. The non-empty responses carry no `diagnostics` key at all.
- **Pass criteria:** GO when `diagnostics` appears exactly on the zero-result responses, per-agent session counts are plausible, and the filter echo matches the call. A bare `{results/events: [], count: 0}` without diagnostics is NO-GO.

### DIAG-2 — cause hints: missing source dir and all-excluding date filter
- **Function:** `find_tool_calls` (hints are shared by all scanning methods)
- **Goal:** The two diagnosable causes are named explicitly: a source directory that does not exist, and a `since`/`until` bound that excludes the whole corpus.
- **Preconditions:** none. `[hermetic-ok]` (point `AI_R_HOME` at an empty directory; seed one synthetic claude session with a tool call for the date case).
- **Steps:** (a) with no agent data at all: `mcp__ai-r__find_tool_calls(tool_name="Bash", agent="claude")` → inspect `diagnostics.scanned[claude]`; (b) with one seeded session dated 2026: `mcp__ai-r__find_tool_calls(tool_name="Bash", agent="claude", since="2999-01-01")` → inspect `diagnostics.hints`.
- **Expected:** (a) `scanned[claude].source_found == false` and its `hint` names the missing path (`source not found: …/.claude/projects`); (b) the corpus is non-empty and a hint states that `since='2999-01-01'` is after the newest session and "excludes the entire corpus".
- **Pass criteria:** GO when the missing-source case names the looked-at path and the date case names the excluding bound with the corpus boundary. A generic "no results" with no cause is NO-GO.

</details>

---

## Secret redaction (cross-cutting)

Every method that emits session-derived text masks secrets on output by default (F2.1):
replacements are `[REDACTED_<TYPE>]`, the response carries a per-type `redactions` counter when
anything was masked, and `redact=false` returns the raw content. Redaction is **emission-time
only** — filters and search always match the RAW stored text. Pattern SSOT: `src/ai_r/redact.py`;
behaviour spec: `docs/methods.md` → *Redaction*.

<details>
<summary>Show 5 scenarios (RED-1…RED-5)</summary>

### RED-1 — secrets masked by default, counter present, `redact=false` returns raw
- **Function:** `read_session` + `query` (representative of all emitting methods)
- **Goal:** A transcript containing a pasted secret never leaks it through the default surface, and the caller can still get the raw bytes on explicit request.
- **Preconditions:** none. `[hermetic-ok]` (seed one synthetic claude session whose user turn contains a fake key, e.g. `sk-abc123def456ghi789jkl012mno`, and whose Bash tool input contains `PASSWORD=hunter2x9extra`).
- **Steps:** `mcp__ai-r__read_session(<uuid>, agent="claude")` → scan the full JSON for the raw secret; then the same call with `redact=false`; repeat the pair with `mcp__ai-r__query(session=<uuid>, type="user_turn")`.
- **Expected:** Default responses contain `[REDACTED_OPENAI_KEY]` / `[REDACTED_GENERIC_SECRET]` and NO raw secret anywhere; each carries `redactions` (e.g. `{"OPENAI_KEY": 1, …}`). The `redact=false` responses contain the raw values and NO `redactions` key.
- **Pass criteria:** GO when the raw secret is absent from every default response, the per-type counter matches what was masked, and `redact=false` round-trips the raw content. A raw secret in a default response, or a `redactions` counter without any masking, is NO-GO.

### RED-2 — matching runs on RAW text; benign look-alikes stay untouched
- **Function:** `search_sessions` + `query(text=…)`
- **Goal:** Redaction never changes what is findable — only what is displayed — and the pattern table does not fire on identifiers.
- **Preconditions:** the RED-1 seeded session. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__search_sessions(<the raw fake key>, agent="claude")` → expect the session found, snippet masked; `mcp__ai-r__query(session=<uuid>, text=<the raw fake key>)` → expect ≥1 event; then read a session containing only uuids / git hashes / `sk-learn` prose and confirm zero `redactions`.
- **Expected:** Searching the literal secret finds its session (`count ≥ 1`) while the emitted snippet/text shows `[REDACTED_*]`; benign identifier text comes back byte-identical with no `redactions` key.
- **Pass criteria:** GO when raw-text matching and masked display hold simultaneously and no false positive fires on uuid/hash/prose. A search that misses because of redaction, or a masked uuid, is NO-GO.

### RED-3 — empty result + secret-looking filter → redaction hint
- **Function:** empty-result diagnostics (F1.1 × F2.1 link)
- **Goal:** An empty search for a placeholder or a secret-shaped value explains the redaction semantics instead of leaving a bare zero.
- **Preconditions:** none. `[hermetic-ok]` (empty or seeded vault).
- **Steps:** `mcp__ai-r__query(agent="claude", text="[REDACTED_OPENAI_KEY]")` → inspect `diagnostics.hints`; then `mcp__ai-r__query(agent="claude", text="sk-zzz999zzz999zzz999zzz999")` (absent from the corpus) → inspect `diagnostics.hints`.
- **Expected:** The placeholder case yields a hint that placeholders never exist in stored text and can never match (search the raw value / use `redact=false`); the secret-shaped case yields a hint that redaction is enabled, matching ran on RAW text, and `redact=false` shows raw values. With `redact=false` on the call, neither hint appears.
- **Pass criteria:** GO when both hint variants appear exactly on the empty path and name `redact=false`. A bare empty result for a placeholder search is NO-GO.

### RED-4 — vendor key types masked (Stripe / JWT / Google) `[hermetic-ok]`
- **Function:** `read_session` (representative of the redaction surface)
- **Goal:** The pattern table covers the vendor formats an `sk-`/`AWS`-only table missed — a Stripe `sk_`/`rk_` key, a `eyJ…` JWT, a Google `AIza…` key — and still does not fire on look-alikes.
- **Preconditions:** none. `[hermetic-ok]` (seed one synthetic claude session whose turns paste a fake `sk_live_0123456789abcdefghij`, a fake three-segment `eyJhbGciOi….eyJzdWIi….sig` JWT, and a fake `AIzaSy0123456789012345678901234567890` key, plus benign look-alikes `sk_foo_bar` and a two-segment `eyJ.abc`).
- **Steps:** `mcp__ai-r__read_session(<uuid>, agent="claude")` → scan the JSON for the raw values and inspect `redactions`.
- **Expected:** The three real keys appear only as `[REDACTED_STRIPE_KEY]` / `[REDACTED_JWT]` / `[REDACTED_GOOGLE_API_KEY]`, `redactions` counts each once, and NO raw key text remains; the look-alikes are untouched (no over-masking).
- **Pass criteria:** GO when all three vendor formats are masked with the right type label and the look-alikes are left raw. A leaked vendor key, or a masked look-alike, is NO-GO.

### RED-5 — CLI redacts on output by default, `--no-redact` opts out `[hermetic-ok]`
- **Function:** `ai-r read` / `ai-r export` / `ai-r list` (CLI parity with the MCP `redact` default)
- **Goal:** The CLI honours the same F2.1 emission-time masking as the MCP verbs — a secret in a session never leaves through a default CLI print — with a symmetric `--no-redact` (alias `--raw`) opt-out.
- **Preconditions:** none. `[hermetic-ok]` (seed under `AI_R_HOME` one claude session whose title AND a message body contain a fake `ghp_0123456789abcdefghijklmnopqrstuvwxyzA`).
- **Steps:** `ai-r read <uuid> --messages` and `--json`; `ai-r export rounds <uuid>` (stdout and `--output <file>`); `ai-r list --json`; then repeat `ai-r read <uuid> --messages --no-redact`.
- **Expected:** Every default CLI path prints `[REDACTED_GITHUB_TOKEN]` and NO raw token (including the `--output` file and the `list` title); `--no-redact`/`--raw` prints the raw token.
- **Pass criteria:** GO when no default CLI surface leaks the secret and the opt-out flag round-trips raw. A raw secret in any default CLI output is NO-GO.

</details>

---

## MCP transport auth (cross-cutting)

The opt-in shared `streamable-http` transport is reachable over a socket, so — unlike stdio — a
caller is not necessarily the session owner. Two SDK-native controls apply: the `mcp` SDK's
DNS-rebinding/Origin allowlist (always on for the loopback default) and an opt-in bearer token
(`AI_R_HTTP_TOKEN`, constant-time compared), **required (fail-closed) for any non-loopback bind**.
Behaviour spec: `docs/architecture.md` → *ADR: shared http transport* (Transport auth). These
scenarios drive `ai_r.serve` helpers directly (no live socket needed).

<details>
<summary>Show 2 scenarios (SRV-1…SRV-2)</summary>

### SRV-1 — bearer token gate: 401 without / pass-through with `[hermetic-ok]`
- **Function:** `ai_r.serve` HTTP transport (bearer auth wrapper)
- **Goal:** When `AI_R_HTTP_TOKEN` is set, an HTTP request without a matching `Authorization: Bearer <token>` is rejected before reaching any tool; a correct token passes through.
- **Preconditions:** none. `[hermetic-ok]` (set `AI_R_HTTP_TOKEN` in the call env; exercise the ASGI wrapper with a fake request scope — no real network bind).
- **Steps:** send a request with no `Authorization` header; with `Authorization: Bearer wrong`; with `Authorization: Bearer <token>` (and a case-variant scheme `bearer`).
- **Expected:** The first two get `401` and never reach a tool handler; the correct token (any-case scheme) is forwarded to the inner app. The compare is constant-time (`hmac.compare_digest`).
- **Pass criteria:** GO when only the exact token is admitted and a missing/wrong token is a `401` that never invokes a tool. A tool call reachable without the token is NO-GO.

### SRV-2 — remote bind without a token is a hard refusal (fail-closed) `[hermetic-ok]`
- **Function:** `ai_r.serve` (`require_http_token` + host resolution)
- **Goal:** A non-loopback bind (`AI_R_MCP_ALLOW_REMOTE=1`) with no `AI_R_HTTP_TOKEN` set refuses to start rather than serving secret-bearing transcripts unauthenticated; the loopback default without a token still runs (relying on the DNS-rebinding allowlist).
- **Preconditions:** none. `[hermetic-ok]` (call the pure `require_http_token`/host resolver with assorted host + token combinations; no socket).
- **Steps:** resolve for a loopback host with no token (allowed); for a remote host with no token (refused); for a remote host WITH a token (allowed).
- **Expected:** Remote-without-token raises a hard error naming `AI_R_HTTP_TOKEN`; loopback-without-token and remote-with-token both proceed.
- **Pass criteria:** GO when the only refused combination is remote-without-token and the error is explicit and fail-closed. A remote server starting unauthenticated is NO-GO.

</details>

---

## Semantic sort (cross-cutting, F5.1)

`sort="semantic"` on the text-search surface (`query` with a `text` facet, `search_sessions`):
BM25 supplies the top-50 candidate pool, a local multilingual embedding model
(`intfloat/multilingual-e5-small`, int8 ONNX via onnxruntime + tokenizers, `query:`/`passage:`
prefixes applied internally) re-ranks it by meaning. Blended score = 75 % meaning + 25 % word match;
no similarity cut-off (re-order, never drop); tail beyond the pool keeps BM25 order. Optional
`ai-r[semantic]` extra; without it — honest BM25 fallback with a reason, never a crash.

<details>
<summary>Show 3 scenarios (SEM-1…SEM-3)</summary>

### SEM-1 — meaning beats word frequency `[needs-real-vault]` (requires `ai-r[semantic]` + model)
- **Function:** `query` / `search_sessions` with `sort="semantic"`
- **Goal:** A semantically close session outranks a higher word-frequency but off-topic one.
- **Preconditions:** `ai-r[semantic]` installed AND the model files present (`AI_R_EXTRAS=semantic bash install.sh`); a vault with sessions where a term appears both on-topic and off-topic.
- **Steps:** `mcp__ai-r__search_sessions(query="<term>", scope="body", sort="relevance")`, then the same call with `sort="semantic"`; compare top results.
- **Expected:** Both calls return the same match SET (semantic re-orders, never drops); the semantic top hit is the session whose *content* matches the query's meaning; the response carries `semantic: {active: true, model: "intfloat/multilingual-e5-small", candidates: ≤50, weight: 0.75}`.
- **Pass criteria:** GO when the match set is identical to the BM25 call, only the order differs, the top hit is semantically the right one, and the `semantic` report says `active: true` with the model name. A dropped result or a missing `semantic` field is NO-GO.

### SEM-2 — honest degradation without the extra `[hermetic-ok]`
- **Function:** `query` / `search_sessions` with `sort="semantic"`
- **Goal:** Without `ai-r[semantic]` (or without the model files) the call still works: BM25 order + a plain-words notice — never an exception.
- **Preconditions:** onnxruntime/tokenizers NOT importable, or the model dir empty (point `AI_R_SEMANTIC_MODEL_DIR` at an empty dir).
- **Steps:** `mcp__ai-r__search_sessions(query="<term>", scope="body", sort="semantic")` and `mcp__ai-r__query(text="<term>", sort="semantic")`; compare each with its `sort="relevance"` twin.
- **Expected:** Results and their order are byte-identical to the `relevance` call; the response carries `semantic: {active: false, reason: <mentions pip install "ai-r[semantic]" or the model dir + install.sh>, fallback: "bm25"}`; the `relevance`/`date` calls never carry a `semantic` field.
- **Pass criteria:** GO when the fallback order equals BM25, the reason is actionable (names the install command or the model dir), and no error/exception surfaces. A crash, an empty result caused by the missing extra, or a silent fallback without `reason` is NO-GO.

### SEM-3 — cross-lingual ru↔en retrieval `[needs-real-vault]` (requires `ai-r[semantic]` + model)
- **Function:** `search_sessions` / `query` with `sort="semantic"`
- **Goal:** A Russian query surfaces an English session about the same thing (and vice versa) — the project's hard multilingual requirement.
- **Preconditions:** `ai-r[semantic]` + model files; a vault containing sessions on one topic in English and another topic as noise; a query for that topic in Russian (e.g. query «ошибка сегментации» vs an English segfault-debugging session).
- **Steps:** pick a broad word-match query that catches both the on-topic English session and off-topic noise; run with `sort="relevance"` then `sort="semantic"`; compare the rank of the on-topic English session.
- **Expected:** Under `sort="semantic"` the on-topic English session ranks above the off-topic noise even though the query is Russian (E5 cross-lingual embedding space); the reverse direction (English query, Russian session) behaves symmetrically.
- **Pass criteria:** GO when the cross-language on-topic session demonstrably outranks off-topic same-language noise in at least one direction ru→en or en→ru (both preferred). NO-GO if semantic order equals BM25 order on a case where meaning and word frequency clearly disagree.

</details>

---

## CLI error contract

<details>
<summary>Show 1 scenario (CLI-1)</summary>

### CLI-1 — structured errors, non-zero exit, never a traceback
- **Function:** `ai-r` CLI (all subcommands)
- **Goal:** A failing CLI invocation never dumps a Python traceback into a consumer script — errors are structured and the exit code is non-zero.
- **Preconditions:** `ai-r` installed on PATH (or `python -m ai_r.cli`). `[hermetic-ok]`.
- **Steps:** run and capture `rc`/stderr for: `ai-r find-tool-calls --limit -1` (invalid argument); `ai-r read no-such-session-zzz --agent claude` (not found); `ai-r list --from-date junk` (bad date). Grep each stderr for `Traceback`.
- **Expected:** Every invocation exits non-zero (invalid argument → 2, not found → 3, bad date → 1); stderr carries a single structured line — `ai-r: <message>` for expected failures, or one JSON `{"error": "internal_error", "type": …, "message": …}` line for an unexpected internal failure — and `Traceback` appears nowhere. With `AI_R_DEBUG=1` an unexpected failure re-raises (traceback allowed then, by request).
- **Pass criteria:** GO when all failing invocations are traceback-free with a non-zero exit code and a parseable one-line error. Any Python traceback without `AI_R_DEBUG=1` is NO-GO.

</details>

## Unknown-argument fail-loud (cross-cutting)

<details>
<summary>Show 1 scenario (STRICT-1)</summary>

### STRICT-1 — an undeclared tool argument is rejected, not silently dropped `[hermetic-ok]`
- **Function:** every `mcp__ai-r__*` tool (transport-level `_StrictArgsFastMCP`)
- **Goal:** A caller that passes a parameter a verb does not declare gets a loud `invalid_argument`, never a successful-looking but unfiltered result — the failure a self-referential usage audit found in real history (`plan(limit=…)`, `list_sessions(since=…)`).
- **Preconditions:** a live ai-r MCP server; no vault data required (rejection happens before any read). `[hermetic-ok]`.
- **Steps:** call `plan(session="anything", limit=1)`; call `list_sessions(since="2026-07-05")`; call a fully-declared control such as `list_sessions(limit=1)` or `detect_current()`.
- **Expected:** Both phantom-argument calls return `{"error": "invalid_argument", "message": …}` whose message names the offending key (`limit` / `since`) and lists the accepted parameters; the message is returned **before** any session is read. The declared control call is NOT short-circuited — it reaches the tool and returns its normal result (or an empty-result `diagnostics`, never the unknown-argument error).
- **Pass criteria:** GO when each undeclared argument yields the `invalid_argument` shape naming that argument, AND a fully-declared call passes through untouched. A silent success on a phantom argument (the pre-fix behaviour) is NO-GO.

</details>

## Subagent cost (`subagent` sidecar + exact child tokens)

<details>
<summary>Show 2 scenarios (SUB-1, SUB-2)</summary>

### SUB-1 — a spawn is priced by the model it actually resolved to `[needs-real-vault]`
- **Function:** `find_tool_calls` (`subagent` sidecar, `tool_use_id`, `with_subagent_cost`), `parsers.claude._subagent_sidecar`, `session_stats.subagent_costs_by_spawn`
- **Goal:** "Which subagent burned the budget, and on which model" is answerable in ONE call — including for a BACKGROUND spawn (the majority in a real vault), whose parent-side sidecar names neither the persona nor the tokens, and including a subagent pinned to a model cheaper than its parent's.
- **Preconditions:** a real Claude vault holding at least one session that spawned subagents. The spawn tool is named `Agent` in current transcripts and `Task` in older ones — both classify as `tool_kind=task`. `[needs-real-vault]`
- **Steps:** (1) DEFAULT — call `find_tool_calls(tool_name="Agent", agent="claude", limit=200)` (fall back to `tool_name="Task"` on an older vault); note that every spawn record carries `tool_kind="task"` + `tool_use_id`, that a COMPLETED spawn's `subagent` carries `model`/`agent_type`/`status`/`tokens`, and that a background spawn's `subagent` carries `status="async_launched"` + `model` but NO `agent_type` and NO `tokens`. (2) COST — repeat with `with_subagent_cost=True`; for each spawn read `subagent.agent_type`, `subagent.model`(s) and `subagent.tokens`; group by `(agent_type, model)` and sum `subagent.tokens.total`.
- **Expected:** DEFAULT is unchanged and honest: a background spawn is a model + `async_launched` with no invented persona or cost. Under `with_subagent_cost=True` each JOINABLE spawn — background ones included — gains `subagent.child_uuid`, `agent_type` (from the child's own `agent-*.meta.json`), `models`, and EXACT `tokens` (`source="exact"`, read from the child's transcript). Where a persona is pinned to a cheaper tier, `subagent.model`/`models` report the PINNED model, not the parent's. A spawn whose child cannot be joined (not yet on disk) keeps its parent-side sidecar unchanged — `tokens` absent, never a zero.
- **Pass criteria:** GO when (a) the DEFAULT call reports background spawns with a model but no fabricated persona/tokens, AND (b) `with_subagent_cost=True` yields a per-persona × per-model token table in which background spawns are now named and priced with `tokens.source="exact"`, and at least one row shows a subagent model differing from its parent session's model. A background spawn reported as priced WITHOUT the flag, an `"estimate"` block lifted into `subagent.tokens`, or a fabricated zero for an unjoinable child, is NO-GO.

### SUB-2 — a background spawn's cost is recovered from the child's own transcript `[needs-real-vault]`
- **Function:** `read_session(include_subagents=True)` → `subagent_rollup.children`
- **Goal:** The cost of a background subagent — whose parent-side sidecar was written at launch, before any usage existed — is not lost. It is read from the child's own transcript and joined back to the persona that spawned it, with an HONEST `source` on the reported tokens (exact where the child's transcript records usage, a labeled estimate where it does not).
- **Preconditions:** a real Claude vault holding a session that spawned at least one background subagent (`status: async_launched` in SUB-1). `[needs-real-vault]`
- **Steps:** take that parent session's uuid; call `read_session(uuid, limit=1, include_subagents=True)`; read `subagent_rollup.children`.
- **Expected:** Each child entry carries `uuid`, `agent`, `component_tokens` (estimate, as before) AND a `tokens` block read from the child's OWN transcript on the same three-tier `source` ladder `session_stats(with_tokens)` uses — `"exact"` where the child records usage (the common case, including the background child whose parent-side sidecar had none), a labeled `"estimate"` where the child's transcript records no usage (a truncated / reference-only run), `source=null` without any signal — never a fabricated zero. Each child is also NAMED: `subagent_type` comes from the child's own `agent-*.meta.json` (not from the parent's sidecar, which for a background spawn carries no `agentType` at all), and `models` lists the model(s) the child ran on. `status` is present where the parent-side sidecar recorded one.
- **Pass criteria:** GO when EVERY child carries a `tokens` block with an HONEST `source` — the background child included is priced from its own transcript, and where usage exists `source="exact"` — and a `subagent_type`, so the result groups cleanly into a persona × model × tokens table. A background child reporting NO cost (the pre-fix behaviour: the parent sidecar's missing usage was the only source), a child whose missing usage is reported as a zero or falsely stamped `"exact"`, or an unnamed child in a vault whose meta files carry `agentType`, is NO-GO.

</details>
