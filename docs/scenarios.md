# ai-r — LLM e2e acceptance scenarios (SSOT)

> Single source of truth for **LLM-driven end-to-end acceptance scenarios** of the ai-r service.
> These are natural-language scenarios an LLM agent executes against the **live MCP tools**
> (`mcp__ai-r__*`) on a real vault, to validate the whole public surface. They **complement** the
> Python pytest suite (`tests/`): pytest proves the internals byte-for-byte and hermetically; these
> scenarios prove the *deployed* MCP surface behaves correctly and semantically end-to-end.
> English SSOT. README frames the compact table below (marker block). Update on every functionality change.

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

<!-- scenarios:start -->

## Acceptance summary

Full spec: [docs/scenarios.md](docs/scenarios.md) — 84 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 12 | Facet filters return correct event shape (references, no body inlined — `text` is a ~160-char preview, a real cut flagged `text_truncated: true`, full body via `get_body`); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; `tool_call` events carry an `is_error` outcome (cross-agent best-effort) without changing counts; every `tool_call` event carries a wrapper-aware `tool_kind` (`edit\|write\|read\|bash\|task\|skill\|mcp\|web\|other`) and — when the wrapper's input names the real actor — `tool_resolved` (subagent type under Task/Agent/spawn_agent, skill name under Skill/SlashCommand, `server:tool` under `mcp__*`; no signal → no field, never guessed), the `tool_kind` facet filters by it (unknown value fails loud) and the `tool` facet also matches resolved names; session-level `noise=exclude\|include\|only` drops/isolates subagent sessions before any message is read, an unknown mode fails loud; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result; session-level `project_dir` filter scopes events to one project (exact-or-descendant, path-boundary aware); the `session` facet accepts a single uuid OR a list of uuids — the union of those sessions' events in one call (duplicates collapse, an unknown uuid contributes nothing, an empty list or non-string item fails loud — never a silent full-corpus scan). |
| `get_body` | 5 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated; a plan-feedback `ref` (`"<session>:pf<N>"`) resolves to the FULL raw plan response (type `plan_feedback`, redacted, capped), out-of-range/unknown refs are `not_found`. |
| `aggregate` | 5 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash; the `tokens` metric folds per-row `tokens` blocks into `{input, output, reasoning, cache_read, cache_write, total, exact, estimated, unknown}` — sums stay `null` when no row carried the field (never a fabricated 0) and `exact + estimated + unknown == len(rows)` always holds. |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 11 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal; F3.4 default schema — the **final** plan's full text inlined (`body` + `body_source`, the user-edited approval text is authoritative over the signal/file body; honest `null` for steps-only plans), drafts stay references, and `feedback` carries ALL «plan quote → user comment» pairs (chronological, `plan_id`-bound, `verdict ∈ rejected\|stay_in_plan_mode`, `quote=null` for free-text comments, raw response on-demand via `ref`); technical failures filtered; agents without an approval flow contribute an honest empty `feedback`; `bodies="none"`/`feedback=false` restore the historical shape; v2 — every atom carries `version` (v1…vN per task, chronological, final = vN), every pair carries `plan_version` + `round` + `section` (the quote anchored to its source-markdown section through markup-stripping normalization — miss or multi-section ambiguity is an honest `null`, never a nearest guess; a rejected plan-file `Write` correlates like an `ExitPlanMode` verdict), and `rounds=all\|last` filters to each session's final feedback round (unknown value fails loud). |
| `session_stats` (preset) | 4 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot; `with_tokens=true` (F3.3) reads token usage at request time and adds a folded `tokens` block to every group + totals — **exact** where the agent's files record usage (Claude `message.usage` deduped per API call, Codex last cumulative `token_count`, OpenCode `message.data.tokens`, Pi `usage`), a labeled `estimate` otherwise (optional tiktoken, else a rough chars/4 heuristic — degradation, never a crash), honest `unknown` without any signal; default `false` is byte-identical to the historical output. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `incidents` (preset) | 4 | One call finds dangerous shell commands + regret reactions (F4.1) via a baked chain — ONE `query(type=tool_call, tool_kind=bash)` scan → deterministic danger dictionary on the extracted command (a Bash `description` alone never fires; `--force-with-lease` is not force-push) → bilingual (ru+en) regret-marker scan over the next `reaction_window` messages (default 6) — the two-step `confirmed` verdict, never guessed; each record carries the query event `id` (context on-demand via `relative_to`), `patterns`+`categories`, a char-capped `command` fragment centred on the hit, tri-state `is_error` (`null` where the agent's format has no correlated outcome signal) and `reaction` (marker labels + capped preview, `null` when unconfirmed); `count`/`confirmed_count`/`by_pattern` reflect the FULL match set independent of `limit`; `category`/`confirmed` filters fail loud on unknown values; emitted fields are redacted by default while matching runs on RAW text; zero incidents → `diagnostics`; documented dictionary caveat: quoting a dangerous string (echo/grep/test payloads) can still match — mention vs execution is not decidable by regex. |
| `network` (preset) | 4 | One call audits network egress (F4.3) via a baked chain — ONE `query(type=tool_call, tool_kind=web)` scan (Claude `WebFetch`/`WebSearch`, OpenCode `webfetch`, Codex `web_search` surfaced from `web_search_call` rollout records, Gemini/Antigravity `web_fetch`/`google_web_search`; Pi records no web tool — honest absence) → the request target (`url`/`query`) extracted from the call's own input (never guessed from the tool name; no target → honest `null` fields, `kind: null`) → a deterministic **risk dictionary** (`plain_http`, `credentials_in_url`, `secret_in_url`/`secret_in_query` — the redaction patterns double as the detector, `ip_literal_host`, `private_or_local_host`, `punycode_host`); each record carries the query event `id` (context on-demand via `relative_to`), derived `kind` (`fetch`\|`search`), char-capped `url`/`query`, `domain`, `risks` and tri-state `is_error`; `count`/`risky_count`/`by_domain`/`by_risk` reflect the FULL match set independent of `limit`; `kind`/`risk` filters fail loud on unknown values, `domain` matches equals-or-subdomain; risk assessment runs on RAW strings while emitted fields are redacted by default (cap applied AFTER redaction — a boundary-sliced secret never leaks); zero requests → `diagnostics`; documented caveat: MCP-mediated network access stays under `tool_kind="mcp"` — never guessed into the audit. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |
| `list_sessions` | 6 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid` (subagent detection: Claude/OpenCode/Codex/Pi; Antigravity has no signal); `agent` filter narrows the set; `noise=exclude\|include\|only` splits the inventory into top-level vs subagent sessions and composes with `kind` by AND; the Claude parser merges the CLI transcript root with the Claude Desktop metadata root — dedup by uuid, Desktop title wins (CLI title kept in `extra["cli_title"]`), origin marked `extra["source_root"]="cli"\|"desktop"`, a metadata-only session stays visible as a zero-message reference; each summary carries top-level `project_dir`+`launch_surface` (null when the format has no signal) and `project_dir` filters the inventory exact-or-descendant; each summary also carries the A3 recency signal `last_activity`+`age_sec`+`activity` (`fresh`/`stale` vs `AI_R_STALL_SEC`, default 600s) — record recency only, never a process-liveness claim. |
| `outcome` (read_session field) | 2 | `read_session` carries `outcome` — `status ∈ success\|failure\|mixed\|unknown` from two honest signals: tool-call error rate (real flag only for Claude/OpenCode — `tool_errors`/`error_rate` are `null` elsewhere, `error_rate_reliable` says which) and a calibrated bilingual (ru+en) success/failure dictionary over the last 3 *human* user turns (assistant self-reports never trusted); every deciding reason spelled out in `signals` (empty ⇔ `unknown`); no raw session text in the block; nothing guessed — no signal is `unknown`, never a fabricated verdict. |
| `resume_command` (summary field) | 1 | Every session summary carries `resume_command` — the ready-to-run CLI one-liner (`cd <project_dir> && claude --resume <uuid>` / `codex resume <uuid>` / `opencode --session <id>` / `pi --session <path>`), shell-quoted, `cd`-prefixed when `project_dir` is known; `null` exactly where no real command exists (Antigravity, subagent sessions, reference-only Desktop sessions) — text only, never executed. |
| `find_tool_calls` | 5 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result; each record surfaces the correlated `is_error` outcome + char-capped `output` (authoritative for Claude/OpenCode, best-effort elsewhere) + `is_error_reliable`; each record also carries the wrapper-aware `tool_kind` + `tool_resolved` (the real name under a Skill/Task/MCP wrapper, `null` without a signal); `input_contains`/`output_contains`/`output_excludes`/`is_error` filters compose by AND (domain × error without a special verb); adaptive `output_mode` (`smart` for errors) keeps a trailing error line that `head` would drop. |
| `read_session` | 5 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices; `agent` is **optional** — an id resolves across every parser, a rare cross-agent id collision returns a `candidates` list (not an error), a miss names `agents_scanned`; `with_tokens=true` (F3.3) attaches `tokens` (flat exact-or-estimate) + `component_tokens` — a per-component estimate over ai-r's existing event taxonomy (`user_turn`/`assistant_turn`/`thinking`/`plan`/`tool_call.<kind>`, `total`, always `source="estimate"`, plan-authoring calls under `plan` not `tool_call`, `total == sum(scalars)+sum(tool_call.values())`, empty transcript → `null`) plus per-message EXACT `tokens` blocks where the format records per-message usage (Claude deduped per API call before pagination, OpenCode, Pi; Codex/Antigravity/user turns carry no key — absent, not null); `include_subagents=true` attaches `subagent_rollup` (parent + one child per spawned subagent via `children_of(parent_uuid)` + folded `total`); CLI `ai-r read --with-tokens` prints a `COMPONENT \| TOKENS \| SOURCE` table; integers-and-labels only → outside redaction; default `false` is byte-identical to the historical output. |
| `search_sessions` | 4 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort; `noise=exclude` removes subagent matches before scanning, `noise=only` searches only the subagent tree. |
| empty-result diagnostics (cross-cutting) | 2 | A zero-result `query`/`search_sessions`/`find_tool_calls`/`find_file_edits`/`list_sessions` response carries `diagnostics` (per-agent scan counts + date bounds + `source_found`, corpus totals, cause hints: missing source dir / all-excluding `since`/`until` / remaining filters); a non-empty response never carries it. |
| secret redaction (cross-cutting) | 3 | Every text-emitting method masks secrets on output as `[REDACTED_<TYPE>]` by default and carries a `redactions` type→count dict; `redact=false` returns the raw content; matching always runs on the RAW stored text (searching a literal secret finds its session, only the display is masked); a `[REDACTED_*]` placeholder or secret-looking filter value on an empty result earns a diagnostics hint suggesting `redact=false`. |
| semantic sort (cross-cutting) | 3 | `sort="semantic"` on the text-search surface (`query` text facet, `search_sessions`) re-ranks the BM25 top-50 candidates by meaning with a local multilingual embedding model (`intfloat/multilingual-e5-small`, int8 ONNX, direct onnxruntime+tokenizers, mandatory `query:`/`passage:` prefixes applied internally, no persistent index); blended candidate score = 75 % meaning + 25 % word match (min–max normalized within the pool), no similarity cut-off — results are re-ordered, never dropped, the tail keeps BM25 order; the response carries a `semantic` report (`active: true` + model/candidates/weight, or `active: false` + plain-words `reason` + `fallback: "bm25"`); without the optional `ai-r[semantic]` deps/model files the order honestly falls back to plain BM25 — never a crash — and the default sorts never touch the module; cross-lingual ru↔en retrieval works both ways. |
| CLI error contract | 1 | A failing `ai-r` CLI invocation exits non-zero with a structured error on stderr (single `ai-r: …` line, or one JSON `internal_error` line for unexpected failures) — never a Python traceback; `AI_R_DEBUG=1` re-raises for debugging. |

<!-- scenarios:end -->

---

## `query`

The workhorse verb: filters the unified, agent-neutral event stream
(`user_turn` / `assistant_turn` / `tool_call(<sub>)` / `plan_event`) by facets. All behaviour is
parameters. Events carry **references** (`refs`), never inlined bodies: the emitted `text` is a
~160-char preview (a real cut ends with `…` and sets `text_truncated: true`); the full body is
fetched on demand via `get_body(id)`.

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

### QRY-7 — fail-loud on unimplemented facets
- **Function:** `query`
- **Goal:** The not-yet-implemented facets (`kind`/`parent`/`group`) MUST error, not silently return.
- **Preconditions:** none. `[hermetic-ok]`.
- **Steps:** `mcp__ai-r__query(kind="subagent")`; also `mcp__ai-r__query(parent="…")` and `mcp__ai-r__query(group="…")`.
- **Expected:** An error dict `{error:"invalid_argument", message:"… not yet supported …"}` (or equivalent) — **not** an events list.
- **Pass criteria:** GO only when each of the three facets returns a loud error mentioning "not yet supported". A silent (empty or unfiltered) result is NO-GO.

### QRY-8 — `tool_call` events carry an `is_error` outcome (cross-agent best-effort)
- **Function:** `query`
- **Goal:** A `tool_call` event surfaces whether the call succeeded or failed, without changing the bare `tool_call` filter/counts.
- **Preconditions:** A claude (or opencode) session containing at least one FAILED tool call — e.g. a `Bash` that exited non-zero or an errored tool. `[needs-real-vault]`.
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

---

## `get_body`

Bodies are deliberately kept off the event stream; this verb fetches them on demand by id.

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

---

## `aggregate`

Rolls up rows (from `query` / `find_file_edits` / session inventory) → `{groups, totals}`.

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

---

## `diff`

Stitches edit rows into a per-file unified diff; bodies fetched on demand.

### DIFF-1 — rows → per-file unified diff
- **Function:** `diff`
- **Goal:** Edit/write rows fold into a per-file unified diff, bodies on-demand.
- **Preconditions:** Edit rows for a session (e.g. `query(type="tool_call(edit)", session=<uuid>)`). `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__diff(rows=<edit-rows>, per_file=true, format="unified")`.
- **Expected:** `{files:[{file, edits, diff, hunks}], count, caveats}`; one entry per touched file; `diff` is a unified diff; rows without a file `ref` produce no phantom file.
- **Pass criteria:** GO when each touched file has a unified `diff` and `hunks`, `count` matches the file count, and no body is inlined beyond the diff itself.

---

## `detect_current`

### DET-1 — runtime identity
- **Function:** `detect_current`
- **Goal:** Returns a sensible runtime identity of the calling agent/session.
- **Preconditions:** Running inside an agent session (env/fs signals present). `[hermetic-ok]` (empty env → null identity is still valid).
- **Steps:** `mcp__ai-r__detect_current()` (optionally `agent="<hint>"`).
- **Expected:** `{session_id, agent, candidates:[…], verified, self}`; when env carries a session id, `session_id`/`agent` are filled and `candidates[0].source` names the winning env var; empty env → all-null/false.
- **Pass criteria:** GO when the reported identity is internally consistent (candidates cascade explains the chosen `session_id`/`agent`, `verified` reflects whether the id was confirmed). An unknown `agent` hint must error.

---

## `plan`

Normalized plan atoms of a session; agent differences hidden. Task grouping is by stable
`task_id` (plan-file slug), not title.

### PLAN-1 — grouped by slug, not title `[needs-real-vault]`
- **Function:** `plan`
- **Goal:** A task whose title drifts across drafts stays ONE task, with zero false `completed_major`.
- **Preconditions:** A real claude session that redrafts one plan-file with drifting titles (e.g. `proud-snacking-ritchie`, uuid `d61def2a-…`). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__plan(session="<uuid>", agent="claude")`.
- **Expected:** All plan atoms share one `task_id` (the slug `plans/<slug>.md`); exactly 1 `final`, the rest `draft`, `0` `completed_major`.
- **Pass criteria:** GO when `len({p.task_id}) == 1`, `count(final) == 1`, and `count(completed_major) == 0` despite the drifting titles.

