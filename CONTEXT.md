# CONTEXT.md — Domain language for AI agents

This file is the canonical domain glossary for `ai-r`. Other agents
working in this codebase (Claude Code, Codex, OpenCode, Antigravity,
Roo, Gemini, etc.) should use these terms consistently.

## What ai-r is

`ai-r` is a read-only multi-agent session reader. It parses the
on-disk conversation logs produced by Claude, Codex, OpenCode,
Antigravity, and Pi and exposes them through three surfaces — a CLI, an MCP
server, and a Python parser package. Any caller can read any session;
there is no access layer in front of the parsers. See Design boundaries in README.

## Glossary

| Term | Definition |
|---|---|
| **Session** | A conversation log persisted by an agent. Format is agent-specific: JSONL (Claude, Codex, Pi), SQLite (OpenCode), brain dir (Antigravity). |
| **Agent** | One of the supported runtimes. Represented in code by the `AgentName` enum (`CLAUDE`, `CODEX`, `OPENCODE`, `ANTIGRAVITY`, `PI`). |
| **MCP** | [Model Context Protocol](https://modelcontextprotocol.io/) — JSON-RPC over stdio for tool calls. |
| **Parser** | A module that reads an agent's session storage and returns `Session` objects. One per supported agent, under `src/ai_r/parsers/`. |
| **Brain** | Antigravity's per-session scratchpad directory. Contains `overview.txt`, `transcript.jsonl`, `walkthrough.md`, `task.md`, etc. |
| **Rollout** | Codex's per-session JSONL file under `~/.codex/sessions/YYYY/MM/DD/rollout-<uuid>.jsonl`. |

## Storage layout

| Agent | Storage | Parser |
|---|---|---|
| Claude Code | `~/.claude/projects/<project-slug>/<session-uuid>.jsonl` | `parsers/claude.py` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and `~/.codex/archived_sessions/YYYY/MM/DD/rollout-*.jsonl` | `parsers/codex.py` |
| OpenCode | `~/.local/share/opencode/opencode.db` (SQLite; auto-detects snap/flatpak) | `parsers/opencode.py` |
| Antigravity | `~/.gemini/antigravity/brain/<session-uuid>/` | `parsers/antigravity.py` |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | `parsers/pi.py` |

Any base directory can be overridden by setting `AI_R_HOME`.

## Module map (where to look first)

| Question | Look at |
|---|---|
| "How is a Claude session parsed?" | [`src/ai_r/parsers/claude.py`](./src/ai_r/parsers/claude.py) |
| "How do I add a new agent?" | [`docs/parsers.md`](./docs/parsers.md) |
| "What's the layering?" | [`docs/architecture.md`](./docs/architecture.md) |

## Cross-references

- This project's canonical docs: [README.md](./README.md), [CHANGELOG.md](./CHANGELOG.md)
- Open issues: <https://github.com/pro-target/ai-r/issues>

## Known limitations

- **OpenCode message bodies**: real-world OpenCode DBs store message
  text and tool calls in a separate `part` table (keyed by
  `message_id`), while `message.data` carries only metadata. The
  parser joins `part` (commit `6b3cfb4`) and assembles `text` from
  `text`/`reasoning` parts, `tool_use`/`tool_result` from `tool`
  parts; legacy DBs without a `part` table fall back to
  `message.data`-inline parts.
