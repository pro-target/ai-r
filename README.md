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
- **Works over MCP (15 tools).** An agent calls `ai-r` directly in plain
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

- **MCP server** (`ai-r-mcp`) — 15 tools over JSON-RPC, so any MCP agent
  calls it directly (recommended). Default is **stdio**; optionally a **shared
  http server** (one warm process for all agents instead of a per-agent stdio
  swarm), see the `http` extra under Quick start. Registration — see
  [docs/mcp-registration.md](./docs/mcp-registration.md).
- **CLI** (`ai-r`) — subcommands for scripts and manual use (`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`). Search operators —
  [docs/search-operators.md](./docs/search-operators.md).
- **Python SDK** (`from ai_r.parsers import ...`) — parsers, typed
  `Session`/message models, and the event verbs, to build your own tools.

### Method vocabulary

The full dictionary of public verbs and presets (signatures, parameters, behaviour) lives in its own file: [`docs/methods.md`](./docs/methods.md).

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

Optional extra — `tokens`: `AI_R_EXTRAS=tokens bash install.sh` (or
`pip install "ai-r[tokens]"`) adds [tiktoken](https://github.com/openai/tiktoken)
for better token **estimates** on sessions whose format stores no exact usage
numbers. Fully optional: without it exact numbers still come straight from the
session files where recorded, and the fallback estimate degrades to a rough
chars/4 heuristic, honestly labeled `estimate` — never a crash.

Optional extra — `semantic`: `AI_R_EXTRAS=semantic bash install.sh` (or
`pip install "ai-r[semantic]"` + a one-time model download the installer does
for you) enables `sort="semantic"` on text search (`query`, `search_sessions`):
the BM25 top-50 candidates are re-ranked by **meaning** with a local
multilingual embedding model —
[intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
(int8 ONNX, ~118 MB, MIT), run directly via
[onnxruntime](https://onnxruntime.ai) + [tokenizers](https://github.com/huggingface/tokenizers) + [numpy](https://numpy.org),
no torch, no persistent index. Why this model: strong cross-lingual retrieval
(a Russian query finds an English session and vice versa) at a small size. How
the score works, in plain words: BM25 picks the 50 best word-matches (a cost
budget, not a quality cut-off — there is deliberately *no* similarity
threshold, because this model family scores even unrelated texts ≈0.7); within
that pool the final score is **75 % meaning + 25 % word match** — meaning
dominates, the word share keeps exact-term hits from being drowned and breaks
ties. Fully optional: without the packages or model files, `sort="semantic"`
honestly falls back to the BM25 order and the response says why
(`semantic: {active: false, reason, fallback: "bm25"}`) — never a crash.

Two knobs keep the model well-behaved inside a long-lived MCP process (both
env-tunable, both degrading to the default on blank/invalid input — never a
crash): `AI_R_SEMANTIC_THREADS` caps how many CPU threads onnxruntime may use
per inference (default `2`, never more than the machine's core count — so it
does not grab every core and fight the server for CPU), and
`AI_R_SEMANTIC_IDLE_SEC` frees the loaded model's ~118 MB of RAM after that
many idle seconds (default `300`); the next request transparently re-loads it.

Optional extra — `http`: `AI_R_EXTRAS=http bash install.sh` (or
`pip install "ai-r[http]"`) adds [uvicorn](https://www.uvicorn.org) and enables
a **shared streamable-http transport**. By default every agent spawns its own
`ai-r-mcp` over stdio — under multi-agent fan-out that is N processes, each with
a cold cache, re-scanning the corpus (the measured cause of RAM exhaustion).
With `AI_R_MCP_TRANSPORT=http` a single **warm server** on localhost (default
`127.0.0.1:8756`) is shared by every agent instead of a swarm; the systemd units
in `packaging/systemd/` add socket-activation with idle self-exit — the process
exists only under load. The bind is loopback-only and **fail-closed**: a
non-localhost `AI_R_MCP_HOST` is refused (transcripts carry secrets and are
served without a token) until the operator explicitly sets
`AI_R_MCP_ALLOW_REMOTE=1`. Other knobs: `AI_R_MCP_PORT`, `AI_R_MCP_IDLE_SEC`
(idle self-exit threshold), `AI_R_HAYSTACK_CACHE_MAX` (search cache ceiling).
Fully optional: without it stdio mode works as before.

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

## Acceptance (end-to-end scenarios)

The public surface is covered by end-to-end scenarios an LLM agent runs against the live MCP (complementing pytest). Full list — [`docs/scenarios.md`](./docs/scenarios.md).

<!-- gallery:start -->
## Example: ai-r in action

A gallery of real examples — one per capability (error analysis, dangerous commands, network trail, token burn, plan comments, commit phantom-check, cross-agent file history, cross-lingual search, zombie subagents, git-less diff): [`docs/examples/showcase-gallery.md`](./docs/examples/showcase-gallery.md).
<!-- gallery:end -->

## Next — documentation

- Method vocabulary (verbs + presets) — [`docs/methods.md`](./docs/methods.md)
  (English SSOT) · [`docs/methods.ru.md`](./docs/methods.ru.md) (Russian mirror)
- Acceptance scenarios (90 e2e) — [`docs/scenarios.md`](./docs/scenarios.md)
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

- 1100+ tests, CI requires ≥85% coverage
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