### PLAN-2 — kinds: N draft + 1 final by `(ts, seq)`
- **Function:** `plan`
- **Goal:** Within one task, the last plan_event by `(ts, seq)` is `final`; earlier ones are `draft`.
- **Preconditions:** A session with a redraft chain. `[needs-real-vault]` (or `[hermetic-ok]` synthetic).
- **Steps:** `mcp__ai-r__plan(session="<uuid>")`; inspect `kind` per atom.
- **Expected:** Exactly one `final` (the latest by `(ts, seq)`), the rest `draft`.
- **Pass criteria:** GO when the single `final` is the chronologically last revision and all earlier revisions are `draft`.

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

---

## `session_stats` (preset)

Thin preset: builds per-session inventory rows → `aggregate(rank_by=stats, kind_split=true)` →
projected to the legacy totals shape.

### STAT-1 — all 4 dims give sensible counts
- **Function:** `session_stats`
- **Goal:** Each grouping dimension (agent/dir/date/kind) returns sensible non-zero counts.
- **Preconditions:** A non-empty vault. `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__session_stats(group_by="agent")`, then `"dir"`, `"date"`, `"kind"`.
- **Expected:** For each dim: a `groups` list and `totals` with `sessions`/`edits`/`agents`/`agents_list`; counts are non-zero and plausible for the vault.
- **Pass criteria:** GO when all four dimensions return well-formed, non-zero, plausible stats.

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

