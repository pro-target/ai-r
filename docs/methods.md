# ai-r — methods dictionary (SSOT)

> Single source of truth for ai-r's public methods. README frames this file (marker block). Update on every functionality change. Russian mirror: `docs/methods.ru.md` (keep in sync).
>
> **Status:** Phase 1–3b live. Event core `query` + presets `intent`/`reaction`, `plan` + `get_body`, verbs `aggregate`/`diff`/`detect_current` (`src/ai_r/events.py`). **Phase 3b:** verbs enriched (`query(with_intent)`, `aggregate(rank_by, kind_split)`, `diff` over intent-carrying rows) → **`session_stats` and `session_diff` are now thin presets over verbs, with byte-parity proven on REAL data** (frozen snapshot ~/.claude: session_stats 8/8 group_by×top EQUAL; session_diff 12/12 sessions EQUAL). Parity tests `tests/test_phase3b_parity.py` + the full legacy suite are green. `find_file_edits`/`find_tool_calls`/`search_sessions`/`detect-*` remain separate (rationale below). Facets `kind=subagent`/`parent` in `query` are stubs (Phase 3).

<!-- methods:start -->

## Verbs

| verb | purpose | parameters |
|---|---|---|
| `query` | filter/search session events; `with_intent=True` → a top-level `intent` on each event (the same `previous_user_intent` as legacy) | type, agent, session, since, until, file, tool, text, sort(relevance\|date), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent; kind/parent/group — stubs (Phase 3) |
| `plan` | normalized plan atoms of a session (final vs drafts, grouped by task) | session, kind(draft\|final\|completed_major), group=task, agent |
| `get_body` | on-demand body by event/plan id | id, shallow |
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
| `find_file_edits` / `find_tool_calls` | the record carries `session_title`/`session_date`/`assistant`/`input`, which are NOT in a `query` event; reproducing them = re-reading the session (not a *thin* preset but a second parse over events — strictly slower) + loss of codex shell-redirect edits. `intent` is now reproducible (`with_intent`), but the other fields are not. SSOT of the rich edit/tool record |
| `search_sessions` | session-granular + BM25 session snippets; `query` is event-granular (turn/tool) → no clean 1:1 |
| `detect-agent`/`detect-session` (CLI) | the CLI prints the agent `source` and 6 output modes (list/first/strict/self/fingerprint/`--json`/`--count`) + a WARN line; the `detect_current` dict does not provide this |

## Plan atom (normalized, agent differences hidden)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Body/steps — on-demand via `get_body(id, shallow?)`. `shallow=True` → only the task's final, draft bodies dropped (scenario S6).

**Grouping by task = `task_id` (stable key):** for Claude it's the plan slug `plans/<slug>.md` (Write carries the path directly; `ExitPlanMode` without a path inherits the slug of the nearest preceding plan-Write in the session; if there is no slug yet — fallback to the normalized title). For Antigravity — the `implementation_plan.md` path. For Codex (no file) — the normalized title (a continuous `update_plan` run). Keyed by slug, NOT by title, because the title drifts within one iteration chain (decorations change the heading) — on real data that split one task into several. In a group the last plan_event by (ts, seq) = `final`, the earlier ones = `draft`; strictly earlier tasks (a DIFFERENT slug) = `completed_major`. The internal parser→signal table (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) is an implementation detail, invisible from outside.

<!-- methods:end -->
