# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **One read-only surface for every AI coding agent's session history** —
> Claude, Codex, OpenCode, Antigravity, and Pi — over **MCP**, a **CLI**,
> or a **Python SDK**.
>
> Switch agents without losing the thread · Attribute any edit or command
> to the agent that ran it · Replay a session · Extract the plan behind
> the work — across all five agents, one interface.

```bash
# one query, every agent — find the session where that auth bug came up
ai-r search "auth token refresh" --scope body
```

## The pain: five silos, no shared view

Every AI coding agent keeps its own conversation history — in its own
place, in its own format:

- **Claude** and **Codex** write JSONL,
- **OpenCode** uses a SQLite DB,
- **Antigravity** scatters "brain" directories,
- **Pi** writes per-project JSONL.

Five formats, five layouts. So the moment you run more than one agent,
your work goes **siloed per tool**. Switch agents and you lose the
thread. You can't ask "what did the *other* agent already try?" And when
a commit or a file edit shows up, there's no straight answer to **which
agent actually did it** — the attribution lives in five incompatible
logs you'd have to learn one by one.

## The promise

`ai-r` collapses all five into **one read-only interface**. Point any
agent — or a script, or yourself — at any session, no matter which tool
wrote it. Same query shape for every agent; the per-format differences
are normalized away inside the parsers.

## How it works

```
┌──────────────────────────────────────────────────────────────┐
│ Public API (3 surfaces)                                       │
│   • ai-r        CLI (argparse)                                │
│   • ai-r-mcp    MCP server (stdio JSON-RPC)                   │
│   • from ai_r.parsers import ...   (Python SDK)               │
└──────────────────────────────────────────────────────────────┘
                          ▲
┌──────────────────────────────────────────────────────────────┐
│ Event core: one agent-neutral stream                          │
│   user_turn · assistant_turn · tool_call(edit|write|read|…)   │
│   · plan_event   → filtered/aggregated/diffed by verbs        │
└──────────────────────────────────────────────────────────────┘
                          ▲
┌──────────────────────────────────────────────────────────────┐
│ Per-agent parsers (read-only)                                 │
│   claude · codex · opencode(SQLite) · antigravity · pi        │
└──────────────────────────────────────────────────────────────┘
```

Each parser reads one agent's on-disk logs and emits typed `Session`
and message models. Those normalize into a single, agent-neutral **event
stream** — `user_turn` / `assistant_turn` / `tool_call(...)` /
`plan_event` — and a small set of **verbs** filter, aggregate, and diff
that stream. The differences between agents (`ExitPlanMode` vs
`update_plan` vs `implementation_plan.md`) are hidden inside the
parsers; callers see one shape.

## Proof — it reads the sessions that built it

`ai-r` reads the very sessions that built `ai-r`. Across **5 agents** it
is called routinely by real consumers that live on top of the parser API:

- **session-summarizer** / `export rounds` — render a session into a
  CHANGELOG-style handoff doc.
- **git-log-auditor** — a fresh agent whose only job is to coldly review
  what a previous agent actually did and decided. This has caught agents
  that quietly misled the planning.
- **ai-local-reader** — a read-only skill that audits past sessions from
  local disk across all five agents.
- **MCP registrations** — the server is auto-registered into Claude,
  Codex, OpenCode, and Antigravity; Pi gets a CLI skill.

These consumers are **workflow-side** and live outside this repo; they
call `ai-r`'s read-only parser API (`read_messages`, the MCP tools, the
verbs). `ai-r` itself stays a reader.

## Quick start (1 request)

Prerequisites: Python 3.11+ with either `venv` (`python3-venv`) or `pip`
(`python3-pip`/`pip3`), and `jq` (used to auto-register the Claude and
Antigravity MCP configs — the others need no `jq`).

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

That's it. The installer:
- Uses per-user mode by default; `opt` mode is explicit
- Creates a venv, installs the runtime package
- Patches MCP configs for **Claude**, **Codex**, **OpenCode**, **Antigravity** when those config files exist
- Installs the **Pi** CLI skill at `~/.agents/skills/ai-r/SKILL.md` when absent
- Runs smoke tests

## Supported agents

| Agent | Storage | Parser |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite (auto-detects snap/flatpak) |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain directories |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