---

## `session_diff` (preset)

Thin preset: `diff(query(edit|write, session=<uuid>, with_intent=true))` for non-codex; codex keeps
the legacy shell-scan branch.

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

---

## `incidents` (preset)

The F4.1 preset: dangerous shell command + regret reaction, one call. A baked chain over the
existing core (never a second engine): ONE `query(type="tool_call", tool_kind="bash")` scan supplies
the candidates → the deterministic **danger dictionary** (harvested from public agent-guardrail rule
sets, calibrated on real host history 2026-07-04) selects dangerous commands → the bilingual (ru+en)
**regret dictionary** scans the following `reaction_window` messages (default 6) for an
apology/rollback reaction — the two-step check behind `confirmed`. Zero LLM: no dictionary hit → no
incident, no reaction → `confirmed: false`, never inferred.

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

---

## `find_file_edits`

Cross-agent file-edit inventory. The MCP surface is **reference-by-default**.

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

---

## `list_sessions`

Cross-agent session inventory: newest-first, paginated, each summary self-describing.

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

---

## `resume_command` (session summary field)

Every session summary (`list_sessions` / `read_session` / `search_sessions` candidates) carries
`resume_command` (F2.2): the ready-to-run shell one-liner that reopens the session in its agent's
CLI, or `null` where no real command exists. Text only — **the scenario never executes the
command**; it validates the string shape. Semantics SSOT: `src/ai_r/resume.py`;
spec: `docs/methods.md` → *Resume command*.

