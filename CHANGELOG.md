# Changelog

All notable changes to this project are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-23

### Added

- **MCP `find_file_edits`**: new tool — find every file edit across
  sessions for a given path, cross-agent by default, optionally
  time-boxed with `since`/`until` (ISO 8601). Raises the MCP tool count
  from 3 to 4 (`list_sessions`, `read_session`, `search_sessions`,
  `find_file_edits`). See [docs/architecture.md](./docs/architecture.md).
- **CLI `ai-r find-file-edits`**: mirrors the MCP tool with
  `--agent`, `--since`, `--until`, `--limit`.
- **MCP `search_sessions`**: extended with `scope`, `operator`, `limit`
  parameters and a Google-style `-term` negative prefix in the query.
  The default values (`scope="title"`, `operator="AND"`, `limit=50`)
  preserve the historical title-substring behaviour, so existing
  callers are unaffected. New abilities:
  - `scope="body"` searches message text + `tool_use[*].input` +
    `tool_result[*].content` across every session.
  - `scope="all"` searches title OR body.
  - `operator` accepts `AND` (default), `OR`, or `NOT` for the
    combination of positive terms. Negative `-term` tokens are
    always excluded regardless of operator.
  - Quoted phrases (`"exact phrase"`) are supported via `shlex.split`.
  - When a `body`/`all` search hits, the result includes a `snippet`
    field with the first matching excerpt (up to 200 chars).
- **CLI `ai-r search`**: mirrors the new parameters as
  `--scope {title,body,all}` and `--operator {and,or,not}` (alias
  `--op`). Validation of `--limit` matches the MCP tool. Date
  filters (`--days`/`--from-date`/`--to-date`) compose with the
  new flags.
- **Tests**: added MCP/CLI coverage for backward-compat, all three
  operators, negative prefixes, quoted phrases, tool-call matching,
  snippets, `limit`, and validation error paths.

### Changed

- **claude**: `extract_title()` now uses the **first** user message instead
  of the last. Affects downstream summaries or UI that quoted the wrap-up
  turn. If you depended on the old behavior, filter `Session.title` against
  the original last user message via `read_session(...).messages[-1].content`
  until a migration shim is shipped.

### Security

- **codex**: `AI_R_DEDUP_KEY_LEN` is now re-read from the environment on
  every dedup-key call. Previously the value was captured at import time, so
  any runtime change to the environment (operator re-export, test using
  `monkeypatch.setenv` after import, long-running service restart) was
  silently ignored. New `parsers.codex.get_dedup_key_len()` accessor exposes
  the resolved value for callers that want to introspect it.

## [0.1.0] - 2026-06-14

First public alpha release.

### Added

- **Parsers** for 5 agents:
  - `claude` — JSONL at `~/.claude/projects/<project-slug>/<uuid>.jsonl`
  - `codex` — JSONL at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
  - `opencode` — SQLite at `~/.local/share/opencode/opencode.db` (auto-detects snap/flatpak variants under `~/snap/code/*/...` and `~/snap/opencode/*/...`)
  - `antigravity` — brain directories at `~/.gemini/antigravity/brain/` and `~/.gemini/antigravity-cli/brain/`
  - `pi` — JSONL at `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl`
- **`Session` data model** with `uuid`, `agent`, `title`, `date`, `path`, `message_count`, `parent_uuid`, `extra`
- **CLI** (`ai-r`): `list`, `read`, `search` subcommands with `--agent` filter and `--json` output
- **MCP server** (`ai-r-mcp`): 3 tools — `list_sessions`, `read_session`, `search_sessions`
- **install.sh**: idempotent, dual-mode (system-wide with sudo, or per-user), venv or `--break-system-packages` fallback
- **agent-configs.sh**: patches agent MCP configs (claude, codex, opencode, antigravity)
- **uninstall.sh**: clean removal of binaries and MCP entries
- **Tests**: 184 tests, 87% coverage
- **2-layer architecture**: Public API / Core parsers — a read-only reader with no access-control layer in front of the parsers
- **MIT license**

### Notes

- This is an **alpha**. APIs may change before `0.2.0`.
- `ai-r` is a reader, not a guard. Any caller that can reach the CLI, the MCP server, or the package can read any session. See [docs/architecture.md](./docs/architecture.md).
