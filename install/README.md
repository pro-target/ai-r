# Install `ai-r`

`ai-r` ships with an idempotent installer. It works in three modes and
patches the four agent configs (Claude, Codex, OpenCode, Antigravity) in place.

**Pi is intentionally not auto-registered.** Pi (`@earendil-works/pi-coding-agent`)
has no MCP-server host config — it uses an extension/skill model, not an
`mcpServers` file the installer could patch. Pi sessions are still fully
readable *by* `ai-r` (via the CLI or Python SDK); they just cannot host
`ai-r-mcp` as an in-process MCP tool. See
[Pi — no MCP host](#pi--no-mcp-host) below.

## Quick start

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r
bash install.sh
```

If `sudo -n` works (NOPASSWD) the installer prefers `/opt/ai-r/`. Otherwise
it falls back to `~/.local/share/ai-r/`. Override with the first argument
or `INSTALL_MODE=...`.

## Modes

| Mode     | Install dir                       | Binaries                              | Needs sudo |
|----------|-----------------------------------|---------------------------------------|------------|
| `opt`    | `/opt/ai-r/`                 | `/opt/ai-r/.venv/bin/ai-r*` | yes        |
| `user`   | `~/.local/share/ai-r/`       | `~/.local/share/ai-r/.venv/bin/` | no         |
| `auto`   | (default — picks `opt` or `user`) |                                       | maybe      |

In both modes, two symlinks land in `~/.local/bin/` (so `ai-r` is on
`$PATH` for the current user):

- `~/.local/bin/ai-r`      → entry point in venv
- `~/.local/bin/ai-r-mcp`  → MCP server entry point in venv

If `python3 -m venv` is unavailable (e.g. on systems without
`python3-venv`), the installer falls back to
`pip install --break-system-packages` and places entry points in
`~/.local/bin/` directly.

## What gets patched

`install/agent-configs.sh` adds an `ai-r` entry to the MCP config of
each installed agent. Existing entries are preserved untouched.

| Agent       | Config file                                    | Format | Key added               |
|-------------|------------------------------------------------|--------|-------------------------|
| Claude      | `~/.claude.json`                               | JSON   | `mcpServers["ai-r"]` |
| Codex       | `~/.codex/config.toml`                         | TOML   | `[mcp_servers.ai-r]` |
| OpenCode    | `~/.config/opencode/opencode.jsonc`            | JSONC  | `mcp["ai-r"]`        |
| Antigravity | `~/.gemini/antigravity/mcp_config.json`        | JSON   | `mcpServers["ai-r"]` |
| Pi          | `~/.agents/skills/ai-r/SKILL.md`          | skill  | CLI skill (no MCP host)   |

Re-running `bash install.sh` is safe — already-present entries are detected
and skipped.

### Pi — skill, not MCP

Pi is special. As of Pi v0.79.x there is no MCP config file
(`~/.pi/agent/settings.json` holds only UI/theme keys; the `pi` binary exposes
an extension/skill system, not an `mcpServers` map), so `ai-r-mcp` cannot
be registered as an in-process MCP tool. Instead the installer drops a
read-only **CLI skill** at `~/.agents/skills/ai-r/SKILL.md` — a directory
Pi already scans. The skill teaches the model to call the `ai-r` CLI from
a Pi bash session (no MCP spawn, no design-contract violation). You can also
call the CLI directly (`ai-r list`, `ai-r read …`) or the Python SDK
— both read the session files on disk. For a `/ai-r` slash command, set
`enableSkillCommands: true` in `~/.pi/agent/settings.json`.

## Verify

```bash
which ai-r ai-r-mcp
ai-r --version
ai-r list --agent claude | head -5
```

And confirm the four configs are intact:

```bash
jq  '.mcpServers | keys'                          ~/.claude.json
grep 'mcp_servers.ai-r'                      ~/.codex/config.toml
grep 'ai-r'                                  ~/.config/opencode/opencode.jsonc
jq  '.mcpServers | keys'                          ~/.gemini/antigravity/mcp_config.json
```

## Uninstall

```bash
bash uninstall.sh           # remove symlinks + 4 config entries
bash uninstall.sh --purge   # also remove /opt/ai-r or ~/.local/share/ai-r
```

The source repo at `~/dev/ai-r/` is never touched.

## Troubleshooting

**`sudo` keeps prompting.** Set `INSTALL_MODE=user` (no sudo needed):

```bash
INSTALL_MODE=user bash install.sh
```

**`python3 -m venv` fails with "ensurepip not available".** Install
`python3-venv` (or the matching `python3.X-venv` package), or accept the
`--break-system-packages` fallback the installer uses automatically.

**A config file is missing.** The installer skips that agent and prints
a warning. It does not abort the run.

**Re-install after a config rewrite.** Just re-run `bash install.sh` — it
detects existing entries and reuses the venv.

## Files

| Path                          | Purpose                                              |
|-------------------------------|------------------------------------------------------|
| `install.sh`                  | Main installer                                       |
| `install/agent-configs.sh`    | Patches 4 agent MCP configs (Pi excluded — no MCP host; see above)  |
| `install/README.md`           | This file                                            |
| `uninstall.sh`                | Reverse of install                                   |
