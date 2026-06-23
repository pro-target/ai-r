# ai-reader

[![CI](https://github.com/pro-target/ai-reader/workflows/CI/badge.svg)](https://github.com/pro-target/ai-reader/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> Read Claude, Codex, OpenCode, Antigravity, and Pi sessions through a single MCP server, CLI, or Python package.

## Why?

Every AI agent stores its conversation logs on disk in a different
place and format — JSONL for Claude and Codex, SQLite for OpenCode,
brain directories for Antigravity, project JSONL files for Pi. `ai-reader` gives you one read-only
interface across all of them.

`ai-reader` is a **reader**, not a guard. Any caller that can reach the
CLI, the MCP server, or the package can read any session. There is no
access-control layer in front of the parsers.

## When it helps

One reader across every agent unlocks workflows that a single-agent log
cannot:

- **Hit a provider limit? Switch agents and keep going.** Ran out of
  Codex quota mid-task? Spin up Antigravity, point it at the Codex
  session, and ask it to continue — same task, different model, no lost
  context.
- **Ran out of context window? Start fresh and resume.** Begin a new
  session, hand it the previous session's UUID, and say "continue from
  here." The prior transcript is readable regardless of which agent
  wrote it.
- **Cross-agent handoff & triage.** "What did the other agent do on
  this?" works across Claude, Codex, OpenCode, Antigravity, and Pi
  without learning five different log layouts.
- **"Who touched this file, and when?"** Every edit to a path — across
  every agent, every session — with timestamps. Time-box an audit: "what
  did agents do to `src/auth.py` last week?" (see `find-file-edits`).
- **Replay a session as a CHANGELOG round.** Render a session into
  Goal / Status / Files touched / Decisions / Next-actions markdown — a
  handoff doc you can paste into another agent or a standup (see
  `export rounds`).
- **"Which agent am I, and what session am I in?"** A script or an agent
  bootstrapping fresh detects its own session UUID, then reads itself or
  its predecessor to resume programmatically (see `detect-agent`,
  `detect-session`).
- **Search the bodies, not just titles.** "Find every session that
  discussed `auth token`" — across all five agents, with snippets — via
  body-scoped search with `AND`/`OR`/`NOT` operators.

## Quick Start (1 request)

Prerequisites: Python 3.11+ with either `venv` (`python3-venv`) or `pip`
(`python3-pip`/`pip3`), and `jq` (used to auto-register the Claude and
Antigravity MCP configs — the others need no `jq`).

```bash
git clone https://github.com/pro-target/ai-reader.git ~/dev/ai-reader
cd ~/dev/ai-reader && bash install.sh
```

That's it. The installer:
- Detects install mode (system-wide with sudo, or per-user without)
- Creates a venv, installs the package
- Patches MCP configs for **Claude**, **Codex**, **OpenCode**, **Antigravity** when those config files exist
- Runs smoke tests

## Supported Agents

| Agent | Storage | Parser |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite (auto-detects snap/flatpak) |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain directories |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 1: Public API (3 surfaces)                             │
│   • ai-reader CLI (argparse)                                 │
│   • ai-reader-mcp (MCP server, stdio JSON-RPC)               │
│   • from ai_reader.parsers import ...  (Python SDK)          │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Layer 2: Core (parsers/, models)                             │
│   • claude, codex, opencode (SQLite), antigravity, pi        │
│   • Auto-detect snap/flatpak OpenCode DBs                    │
└──────────────────────────────────────────────────────────────┘
```

## Design boundaries

`ai-reader` is the public core: parsers, typed messages, CLI, and MCP. Workflow-specific reviewers, summaries, and audits live outside this repo and consume the parser API (`read_messages`).

Session content is **untrusted** — a reader's caller (auditor, summarizer, replay agent) must treat it as data, not instructions. See [Security — untrusted session content](docs/security.md).

## Known limitations

- **Antigravity** — fixture coverage plus optional real-data smoke tests when a local brain directory exists.
- **Codex CLI shell edits** — `find_file_edits` recovers codex file writes from `exec_command` / `local_shell_call` shell commands via a conservative quote-aware redirection scan (`>` / `>>`). Writes done through `tee` / `sed -i` / `cp` / `mv` / heredoc-only are not detected; structured edits (`apply_patch` / `write_file`) always are.

See [docs/parsers.md](docs/parsers.md) for the full parser-coverage matrix.

## Usage

### As an MCP server (recommended)

The MCP server is auto-registered in your agent's config. Tools available:

| Tool | Purpose |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | List discoverable sessions, optionally filtered by agent. Paginated: `limit=0` = uncapped; response carries `total`/`offset`/`limit`/`truncated`. |
| `read_session(uuid, agent, offset?, limit?)` | Read one session; returns up to 100 messages by default. Pass `offset`/`limit` to page further. |
| `find_file_edits(path, agent?, since?, until?, limit?)` | Find every file edit across sessions for a given path, cross-agent by default, optionally time-boxed (`since`/`until` ISO 8601). |
| `search_sessions(query, agent?, scope?, operator?, limit?)` | Search by title and/or message body, with `AND`/`OR`/`NOT` operators and Google-style `-term` exclusions. See [Search operators](#search-operators). |

**Pagination** (`limit`/`offset`, plus a `truncated` flag when more pages remain) is exposed on the MCP tools and the Python SDK — see [architecture.md](docs/architecture.md).

### As a CLI (testing / scripts)

```bash
# list / read / search
ai-reader list --agent pi
ai-reader read --agent pi <session-uuid>
ai-reader search "refactor"
ai-reader search "pwa manifest" --scope body --operator and --agent claude

# who edited a file, across all agents, optionally time-boxed
ai-reader find-file-edits src/auth.py --since 2026-06-01 --until 2026-06-30
ai-reader find-file-edits "config" --agent claude --limit 20

# which agent / session am I in (scripts, orchestration, self-resume)
ai-reader detect-agent --quiet          # → e.g. "claude"
ai-reader detect-session --json         # → candidate session UUIDs

# render a session as a CHANGELOG round (handoff doc / replay)
ai-reader export rounds <session-uuid> --include-round --output round.md
```

Add `--json` to most subcommands for machine-readable output.

### Search operators

`search_sessions` (MCP) and `ai-reader search` (CLI) share the same
query grammar. Default behaviour (`scope="title"`, `operator="AND"`,
`limit=50`) is unchanged from the previous title-only substring search.

**Query syntax**

| Form | Example | Meaning |
|---|---|---|
| Bare words | `pwa manifest` | Both terms (operator controls how). |
| Quoted phrase | `"exact phrase"` | Single literal term. |
| Negative prefix | `-claude` | Google-style: this term must NOT appear. |

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
excerpt, up to 200 characters.

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
ai-reader search "refactor"

# body search, all terms must appear, exclude claude
ai-reader search "pwa manifest -claude" --scope body --operator and

# body search, any term, max 5 results
ai-reader search "pwa OR manifest" --scope body --operator or --limit 5

# everything containing neither of these terms
ai-reader search "auth login" --scope body --operator not
```

### As a Python SDK

```python
from ai_reader.parsers import AgentName, claude

for session in claude.list_sessions():
    print(session.uuid, session.title)

session = claude.read_session("<session-uuid>")
print(session.message_count)
```

See [docs/architecture.md](./docs/architecture.md) for the full layering.

## MCP registration

`ai-reader-mcp` is a stdio MCP server. Register it once per host tool.
Replace `USER` with your username (or drop the absolute path if
`ai-reader-mcp` is on your `PATH`). **Restart the host tool after editing
its config** — none of them pick up MCP changes live.

The snippets below use `/home/USER/.local/bin/ai-reader-mcp`. Adjust the
path if your install lives elsewhere (`which ai-reader-mcp` tells you).

### Claude Code

Edit `~/.claude.json` (top-level `mcpServers` object):

```json
{
  "mcpServers": {
    "ai-reader": {
      "type": "stdio",
      "command": "/home/USER/.local/bin/ai-reader-mcp",
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
[mcp_servers.ai-reader]
command = "/home/USER/.local/bin/ai-reader-mcp"
args = []
```

### Gemini CLI

Edit `~/.gemini/settings.json` (`mcpServers` object):

```json
{
  "mcpServers": {
    "ai-reader": {
      "command": "/home/USER/.local/bin/ai-reader-mcp",
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
    "ai-reader": {
      "type": "local",
      "command": ["/home/USER/.local/bin/ai-reader-mcp"],
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
    "ai-reader": {
      "command": "/home/USER/.local/bin/ai-reader-mcp",
      "args": []
    }
  }
}
```

### Pi — skill, not MCP

Pi (`@earendil-works/pi-coding-agent`) has **no MCP-server config** to edit.
It uses an extension/skill model (`pi install <source>`, `pi config`), not an
`mcpServers` map, so `ai-reader-mcp` cannot be registered as an in-process
MCP tool inside Pi (and spawning it in-process would violate Pi's design
contract). Instead, `install/agent-configs.sh` drops a read-only **CLI skill**
into `~/.agents/skills/ai-reader/` — a directory Pi already scans. The skill
teaches the model to call the `ai-reader` CLI from a Pi bash session, with no
MCP spawn involved. Pi sessions are also fully readable *by* `ai-reader` via
the CLI (`ai-reader list --agent pi`, `ai-reader read …`) or the Python SDK;
both read the `~/.pi/agent/sessions/` files directly. For a `/ai-reader` slash
command, set `enableSkillCommands: true` in `~/.pi/agent/settings.json` (the
skill's text works even with the default `false`).

### Notes

- `ai-reader-mcp` must be on `PATH`, or use the absolute path as above.
- JSON config patching uses `jq`. If `jq` is missing, the Codex, OpenCode,
  and Pi registrations still complete; the Claude and Antigravity configs
  are skipped — install `jq` or register them by hand with the snippets
  above.
- Restart the host tool after editing its config file.
- The server is read-only; any caller that can reach it can read any
  session. See [Design boundaries](#design-boundaries).

## Development

```bash
git clone https://github.com/pro-target/ai-reader.git
cd ai-reader
pip install -e ".[dev]"
pytest --cov=src/ai_reader
```

- 350+ tests, ≥80% coverage required by CI
- Conventional Commits (`feat:`, `fix:`, `docs:`, …)
- See [CONTRIBUTING.md](./CONTRIBUTING.md) and [docs/parsers.md](./docs/parsers.md) for adding new agents
- `src/ai_reader/validators/` and `src/ai_reader/templates/` are optional
  standalone helpers (session-note markdown validation), not part of the
  CLI or MCP surface.

## License

MIT — see [LICENSE](./LICENSE).
