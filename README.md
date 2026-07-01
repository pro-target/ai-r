# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> `git` shows **what** made it into the code. `ai-r` shows **why**: which agent
> did it, under which plan — and whether it quietly dropped the plan it actually
> settled on. Read-only, across all five coding agents, one interface.

An agent reports: "done X, per plan Y." You have no way to check. The plan lives
in one format, the edits in another. And if two agents worked the task, their
histories don't reconcile at all — each writes its own way, in its own place.
`ai-r` reads an agent's session history and pulls out the intent, the plan, and
the authorship behind an edit.

## Quick example — an agent asks about history

The primary mode is **MCP**: an agent (Claude, Codex, …) calls `ai-r` directly
and asks about history in plain language. For example — pull the plan the
previous agent settled on, drafts discarded:

```
Show me the plan from the last session — final only, no intermediate revisions.
→ ai-r: plan(session=…, kind="final")  →  get_body(id, shallow=true)
        returns the final task + a list of dropped_drafts
```

Fast edit attribution — one terminal command, across every agent at once:

```bash
# who edited this file, and when — cross-agent, optionally time-boxed
ai-r find-file-edits auth.py --since 2026-06-01
```

## What hurts

- "Done, I did X per plan Y" — with nothing to check it against: the agent keeps
  the plan in one shape, the edits in another.
- You switched agents mid-task and lost the thread. There's nowhere to ask "what
  did the *other* agent already try?"
- An edit shows up in a file — and it's unclear **which** agent made it, and on
  what request.

One cause: every agent writes its history **its own way** — Claude and Codex in
JSONL, OpenCode in SQLite, Antigravity in "brain" directories, Pi in
per-project JSONL. Five formats, five layouts — together they don't reconcile.

## The promise

`ai-r` folds all five into **one read-only interface**. Point any agent — or a
script, or yourself — at any session, no matter which tool recorded it. One
query shape per agent; format differences are normalized inside the parsers.

## Key features

- **"Why?", not just "What?".** Extracts the plan, intent, and authorship behind
  an edit — not just the diff text. `git diff` tells you *what* changed; `ai-r`
  tells you under which plan and on whose request.
- **The final plan, not the drafts.** `ai-r` pulls the plan the agent *settled
  on*, and separately shows what it threw away along the way (`dropped_drafts`)
  — across Claude / Codex / Antigravity, where the plan signals differ.
- **Cross-agent attribution.** Any file edit or tool call → the agent that made
  it, plus the request that triggered it (`find-file-edits` / `find-tool-calls`).
- **Small answer, body on demand.** Records carry a reference to the content
  (hash + length); the full edit text is fetched separately — the response
  doesn't balloon.
- **Works over MCP (13 tools).** An agent calls `ai-r` directly in plain
  language; the same data is available from the terminal (CLI) and from code
  (Python SDK).
- **A reader, not a guard.** Extracts entities; you (or your tool) build the
  knowledge graph and the memory. Read-only: it never runs or writes to an
  agent's history.

## What you use it for

- **Audit sessions with a fresh pair of eyes.** A new agent with an empty
  context coldly checks past sessions on three axes: were promises and
  requirements met; are the decisions sound and well-judged; how deeply was the
  question explored — what the agent missed. On one real run, 271 dialogs were
  reviewed this way in a week, catching agents that finished the task **but
  misled on the planning** — something a live chat hides, and that steers you
  into wrong decisions.
- **Continue past a spent context — without losing detail.** `/compact` erases
  the specifics. Instead, open a fresh session: it reads the previous session's
  **logs** and continues from its conclusions, without re-burning context on
  what's already been worked out. The original session stays intact — for audit
  and search. The new session can run in **any** agent: the history reconciles
  regardless of the tool.
- **Feeds your memory system.** Keeping memory and summaries à la Karpathy, or
  your own method? `ai-r` gives you, for AI chats, what you already do with
  message history — parsed entities to build a lasting memory of the details
  that matter.
- **Recall what you did and why.** Why was this file edited? Why was this rule
  added? Find the session where the file changed and read the request *before*
  the edit.

## How it differs from session-search tools

A handful of cross-agent tools now read more than one agent's history
(`jazzyalex/agent-sessions`, `Dicklesworthstone/coding_agent_session_search`,
`hacktivist123/agent-session-resume`). Almost all are about **search and
timeline**: find a *session*, scroll the history.

`ai-r` goes deeper: it extracts the **plan, intent, and authorship as ready-made
entities** you build memory on. Search finds text — `ai-r` answers **why**.
Technically a search tool could also dig a plan out of a session's text, but it
doesn't hand it back parsed into a single, normalized shape — with `ai-r` that's
the primary surface.