Not your agent? Adding a sixth is **one parser module** — the read-only
pattern ports to any tool (Cursor, Cline, your own) in minutes. See
[CONTRIBUTING.md](./CONTRIBUTING.md).

## Surfaces

`ai-r` exposes the same reading power three ways:

- **MCP server** (`ai-r-mcp`) — 13 tools over stdio JSON-RPC, so any
  MCP-capable agent can call it directly (recommended).
- **CLI** (`ai-r`) — subcommands for scripts and manual use.
- **Python SDK** (`from ai_r.parsers import ...`) — the parsers, typed
  `Session`/message models, and event verbs, for building your own tools.

### Method vocabulary (SSOT)

The block below is framed from [`docs/methods.md`](./docs/methods.md) —
the single source of truth for the public verbs and presets. It is kept
in sync with that file's marker block.

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

### What this branch adds — event core

The verbs above are new: one **event-stream core** replaces a pile of
one-off tools. Highlights:

- **`query`** — the workhorse. Filter the unified event stream by
  `type` / `agent` / `session` / date / `file` / `tool` / `text`. With
  `sort="relevance"` the text match is BM25-ranked (same scorer as
  `search_sessions`). With `relative_to`+`direction`+`n` it walks
  neighbouring turns — the primitive behind both `intent` and
  `reaction`.
- **`intent` / `reaction` presets** — `intent(event)` = the user request
  *behind* an event (walk back); `reaction(event)` = the user's response
  *after* an assistant turn (walk forward — critique, correction,
  approval).
- **`plan`** — normalized plan atoms per session, grouped by task, tagged
  `final` vs `draft` vs `completed_major`. So you can extract *the plan
  the agent settled on* versus the discarded revisions — across Claude,
  Codex, and Antigravity, whose plan signals differ. `get_body(..., 
  shallow=True)` hands a subagent just the final plan, drafts elided.
- **`aggregate` / `diff` / `detect_current`** — generic rollup, per-file
  stitched diff, and runtime self-identity. `session_stats` and
  `session_diff` are now thin presets over these, with byte-identical
  output proven on real data (see the SSOT block above).

Honest scope: this is **read-only entity extraction** — turns, tool
calls, plans, intents, reactions. It is **not** a graph or a memory
store. What a consumer does next (split into a knowledge graph, Obsidian,
persistent memory) is deliberately **out of scope** and lives
consumer-side.

### MCP tools

The MCP server exposes 13 tools. The reading essentials:

| Tool | Purpose |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | List discoverable sessions, optionally filtered by agent. Paginated. |
| `read_session(uuid, agent, offset?, limit?)` | Read one session; up to 100 messages by default, `offset`/`limit` to page. |
| `find_file_edits(path, agent?, since?, until?, limit?)` | Every file edit for a path, cross-agent by default, optionally time-boxed. |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | Every tool call — shell, file writes, web fetches, MCP calls — each carrying the triggering user request as `intent`. |
| `search_sessions(query, agent?, scope?, operator?, limit?, sort?)` | Search title and/or body with `AND`/`OR`/`NOT` and Google-style `-term`; `sort=relevance` (BM25) or `date`. |
| `session_stats(agent?, since?, until?, group_by?, top?)` | Group + rank sessions by `agent`/`dir`/`date`/`kind`. |
| `session_diff(session_uuid, agent, path?)` | Reconstruct what a session changed, per-file, without git. |
| `query`, `plan`, `get_body`, `aggregate`, `diff`, `detect_current` | The event-core verbs described above. |

**Pagination** (`limit`/`offset`, plus a `truncated` flag when more pages
remain) is exposed on the MCP tools and the Python SDK — see
[architecture.md](docs/architecture.md).

### CLI