### RES-1 — per-agent command shape + honest nulls
- **Function:** `list_sessions` (F2.2 `resume_command` in every summary)
- **Goal:** Each agent's summary carries the correct resume command text — `cd`-prefixed when `project_dir` is known — and `null` exactly where no command exists (Antigravity always; subagent sessions; a reference-only Claude Desktop session). Nothing is executed.
- **Preconditions:** `[hermetic-ok]` — seed under `AI_R_HOME`: (a) a Claude transcript with record-level `cwd="/home/u/dev/x"`, (b) a Claude subagent (sidechain) session, (c) a Codex session with `session_meta.payload.cwd`, (d) an OpenCode DB row with `session.directory`, (e) a Pi session with a header `cwd`, (f) an Antigravity brain dir, (g) a Desktop-only Claude metadata JSON with no backing transcript.
- **Steps:** `mcp__ai-r__list_sessions()`; inspect `resume_command` on every summary. Do NOT run any of the returned commands.
- **Expected:** Claude (a) → `cd /home/u/dev/x && claude --resume <uuid>`; Codex (c) → `cd <cwd> && codex resume <uuid>`; OpenCode (d) → `cd <directory> && opencode --session <id>`; Pi (e) → `cd <cwd> && pi --session <session-file-path>` (path form, not id); Antigravity (f) → `null`; the subagent session (b) → `null`; the Desktop-only reference (g) → `null`. A session without `project_dir` gets the bare command (no fabricated `cd`).
- **Pass criteria:** GO when every non-null command matches its agent's documented shape with shell-quoted values, and `null` appears exactly on Antigravity / subagent / reference-only summaries — never an invented command. NO-GO if a command is fabricated where the CLI has no resume verb.

