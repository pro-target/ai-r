# ai-r — methods dictionary (SSOT)

> Single source of truth for ai-r's public methods. README frames this file (marker block). Update on every functionality change. Russian mirror: `docs/methods.ru.md` (keep in sync).
>
> **Status:** Phase 1–3b live. Event core `query` + presets `intent`/`reaction`, `plan` + `get_body`, verbs `aggregate`/`diff`/`detect_current` (`src/ai_r/events.py`). **Phase 3b:** verbs enriched (`query(with_intent)`, `aggregate(rank_by, kind_split)`, `diff` over intent-carrying rows) → **`session_stats` and `session_diff` are now thin presets over verbs, with byte-parity proven on REAL data** (frozen snapshot ~/.claude: session_stats 8/8 group_by×top EQUAL; session_diff 12/12 sessions EQUAL). Parity tests `tests/test_phase3b_parity.py` + the full legacy suite are green. `find_file_edits`/`find_tool_calls`/`search_sessions`/`detect-*` remain separate (rationale below). Facets `kind=subagent`/`parent` in `query` are stubs (Phase 3). **F1.1:** zero-result responses carry `diagnostics`; `read_session` no longer requires `agent`; the CLI never leaks a traceback (see *Empty results & session lookup* below).
>
> **MCP surface invariant:** 13 tools = 7 legacy + 5 event-core verbs + 1 preset (`plan`).
> Source of truth is the code (`@mcp.tool()` in `mcp_server.py`); guarded by `tests/test_docs_sync.py`.

<!-- methods:start -->

## Verbs

| verb | purpose | parameters |
|---|---|---|
| `query` | filter/search session events; `with_intent=True` → a top-level `intent` on each event (the same `previous_user_intent` as legacy); a `tool_call` event carries an `is_error` outcome ref when its result is correlatable (see *Output bounds & outcome* below) | type, agent, session, since, until, file, tool, text, sort(relevance\|date), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent; kind/parent/group — stubs (Phase 3) |
| `plan` | normalized plan atoms of a session (final vs drafts, grouped by task) | session, kind(draft\|final\|completed_major), group=task, agent |
| `get_body` | on-demand body by event/plan id; returned body/text is bounded by `max_chars` (default 500k) → over-long bodies are cut with a marker and flagged `body_truncated` | id, shallow, max_chars |
| `aggregate` | rollup over rows (query/find_file_edits/session-inventory) → `{groups, totals}`; `rank_by=stats` gives the session_stats order (sessions→edits→label), `kind_split=True` adds `kind_split_available`/`note` | rows, group_by(field\|callable), metrics ⊆ count\|sessions\|edits\|intents\|agents\|messages\|files, rank_by(default\|stats), kind_split |
| `diff` | stitch edit-rows into a per-file unified diff (bodies on-demand via message_index; `intent` taken from the row when `query(with_intent)`) → `{files:[{file,edits,diff,hunks}], count, caveats}` | rows, per_file=True, format=unified |
| `detect_current` | runtime identity (env/fs, outside session-query) → `{session_id, agent, candidates[], verified, self}` | agent (hint) |

## Presets

| preset | expansion |
|---|---|
| `intent(event, n)` | `query(relative_to=event, direction=prev, n)` |
| `reaction(event, n)` | `query(relative_to=event, direction=next, n)` |
| `plan(session, kind, group=task)` | `query(type=plan_event, …)` → normalized + kind-tagged (final/draft/completed_major) |
| `session_stats(group_by)` | builds per-session inventory rows → `aggregate(rows, group_by, rank_by=stats, kind_split=True)` → projection to the legacy totals shape |
| `session_diff(uuid, agent≠codex)` | `diff(query(type=edit\|write, session=uuid, with_intent=True) with file-ref)` → projection (no file-level `hunks`) |

## Legacy tools: presets over verbs (Phase 3b)