| Capability | Single-agent viewers | Cross-agent search tools | `ai-r` |
|---|---|---|---|
| Reads >1 agent's logs | No | Yes | Yes — Claude, Codex, OpenCode, Antigravity, Pi |
| Programmatic surface | Mostly GUI/TUI | Mostly TUI/CLI/app | **MCP + CLI + Python SDK** |
| Attribution (edit/command → agent + intent) | — | Partial | Yes — `find-file-edits` / `find-tool-calls` |
| Audit replay (reconstruct a session's changes, no git) | — | Rarely | Yes — `session_diff` |
| Plan extraction (final vs draft, normalized) | — | — | Yes — `plan` |
| Scope | Viewer | Search / resume / memory | **Read-only extraction core** |

*Competitor columns reflect their public docs as of 2026-07; where a capability
is unclear we under-state rather than over-claim.*

We deliberately **don't** compete on agent breadth, speed, or TUI richness.
`ai-r`'s wedge is extracting the "why" and structured entities for machine
consumption.

## Proven in practice

`ai-r` already reads its own development history — across all five agents. Real
tools run on it (they live separately, on top of its read-only API):

- **auditor** — a fresh agent coldly checks what the previous one actually did
  and decided. This caught agents that quietly fibbed about the plan.
- **summarizer** (`export rounds`) — renders a session into a ready handoff doc.
- **ai-local-reader** — a read-only skill: audits past sessions from disk across
  all agents.

These tools are workflow-side, outside this repo. `ai-r` itself only reads and
returns data.

## Supported agents

| Agent | Storage | Parser |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite (snap/flatpak auto-detect) |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain directories |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

Not your agent? Adding a sixth is **one parser module**; the read-only pattern
ports to any tool in minutes. See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Surfaces

`ai-r` gives the same reading power three ways:

- **MCP server** (`ai-r-mcp`) — 13 tools over stdio JSON-RPC, so any MCP agent
  calls it directly (recommended). Registration — see
  [docs/mcp-registration.md](./docs/mcp-registration.md).
- **CLI** (`ai-r`) — subcommands for scripts and manual use (`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`). Search operators —
  [docs/search-operators.md](./docs/search-operators.md).
- **Python SDK** (`from ai_r.parsers import ...`) — parsers, typed
  `Session`/message models, and the event verbs, to build your own tools.

### Method vocabulary (SSOT)

The block below is framed from [`docs/methods.md`](./docs/methods.md) — the
English source of truth for the public verbs and presets. It's kept in sync with
that file's marker block.

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
| `find_file_edits` / `find_tool_calls` | the record carries `session_title`/`session_date`/`assistant`/`input`, which are NOT in a `query` event; reproducing them = re-reading the session (not a *thin* preset but a second parse over events — strictly slower) + loss of codex shell-redirect edits. `intent` is now reproducible (`with_intent`), but the other fields are not. SSOT of the rich edit/tool record |
| `search_sessions` | session-granular + BM25 session snippets; `query` is event-granular (turn/tool) → no clean 1:1 |
| `detect-agent`/`detect-session` (CLI) | the CLI prints the agent `source` and 6 output modes (list/first/strict/self/fingerprint/`--json`/`--count`) + a WARN line; the `detect_current` dict does not provide this |

## Plan atom (normalized, agent differences hidden)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Body/steps — on-demand via `get_body(id, shallow?)`. `shallow=True` → only the task's final, draft bodies dropped (scenario S6).

**Grouping by task = `task_id` (stable key):** for Claude it's the plan slug `plans/<slug>.md` (Write carries the path directly; `ExitPlanMode` without a path inherits the slug of the nearest preceding plan-Write in the session; if there is no slug yet — fallback to the normalized title). For Antigravity — the `implementation_plan.md` path. For Codex (no file) — the normalized title (a continuous `update_plan` run). Keyed by slug, NOT by title, because the title drifts within one iteration chain (decorations change the heading) — on real data that split one task into several. In a group the last plan_event by (ts, seq) = `final`, the earlier ones = `draft`; strictly earlier tasks (a DIFFERENT slug) = `completed_major`. The internal parser→signal table (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) is an implementation detail, invisible from outside.

## Output bounds & tool-call outcome

**Bounded output (untrusted sessions can be huge — the surface never returns unbounded bytes):** `find_tool_calls` caps each record's `input`/`assistant`/`intent` fields (over-long values cut with a `…[truncated]` marker and named in a per-record `truncated_fields`) and stops appending once a total-response byte budget is hit, flagging `output_truncated`; this is distinct from the count-based `truncated` (more records exist). `get_body` bounds the body via `max_chars` (`body_truncated`). Tool input larger than 1 MB is never JSON-decoded (returned verbatim) — a shared guard on the event stream and `find_tool_calls` alike. `read_session` renders a tool result as `[tool_result ok: <snippet>]` or `[tool_result ERROR: <snippet>]` (was a bare `[tool_result]`).

**`is_error` (tool-call outcome) is cross-agent best-effort:** **Claude** and **OpenCode** carry a real success/error flag (Claude's `tool_result.is_error`; OpenCode's `state.status == "error"`). **Codex** and **Pi** expose no error field on their result records → `is_error` is always `False` (absence of a flag, not a proof of success). **Antigravity** emits no tool-result records at all → no outcome signal. Consumers must not read a cross-agent `is_error=False` as "verified success" for Codex/Pi/Antigravity.

<!-- methods:end -->

### Event core

The verbs above are new: one **event core** replaces a pile of one-off tools.
Each parser reads one agent's logs and emits typed models, normalized into a
single agent-neutral stream — `user_turn` / `assistant_turn` / `tool_call(...)`
/ `plan_event`. A small set of verbs filters, aggregates, and diffs that stream;
agent differences (`ExitPlanMode` vs `update_plan` vs `implementation_plan.md`)
stay hidden inside the parsers — the caller sees one shape.

An honest boundary: this is **extraction of entities only** — turns, tool calls,
plans, intents, reactions. It is **not** a graph and **not** a memory store.
What you do next (knowledge graph, Obsidian, persistent memory) is on your side,
outside this repo. For the full layering and the MCP tool list, see
[docs/architecture.md](./docs/architecture.md).

## Quick start (1 command)

Requirements: Python 3.11+ with `venv` or `pip`, and `jq` (used to auto-patch
the Claude and Antigravity MCP configs — the others don't need `jq`).

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

The installer creates a venv, installs the runtime package, patches MCP configs
for **Claude**, **Codex**, **OpenCode**, **Antigravity** (where the configs
exist), installs the **Pi** CLI skill, and runs smoke tests.

## Boundaries: a reader, not a guard

- **Read-only.** It never runs an agent's code and never writes to its history —
  it reads and returns.
- **No graph, no memory.** It extracts entities (turns, calls, plans, intents).
  Building a knowledge graph or memory out of them is your job, not its.
- **Not an access-control layer.** Anyone who can reach the CLI, MCP server, or
  package can read any session. There's no authorization in front of the
  parsers; keep it where untrusted local processes can't reach.
- **Session content is data, not commands.** Whoever reads (auditor, summarizer)
  must treat session text as data, not instructions. See
  [Security](docs/security.md).

<!-- scenarios:start -->

## Acceptance summary

Full spec: [docs/scenarios.md](docs/scenarios.md) — 38 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 8 | Facet filters return correct event shape (references, no body inlined); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; `tool_call` events carry an `is_error` outcome (cross-agent best-effort) without changing counts; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result. |
| `get_body` | 4 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated. |
| `aggregate` | 4 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash. |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 5 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal. |
| `session_stats` (preset) | 3 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |
| `list_sessions` | 1 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid`; `agent` filter narrows the set. |
| `find_tool_calls` | 1 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result. |
| `read_session` | 2 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices. |
| `search_sessions` | 3 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort. |

<!-- scenarios:end -->

## Next — documentation

- Method vocabulary (verbs + presets) — [`docs/methods.md`](./docs/methods.md)
  (English SSOT) · [`docs/methods.ru.md`](./docs/methods.ru.md) (Russian mirror)
- Acceptance scenarios (32 e2e) — [`docs/scenarios.md`](./docs/scenarios.md)
- Architecture & layering — [`docs/architecture.md`](./docs/architecture.md)
- Search operators — [`docs/search-operators.md`](./docs/search-operators.md)
- Per-agent MCP registration — [`docs/mcp-registration.md`](./docs/mcp-registration.md)
- Parser coverage & limitations — [`docs/parsers.md`](./docs/parsers.md)
- Security (untrusted content) — [`docs/security.md`](./docs/security.md)
- Add a sixth agent — [`CONTRIBUTING.md`](./CONTRIBUTING.md)

## Development

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ tests, CI requires ≥80% coverage
- Conventional Commits (`feat:`, `fix:`, `docs:`, …)
- On adding new agents, see [CONTRIBUTING.md](./CONTRIBUTING.md) and
  [docs/parsers.md](./docs/parsers.md)

<details>
<summary>Keywords</summary>

claude code session reader · claude code session parser · codex session parser ·
opencode session reader · antigravity brain parser · pi agent session reader ·
cross-agent attribution · ai coding agent audit · ai agent session history ·
mcp session tools · read-only session reader · agent session replay ·
resume agent session · agent handoff · plan extraction · tool-call audit ·
file edit attribution · multi-agent coding · claude codex opencode antigravity pi

</details>

## License

MIT — see [LICENSE](./LICENSE).

---

**Get started:** clone + `bash install.sh`, then register the MCP server for your
agent ([docs/mcp-registration.md](./docs/mcp-registration.md)) and restart the
host tool. One read-only surface to every agent's history.