---

## `outcome` (read_session field)

Every `read_session` response carries `outcome` (F2.3): a session-outcome classification from two
honest signals — the tool-call error rate (real per-result flag only for Claude/OpenCode) and a
calibrated bilingual (ru+en) success/failure word dictionary over the closing *human* user turns.
`status` is `success` / `failure` / `mixed` / `unknown`; with no signal the status is an honest
`unknown` — never a guess. Every deciding reason is spelled out in `signals` (empty ⇔ `unknown`).
The block carries only ai-r-authored strings and dictionary marker labels, never raw session text.
Semantics SSOT: `src/ai_r/outcome.py`.

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

---

## `find_tool_calls`

Cross-agent tool-call search by exact name or substring pattern, with a loud XOR contract.

### FTC-1 — exact vs pattern search, cross-agent, fail-loud arg contract
- **Function:** `find_tool_calls`
- **Goal:** Locate tool invocations across every agent by exact name or substring, and reject an ambiguous argument set instead of returning a misleading empty list.
- **Preconditions:** A vault where at least one agent recorded tool calls (e.g. `Read`, an edit tool). `[needs-real-vault]`.
- **Steps:** `mcp__ai-r__find_tool_calls(tool_name="Read", limit=20)`; then `mcp__ai-r__find_tool_calls(tool_name_pattern="edit", limit=20)`; then the invalid `mcp__ai-r__find_tool_calls()` (neither name nor pattern).
- **Expected:** The exact call returns only `Read` calls (case-insensitive), spanning whichever agents recorded them; the pattern call returns calls whose tool name contains `edit` (case-insensitive); the argument-less call returns `{"error": "invalid_argument", "message": …}`.
- **Pass criteria:** GO when exact and pattern searches both return correct cross-agent matches AND the neither-argument call returns the `invalid_argument` error shape — never a silent empty result.