Phase 3b enriched the verbs so old tools became thin presets **with byte-identical output, proven on REAL data** (frozen snapshot `~/.claude`, so the live vault doesn't mutate mid-run — that produced false mismatches). The legacy suites (`test_session_stats`/`test_session_diff`) are green — the second half of the compatibility proof.

**Ported to verbs (byte-parity proven):**

| tool | preset over verb | proof |
|---|---|---|
| `session_stats` | `aggregate(rank_by=stats, kind_split=True)` over per-session inventory rows | 8/8 (group_by∈agent\|dir\|date\|kind × top∈8\|0) EQUAL on the snapshot; the key is `rank_by=stats` reproducing the sessions-first rank, `kind_split` giving `kind_split_available`/`note` |
| `session_diff` (≠codex) | `diff(query(edit\|write, with_intent=True))` | 12/12 real Claude sessions EQUAL; the key is `with_intent` returning `intent`, a single chronological stream giving the same file order, the edit\|write filter excluding `Read` (else extra files) |

**Codex — exception in `session_diff`:** codex writes files via shell-exec, and the target is recovered by scanning the command line, which the event stream does NOT do → shell-redirect edits would vanish from the `query` fold. So the codex branch of `session_diff` keeps the legacy `_scan_session` (byte-parity for all agents).

**Stay separate (justified):**

| tool | why NOT a preset |
|---|---|
| `find_file_edits` / `find_tool_calls` | the record carries `session_title`/`session_date`/`assistant`/`input`, which are NOT in a `query` event; `find_tool_calls` additionally carries per-record `is_error` (correlated tool-call outcome) and `output` (correlated tool-result content, char-capped); reproducing them = re-reading the session (not a *thin* preset but a second parse over events — strictly slower) + loss of codex shell-redirect edits. `intent` is now reproducible (`with_intent`), but the other fields are not. SSOT of the rich edit/tool record |
| `search_sessions` | session-granular + BM25 session snippets; `query` is event-granular (turn/tool) → no clean 1:1 |
| `detect-agent`/`detect-session` (CLI) | the CLI prints the agent `source` and 6 output modes (list/first/strict/self/fingerprint/`--json`/`--count`) + a WARN line; the `detect_current` dict does not provide this |

## Plan atom (normalized, agent differences hidden)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Body/steps — on-demand via `get_body(id, shallow?)`. `shallow=True` → only the task's final, draft bodies dropped (scenario S6).

**Grouping by task = `task_id` (stable key):** for Claude it's the plan slug `plans/<slug>.md` (Write carries the path directly; `ExitPlanMode` without a path inherits the slug of the nearest preceding plan-Write in the session; if there is no slug yet — fallback to the normalized title). For Antigravity — the `implementation_plan.md` path. For Codex (no file) — the normalized title (a continuous `update_plan` run). Keyed by slug, NOT by title, because the title drifts within one iteration chain (decorations change the heading) — on real data that split one task into several. In a group the last plan_event by (ts, seq) = `final`, the earlier ones = `draft`; strictly earlier tasks (a DIFFERENT slug) = `completed_major`. The internal parser→signal table (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) is an implementation detail, invisible from outside.

## Output bounds & tool-call outcome

**Bounded output (untrusted sessions can be huge — the surface never returns unbounded bytes):** `find_tool_calls` caps each record's `input`/`assistant`/`intent`/`output` fields (over-long values cut with a `…[truncated]` marker and named in a per-record `truncated_fields`) and stops appending once a total-response byte budget is hit, flagging `output_truncated`; this is distinct from the count-based `truncated` (more records exist). `get_body` bounds the body via `max_chars` (`body_truncated`). Tool input larger than 1 MB is never JSON-decoded (returned verbatim) — a shared guard on the event stream and `find_tool_calls` alike. `read_session` renders a tool result as `[tool_result ok: <snippet>]` or `[tool_result ERROR: <snippet>]` (was a bare `[tool_result]`).

**Adaptive output truncation (`output_mode`):** the per-record `output` cap is `_OUTPUT_CHARS_CAP = 2000` chars. How that budget is spent is controlled by `output_mode ∈ {"head", "tail", "smart"}`. The default (`output_mode=None`) is **adaptive per record**: a record with `is_error == True` is truncated `"smart"` (surface the error lines — `error`/`fatal`/`traceback`/… — plus the tail, so an error at the *end* of a long log is not lost to a head-only cut), while a successful record is truncated `"head"` (legacy behaviour). An explicit `output_mode` forces one strategy for every record. `smart`/`tail` may return up to ~2× the cap to keep both the surfaced lines and the tail; whenever `output` is cut it is still named in that record's `truncated_fields`.

**Filtering `find_tool_calls` (all optional, composed by AND):** beyond `tool_name`/`tool_name_pattern`, records can be narrowed by `input_contains` (case-insensitive substring over the serialized tool input / command text), `output_contains` (ci substring over the correlated `output`), `output_excludes` (drop a record whose `output` contains the marker — a caller-supplied noise filter, e.g. a harness security-gate line, `"user rejected"`, `"MANUAL COMMIT BLOCKED"`; **no such list is hard-coded in the core**), and `is_error` (tri-state: `None` = all, `True` = errors only, `False` = successes only). All filters intersect (AND). There is **no** dedicated "error + domain" verb: that pairing is a *composition* — e.g. `find_tool_calls(input_contains="git", is_error=True)` returns the real command failures of a chosen domain (`git` is just an example domain, not a special case).

**`is_error` (tool-call outcome) is cross-agent best-effort:** **Claude** and **OpenCode** carry a real success/error flag (Claude's `tool_result.is_error`; OpenCode's `state.status == "error"`). **Codex** and **Pi** expose no error field on their result records → `is_error` is always `False` (absence of a flag, not a proof of success). **Antigravity** emits no tool-result records at all → no outcome signal. Consumers must not read a cross-agent `is_error=False` as "verified success" for Codex/Pi/Antigravity. `find_tool_calls` now carries the same `is_error` per record, plus the correlated `output` (tool-result content, char-capped) — correlation is by tool_use_id (Claude `tool_use.id` / OpenCode `callID`); with the same best-effort caveat (`is_error` is authoritative only for Claude/OpenCode, and defaults to `False` for Codex/Pi/Antigravity or when no result correlates). To make that honesty machine-readable, each `find_tool_calls` record also carries `is_error_reliable` (bool): `True` for Claude/OpenCode (a real flag backs the value), `False` for Codex/Pi/Antigravity (no source → `is_error` is always `False` and may **undercount** true failures). A consumer filtering `is_error=True` should read `is_error_reliable` to know whether a `False` means "verified success" or merely "no signal".

## Empty results & session lookup

**Empty-result diagnostics (a zero-result response explains itself, never a bare empty list):** when a scanning method — `query`, `search_sessions`, `find_tool_calls`, `find_file_edits`, `list_sessions` — matches nothing, the response carries a `diagnostics` object next to the empty list/count. Shape: `scanned` (one entry per scanned agent — `sessions` count, `date_min`/`date_max`, `source_found`, plus a per-agent `hint` such as `source not found: ~/.pi/agent/sessions` or `source present but contains no sessions`), `corpus` (total sessions + overall date bounds), `filters` (echo of the active filters), `hints` (cause candidates: a `since`/`until` bound that excludes the entire corpus is called out explicitly — e.g. `since='2030-01-01' is after the newest session (…) — the date filter excludes the entire corpus`; otherwise the remaining filters are named, or the result is declared a genuine no-match). Diagnostics are computed only on the empty path — a non-empty response never carries (or pays for) them — and never crash the response (an unreadable source degrades to a per-agent hint).

**`read_session` no longer requires `agent`:** the parameter is optional. When omitted, the id is looked up across every parser (session ids are unique across agents in practice). A rare cross-agent id collision returns `{ambiguous: true, candidates: [...], count}` — a disambiguation list where each candidate carries its `agent`, NOT an error; re-ask with an explicit `agent`. A miss returns `{error: "not_found", agents_scanned: [...]}`. `get_body` was already agent-free (its event id embeds the owning session).

**CLI error contract (a consumer script never sees a Python traceback):** expected failures keep the single `ai-r: <message>` stderr line + non-zero exit (1 generic / 2 ambiguous or invalid / 3 not found); an *unexpected* internal error is emitted as one structured JSON line on stderr (`{"error": "internal_error", "type", "message", "hint"}`) with exit code 1. `AI_R_DEBUG=1` re-raises the original exception for debugging.

<!-- methods:end -->
