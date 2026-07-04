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

Full spec: [docs/scenarios.md](docs/scenarios.md) — 48 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 9 | Facet filters return correct event shape (references, no body inlined); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; `tool_call` events carry an `is_error` outcome (cross-agent best-effort) without changing counts; session-level `noise=exclude\|include\|only` drops/isolates subagent sessions before any message is read, an unknown mode fails loud; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result. |
| `get_body` | 4 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated. |
| `aggregate` | 4 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash. |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 5 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal. |
| `session_stats` (preset) | 3 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |
| `list_sessions` | 2 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid` (subagent detection: Claude/OpenCode/Codex/Pi; Antigravity has no signal); `agent` filter narrows the set; `noise=exclude\|include\|only` splits the inventory into top-level vs subagent sessions and composes with `kind` by AND. |
| `find_tool_calls` | 4 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result; each record surfaces the correlated `is_error` outcome + char-capped `output` (authoritative for Claude/OpenCode, best-effort elsewhere) + `is_error_reliable`; `input_contains`/`output_contains`/`output_excludes`/`is_error` filters compose by AND (domain × error without a special verb); adaptive `output_mode` (`smart` for errors) keeps a trailing error line that `head` would drop. |
| `read_session` | 3 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices; `agent` is **optional** — an id resolves across every parser, a rare cross-agent id collision returns a `candidates` list (not an error), a miss names `agents_scanned`. |
| `search_sessions` | 4 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort; `noise=exclude` removes subagent matches before scanning, `noise=only` searches only the subagent tree. |
| empty-result diagnostics (cross-cutting) | 2 | A zero-result `query`/`search_sessions`/`find_tool_calls`/`find_file_edits`/`list_sessions` response carries `diagnostics` (per-agent scan counts + date bounds + `source_found`, corpus totals, cause hints: missing source dir / all-excluding `since`/`until` / remaining filters); a non-empty response never carries it. |
| CLI error contract | 1 | A failing `ai-r` CLI invocation exits non-zero with a structured error on stderr (single `ai-r: …` line, or one JSON `internal_error` line for unexpected failures) — never a Python traceback; `AI_R_DEBUG=1` re-raises for debugging. |

<!-- scenarios:end -->

---

## `query`

The workhorse verb: filters the unified, agent-neutral event stream
(`user_turn` / `assistant_turn` / `tool_call(<sub>)` / `plan_event`) by facets. All behaviour is
parameters. Events carry **references** (`refs`), never inlined bodies.

### QRY-1 — filter by agent + type
- **Function:** `query`
- **Goal:** A facet-filtered listing returns the correct event shape with no body inlined.
- **Preconditions:** A vault with at least one `claude` session. `[needs-real-vault]` for non-empty output; `[hermetic-ok]` for the empty-vault shape check.
- **Steps:** `mcp__ai-r__query(agent="claude", type="user_turn", limit=20)`.
- **Expected:** `{events:[…], count:N}`; every event has `type == "user_turn"`, an `id`, a timestamp, and `refs`; no event carries a full `body`/`text` payload inlined.
- **Pass criteria:** GO when all returned events match `type` and `agent`, each has an `id` usable by `get_body`, and no message body is inlined in the event.

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

## CLI error contract

### CLI-1 — structured errors, non-zero exit, never a traceback
- **Function:** `ai-r` CLI (all subcommands)
- **Goal:** A failing CLI invocation never dumps a Python traceback into a consumer script — errors are structured and the exit code is non-zero.
- **Preconditions:** `ai-r` installed on PATH (or `python -m ai_r.cli`). `[hermetic-ok]`.
- **Steps:** run and capture `rc`/stderr for: `ai-r find-tool-calls --limit -1` (invalid argument); `ai-r read no-such-session-zzz --agent claude` (not found); `ai-r list --from-date junk` (bad date). Grep each stderr for `Traceback`.
- **Expected:** Every invocation exits non-zero (invalid argument → 2, not found → 3, bad date → 1); stderr carries a single structured line — `ai-r: <message>` for expected failures, or one JSON `{"error": "internal_error", "type": …, "message": …}` line for an unexpected internal failure — and `Traceback` appears nowhere. With `AI_R_DEBUG=1` an unexpected failure re-raises (traceback allowed then, by request).
- **Pass criteria:** GO when all failing invocations are traceback-free with a non-zero exit code and a parseable one-line error. Any Python traceback without `AI_R_DEBUG=1` is NO-GO.