```bash
# list / read / search
ai-r list --agent pi
ai-r read --agent pi <session-uuid>
ai-r search "refactor"
ai-r search "pwa manifest" --scope body --operator and --agent claude

# who edited a file, across all agents, optionally time-boxed
ai-r find-file-edits src/auth.py --since 2026-06-01 --until 2026-06-30
ai-r find-file-edits "config" --agent claude --limit 20

# what did agents run? exact tool name or substring pattern, time-boxed
ai-r find-tool-calls Bash --since 2026-06-01
ai-r find-tool-calls --pattern deploy --agent codex

# which files change most? rank by edits / sessions / distinct requests / agents
ai-r file-frequency --top 10
ai-r file-frequency --path src/ --agent claude --since 2026-06-01

# which agent / session am I in (scripts, orchestration, self-resume)
ai-r detect-agent --quiet          # → e.g. "claude"
ai-r detect-session --json         # → candidate session UUIDs

# render a session as a CHANGELOG round (handoff doc / replay)
ai-r export rounds <session-uuid> --include-round --output round.md
```

Add `--json` to most subcommands for machine-readable output. The
event-core verbs (`query`/`plan`/`aggregate`/`diff`/`detect_current`) are
available over MCP and the Python SDK; the CLI covers the subcommands
listed above.

#### Search operators

`search_sessions` (MCP) and `ai-r search` (CLI) share the same query
parser and operator parameter. Default behaviour (`scope="title"`,
`operator="AND"`, `limit=50`) is the historical title-only substring
search.

**Query syntax**

| Form | Example | Meaning |
|---|---|---|
| Bare words | `pwa manifest` | Both terms (operator controls how). |
| Quoted phrase | `"exact phrase"` | Single literal term. |
| Negative prefix | `-claude` | Google-style: this term must NOT appear. |

Words `AND`, `OR`, and `NOT` inside the query are literal search terms.
Boolean behaviour is selected with `--operator and|or|not` (CLI) or
`operator="AND"|"OR"|"NOT"` (MCP).

**Operator modes** (controls how positive terms combine)

| Mode | `pwa manifest` semantics | `pwa -claude` semantics |
|---|---|---|
| `AND` (default) | both must appear | `pwa` appears, `claude` does not |
| `OR` | at least one appears | one of `pwa` appears, `claude` does not |
| `NOT` | neither appears | neither `pwa` nor `claude` appears |

**Scope modes**

| Scope | Where the search runs |
|---|---|
| `title` (default) | `session.title` only — matches the historical title-only behaviour. |
| `body` | message text + `tool_use[*].input` + `tool_result[*].content` for every session. |
| `all` | title OR body. |

When `scope` is `body` or `all` and a match is found, the result includes
a `snippet` field (CLI: printed in the table) — the first matching
excerpt, up to 200 characters. Results are BM25-ranked by default
(`sort=relevance`); pass `sort=date` to order by recency.

**Performance note**: `body` and `all` invoke `read_messages` on every
candidate session. On large vaults the first run can be slow; raise
`--limit` to keep the result set bounded while iterating.

**MCP example**

```python
search_sessions(
    query='pwa -claude',
    agent='claude',
    scope='body',
    operator='AND',
    limit=20,
)
```

**CLI examples**

```bash
# title-only (legacy, still default)
ai-r search "refactor"

# body search, all terms must appear, exclude claude
ai-r search "pwa manifest -claude" --scope body --operator and

# body search, any term, max 5 results
ai-r search "pwa manifest" --scope body --operator or --limit 5

# everything containing neither of these terms
ai-r search "auth login" --scope body --operator not
```

### Python SDK

```python
from ai_r.parsers import AgentName, claude

for session in claude.list_sessions():
    print(session.uuid, session.title)

session = claude.read_session("<session-uuid>")
print(session.message_count)

messages = claude.read_messages("<session-uuid>")
print(messages[0].role, messages[0].text)
```

See [docs/architecture.md](./docs/architecture.md) for the full layering.

## Use cases — one job per real consumer

One reader across every agent unlocks jobs a single-agent log can't do:

- **Cross-agent attribution — "which agent did this?"** Every edit to a
  path, every tool call, across every agent and session, tagged with the
  triggering request. Time-box it: "what did agents do to `src/auth.py`
  last week?" — `find-file-edits` / `find-tool-calls`. Powers the
  **git-log-auditor**.
- **Audit & replay — coldly review what an agent actually did.** A fresh
  agent reads a prior session and reports what it *ran*, not just what it
  claimed. `session_diff` reconstructs the per-file change without git;
  `export rounds` renders a CHANGELOG-style handoff. Powers
  **session-summarizer** and **ai-local-reader**.
