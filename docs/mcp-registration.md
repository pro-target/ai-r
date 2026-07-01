# MCP registration

`ai-r-mcp` is a stdio MCP server. Register it once per host tool.
Replace `USER` with your username (or drop the absolute path if
`ai-r-mcp` is on your `PATH`). **Restart the host tool after editing
its config** — none of them pick up MCP changes live.

The snippets below use `/home/USER/.local/bin/ai-r-mcp`. Adjust the
path if your install lives elsewhere (`which ai-r-mcp` tells you).

## Claude Code

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
(see [`.mcp.json`](../.mcp.json)).

## Codex

Edit `~/.codex/config.toml`:

```toml
[mcp_servers.ai-r]
command = "/home/USER/.local/bin/ai-r-mcp"
args = []
```

## Gemini CLI

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

## OpenCode

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

## Antigravity

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

## Pi — skill, not MCP

Pi (`@earendil-works/pi-coding-agent`) has **no MCP-server config** to edit — it
uses an extension/skill model (`pi install <source>`, `pi config`), not an
`mcpServers` map. So `ai-r-mcp` cannot be registered as an in-process MCP tool
inside Pi (and spawning it in-process would violate Pi's design contract).

Instead:

- **Install.** `install/agent-configs.sh` drops a read-only **CLI skill** into
  `~/.agents/skills/ai-r/` — a directory Pi already scans. The skill teaches the
  model to call the `ai-r` CLI from a Pi bash session, with no MCP spawn.
- **Reading Pi's own sessions.** Pi sessions are fully readable *by* `ai-r` via
  the CLI (`ai-r list --agent pi`, `ai-r read …`) or the Python SDK — both read
  the `~/.pi/agent/sessions/` files directly.
- **Slash command.** For a `/ai-r` command, set `enableSkillCommands: true` in
  `~/.pi/agent/settings.json` (the skill's text works even with the default
  `false`).

## Notes

- `ai-r-mcp` must be on `PATH`, or use the absolute path as above.
- JSON config patching uses `jq`. If `jq` is missing, the Codex, OpenCode,
  and Pi registrations still complete; the Claude and Antigravity configs
  are skipped — install `jq` or register them by hand with the snippets
  above.
- Restart the host tool after editing its config file.
- The server is read-only; any caller that can reach it can read any
  session.