### FTC-2 — each record surfaces the correlated `is_error` outcome + `output`
- **Function:** `find_tool_calls`
- **Goal:** A tool-call record carries whether the call succeeded or failed and the correlated tool-result content, without changing the exact/pattern match set.
- **Preconditions:** A claude (or opencode) session with BOTH a known-succeeded and a known-failed call of the same tool — e.g. a `Bash` that exited zero and another that exited non-zero. `[needs-real-vault]`.
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

---

## `read_session`

Read one session by `uuid`+`agent`, projected to the compact `{role, content}` MCP shape, paginated.

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

---

## `search_sessions`

Case-insensitive cross-agent session search: `title`/`body`/`all` scope, `AND`/`OR`/`NOT` + negative `-term` + quoted phrases, BM25 or date sort.

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

---

## Empty-result diagnostics (cross-cutting)

A zero-result response of a scanning method (`query` / `search_sessions` / `find_tool_calls` /
`find_file_edits` / `list_sessions`) must explain itself: which agents were scanned (session
counts, date bounds, `source_found`), the corpus totals, and cause hints. Non-empty responses
never carry `diagnostics`.

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

---

## Secret redaction (cross-cutting)

Every method that emits session-derived text masks secrets on output by default (F2.1):
replacements are `[REDACTED_<TYPE>]`, the response carries a per-type `redactions` counter when
anything was masked, and `redact=false` returns the raw content. Redaction is **emission-time
only** — filters and search always match the RAW stored text. Pattern SSOT: `src/ai_r/redact.py`;
behaviour spec: `docs/methods.md` → *Redaction*.

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

---

## Semantic sort (cross-cutting, F5.1)

`sort="semantic"` on the text-search surface (`query` with a `text` facet, `search_sessions`):
BM25 supplies the top-50 candidate pool, a local multilingual embedding model
(`intfloat/multilingual-e5-small`, int8 ONNX via onnxruntime + tokenizers, `query:`/`passage:`
prefixes applied internally) re-ranks it by meaning. Blended score = 75 % meaning + 25 % word match;
no similarity cut-off (re-order, never drop); tail beyond the pool keeps BM25 order. Optional
`ai-r[semantic]` extra; without it — honest BM25 fallback with a reason, never a crash.

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

---

## CLI error contract

### CLI-1 — structured errors, non-zero exit, never a traceback
- **Function:** `ai-r` CLI (all subcommands)
- **Goal:** A failing CLI invocation never dumps a Python traceback into a consumer script — errors are structured and the exit code is non-zero.
- **Preconditions:** `ai-r` installed on PATH (or `python -m ai_r.cli`). `[hermetic-ok]`.
- **Steps:** run and capture `rc`/stderr for: `ai-r find-tool-calls --limit -1` (invalid argument); `ai-r read no-such-session-zzz --agent claude` (not found); `ai-r list --from-date junk` (bad date). Grep each stderr for `Traceback`.
- **Expected:** Every invocation exits non-zero (invalid argument → 2, not found → 3, bad date → 1); stderr carries a single structured line — `ai-r: <message>` for expected failures, or one JSON `{"error": "internal_error", "type": …, "message": …}` line for an unexpected internal failure — and `Traceback` appears nowhere. With `AI_R_DEBUG=1` an unexpected failure re-raises (traceback allowed then, by request).
- **Pass criteria:** GO when all failing invocations are traceback-free with a non-zero exit code and a parseable one-line error. Any Python traceback without `AI_R_DEBUG=1` is NO-GO.