- **Resume & handoff — switch agents mid-task, keep the thread.** Hit a
  provider limit or run out of context window? Start a fresh session
  (any agent), hand it the previous session's UUID, and continue. The
  prior transcript is readable regardless of which tool wrote it —
  `read_session`, `detect-session`.
- **Find file edits + intents — why did this file keep changing?**
  `file-frequency` rolls up which files churn most, ranked by edits,
  distinct sessions, distinct requests, and agents involved; each edit
  carries the user request behind it as `intent`.
- **Plan extraction — recover the plan the agent settled on.** `plan`
  returns normalized plan atoms per task, `final` versus `draft`, across
  Claude / Codex / Antigravity. Hand a subagent just the final plan with
  `get_body(..., shallow=True)`.

## Differentiators vs alternatives

*Validated via WebSearch, 2026-07-01.* The single-agent viewer space is
crowded (claude-code-viewer, claude-code-history-viewer,
claude-session-viewer, simonw/claude-code-transcripts, claude-view); a
handful of newer tools *are* cross-agent (jazzyalex/agent-sessions,
Dicklesworthstone/coding_agent_session_search, hacktivist123/
agent-session-resume). Where `ai-r` differs:

| Capability | Single-agent viewers | Cross-agent session tools | `ai-r` |
|---|---|---|---|
| Reads >1 agent's logs | No | Yes | Yes — Claude, Codex, OpenCode, Antigravity, Pi |
| Programmatic surface | Mostly GUI/TUI | Mostly TUI/CLI/app | **MCP + CLI + Python SDK** |
| Attribution (edit/command → agent + intent) | — | Partial (provenance in some) | Yes — `find-file-edits` / `find-tool-calls`, each with `intent` |
| Audit-replay (reconstruct what a session changed, no git) | — | Rare | Yes — `session_diff` |
| Plan extraction (final vs draft, normalized) | — | — | Yes — `plan` |
| Scope | Viewer | Search / resume / memory | **Read-only extraction core** (graph/memory left to consumers) |

Some cross-agent tools go the *other* direction — toward persistent
memory or coordination layers (e.g. `cass_memory_system`,
`mcp_agent_mail`). `ai-r` deliberately stops at read-only extraction:
memory and graphs are consumer-side, not baked in. Where a competitor's
exact capabilities are unclear from public docs, the table above
understates rather than over-claims.

## Design boundaries — a reader, not a guard

- **Read-only.** `ai-r` never executes agent code and never writes to
  agent session storage. It reads and returns.
- **No graph, no memory.** It extracts entities (turns, tool calls,
  plans, intents). Building a knowledge graph or persistent memory on
  top is a consumer's job, out of this repo's scope.
- **Not an access-control layer.** Any caller that can reach the CLI, the
  MCP server, or the package can read any session — there is no
  authorization in front of the parsers. Keep it where untrusted local
  callers can't reach it.
- **Session content is untrusted.** A reader's caller (auditor,
  summarizer, replay agent) must treat session content as *data, not
  instructions*. See [Security — untrusted session content](docs/security.md).

Workflow-specific reviewers, summaries, and audits live outside this
repo and consume the parser API (`read_messages`).

### Known limitations

- **Antigravity** — fixture coverage plus optional real-data smoke tests when a local brain directory exists.
- **Codex CLI shell edits** — `find_file_edits` recovers codex file writes from `exec_command` / `local_shell_call` shell commands via a conservative quote-aware redirection scan (`>` / `>>`). Writes done through `tee` / `sed -i` / `cp` / `mv` / heredoc-only are not detected; structured edits (`apply_patch` / `write_file`) always are.

See [docs/parsers.md](docs/parsers.md) for the full parser-coverage matrix.

## MCP registration

`ai-r-mcp` is a stdio MCP server. Register it once per host tool.
Replace `USER` with your username (or drop the absolute path if
`ai-r-mcp` is on your `PATH`). **Restart the host tool after editing
its config** — none of them pick up MCP changes live.

The snippets below use `/home/USER/.local/bin/ai-r-mcp`. Adjust the
path if your install lives elsewhere (`which ai-r-mcp` tells you).

### Claude Code

Edit `~/.claude.json` (top-level `mcpServers` object):

```json
{
  "mcpServers": {
    "ai-r": {
      "type": "stdio",
      "command": "/home/USER/.local/bin/ai-r-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

For a single-project registration, commit a `.mcp.json` at the repo root
(see [`.mcp.json`](./.mcp.json)).

### Codex

Edit `~/.codex/config.toml`:

```toml
[mcp_servers.ai-r]
command = "/home/USER/.local/bin/ai-r-mcp"
args = []
```

### Gemini CLI

Edit `~/.gemini/settings.json` (`mcpServers` object):

```json
{
  "mcpServers": {
    "ai-r": {
      "command": "/home/USER/.local/bin/ai-r-mcp",
      "args": [],
      "timeout": 60
    }
  }
}
```

### OpenCode

Edit `~/.config/opencode/opencode.jsonc` (top-level `mcp` object).
OpenCode differs from the others in three ways: `type` is `"local"` (not
`"stdio"`), `command` is a single fused array (command + args together),
and the env key is `"environment"`.

```json
{
  "mcp": {
    "ai-r": {
      "type": "local",
      "command": ["/home/USER/.local/bin/ai-r-mcp"],
      "enabled": true
    }
  }
}
```

### Antigravity

Edit `~/.gemini/antigravity/mcp_config.json` (`mcpServers` object). This is
distinct from the Gemini CLI config above — Antigravity keeps its MCP config
under `~/.gemini/antigravity/`.

```json
{
  "mcpServers": {
    "ai-r": {
      "command": "/home/USER/.local/bin/ai-r-mcp",
      "args": []
    }
  }
}
```

### Pi — skill, not MCP

Pi (`@earendil-works/pi-coding-agent`) has **no MCP-server config** to edit.
It uses an extension/skill model (`pi install <source>`, `pi config`), not an
`mcpServers` map, so `ai-r-mcp` cannot be registered as an in-process
MCP tool inside Pi (and spawning it in-process would violate Pi's design
contract). Instead, `install/agent-configs.sh` drops a read-only **CLI skill**
into `~/.agents/skills/ai-r/` — a directory Pi already scans. The skill
teaches the model to call the `ai-r` CLI from a Pi bash session, with no
MCP spawn involved. Pi sessions are also fully readable *by* `ai-r` via
the CLI (`ai-r list --agent pi`, `ai-r read …`) or the Python SDK;
both read the `~/.pi/agent/sessions/` files directly. For a `/ai-r` slash
command, set `enableSkillCommands: true` in `~/.pi/agent/settings.json` (the
skill's text works even with the default `false`).

### Notes

- `ai-r-mcp` must be on `PATH`, or use the absolute path as above.
- JSON config patching uses `jq`. If `jq` is missing, the Codex, OpenCode,
  and Pi registrations still complete; the Claude and Antigravity configs
  are skipped — install `jq` or register them by hand with the snippets
  above.
- Restart the host tool after editing its config file.
- The server is read-only; any caller that can reach it can read any
  session. See [Design boundaries](#design-boundaries--a-reader-not-a-guard).

<!-- scenarios:start -->

## Acceptance summary

Full spec: [docs/scenarios.md](docs/scenarios.md) — 30 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 7 | Facet filters return correct event shape (references, no body inlined); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result. |
| `get_body` | 4 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated. |
| `aggregate` | 4 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash. |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 5 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal. |
| `session_stats` (preset) | 3 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |

<!-- scenarios:end -->

## Development

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ tests, ≥80% coverage required by CI
- Conventional Commits (`feat:`, `fix:`, `docs:`, …)
- See [CONTRIBUTING.md](./CONTRIBUTING.md) and [docs/parsers.md](./docs/parsers.md) for adding new agents
- `src/ai_r/validators/` and `src/ai_r/templates/` are optional
  standalone helpers (session-note markdown validation), not part of the
  CLI or MCP surface.

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

**Get started:** clone + `bash install.sh`, then register the MCP server
for your agent ([Claude](#claude-code) · [Codex](#codex) ·
[OpenCode](#opencode) · [Antigravity](#antigravity) · [Pi](#pi--skill-not-mcp))
and restart the host tool. One read-only surface for every agent's
history.
