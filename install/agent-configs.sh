#!/usr/bin/env bash
# Patch MCP configs for 4 agents + install a CLI skill for Pi.
# Idempotent: re-running does not duplicate entries.
#
# Why Pi is different: Pi (@earendil-works/pi-coding-agent) has no MCP-server
# host config to patch — it uses an extension/skill model, not an mcpServers
# file. So instead of an MCP entry, patch_pi() drops an ai-r skill into
# ~/.agents/skills/ai-r/ (Pi reads that dir). No MCP host, no spawn —
# the skill just teaches the model to call the read-only `ai-r` CLI.
# Pi sessions are also readable BY ai-r via CLI/SDK. See install/README.md.
#
# Environment variables:
#   AI_R_CMD  path to ai-r-mcp entry point (default: ~/.local/bin/ai-r-mcp)
#
# This script never deletes any existing keys — it only sets/updates the
# mcpServers / mcp_servers / mcp entries for the "ai-r" name.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
READER_CMD="${AI_R_CMD:-$HOME/.local/bin/ai-r-mcp}"

# Colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; NC=''
fi

log()  { printf "${GREEN}[+]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
err()  { printf "${RED}[x]${NC} %s\n" "$*" >&2; }

# --- helpers ---

backup_file() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    local backup="${file}.bak.$(date +%Y%m%d%H%M%S)"
    cp "$file" "$backup"
    log "Backup:    $backup"
}

atomic_replace() {
    local tmp="$1" file="$2"
    backup_file "$file"
    mv "$tmp" "$file"
}

# json_has_key FILE KEY  — true if FILE is a JSON object with .mcpServers.KEY
json_has_key() {
    local file="$1" key="$2"
    [[ -f "$file" ]] || return 1
    jq -e ".mcpServers.\"$key\"" "$file" >/dev/null 2>&1
}

# json_set_key FILE KEY — merge the ai-r command under .mcpServers
# Preserves every other top-level key (env, permissions, hooks, mcpServers.*, …).
json_set_mcp_key() {
    local file="$1" key="$2"
    local tmp
    tmp="$(mktemp)"
    jq --arg k "$key" --arg cmd "$READER_CMD" \
        '.mcpServers[$k] = {
            "command": $cmd,
            "args": [],
            "transport": "stdio",
            "description": "ai-r: read/list/search local agent sessions"
        }' "$file" > "$tmp"
    atomic_replace "$tmp" "$file"
}

toml_quote() {
    python3 - "$1" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1]))
PY
}

# --- Claude (JSON, mcpServers) ---
patch_claude() {
    local file="$HOME/.claude.json"
    if [[ ! -f "$file" ]]; then
        warn "Claude config not found: $file (skipping)"
        return 0
    fi
    if json_has_key "$file" "ai-r"; then
        log "Claude:    ai-r already configured"
        return 0
    fi
    # ensure mcpServers object exists
    local tmp
    tmp="$(mktemp)"
    if ! jq -e '.mcpServers' "$file" >/dev/null 2>&1; then
        jq '. + {mcpServers: {}}' "$file" > "$tmp"
        atomic_replace "$tmp" "$file"
    fi
    json_set_mcp_key "$file" "ai-r"
    log "Claude:    added mcpServers.ai-r"
}

# --- Codex (TOML, [mcp_servers.ai-r]) ---
patch_codex() {
    local file="$HOME/.codex/config.toml"
    if [[ ! -f "$file" ]]; then
        warn "Codex config not found: $file (skipping)"
        return 0
    fi
    if grep -Eq '^\[mcp_servers\.ai-r\]' "$file"; then
        log "Codex:     ai-r already configured"
        return 0
    fi
    backup_file "$file"
    local quoted_cmd
    quoted_cmd="$(toml_quote "$READER_CMD")"
    {
        printf '\n# Added by ai-r installer\n'
        printf '[mcp_servers.ai-r]\n'
        printf 'command = %s\n' "$quoted_cmd"
        printf 'args = []\n'
        printf 'description = "ai-r: read/list/search local agent sessions"\n'
    } >> "$file"
    log "Codex:     added [mcp_servers.ai-r]"
}

# --- OpenCode (JSONC, mcp.ai-r) ---
patch_opencode() {
    local file="$HOME/.config/opencode/opencode.jsonc"
    if [[ ! -f "$file" ]]; then
        warn "OpenCode config not found: $file (skipping)"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        err "OpenCode: python3 not found — cannot edit JSONC safely"
        return 1
    fi
    if python3 - "$file" <<'PY'
import json
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()
text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)
data = json.loads(text) if text.strip() else {}
sys.exit(0 if "ai-r" in (data.get("mcp") or {}) else 1)
PY
    then
        log "OpenCode:  ai-r already configured"
        return 0
    fi
    backup_file "$file"
    python3 - "$file" "$READER_CMD" <<'PY'
import json, re, sys

path, reader_cmd = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

# Strip // line comments and /* */ block comments (preserve the trailing newline count roughly)
def strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"(^|\s)//[^\n]*", r"\1", s)
    return s

clean = strip_comments(text)
data = json.loads(clean) if clean.strip() else {}
data.setdefault("mcp", {})
if "ai-r" in (data.get("mcp") or {}):
    # idempotent
    print("OpenCode:  ai-r already present (idempotent skip)", file=sys.stderr)
    sys.exit(0)
data["mcp"]["ai-r"] = {
    "type": "local",
    "command": [reader_cmd],
}

# Pretty-print, preserve a leading "$schema" line if it was the only top-level key.
out = json.dumps(data, indent=2, ensure_ascii=False)
# Restore top-of-file $schema if it existed
m_schema = re.search(r'"\$schema"\s*:\s*"([^"]+)"', text)
if m_schema and '"$schema"' not in out:
    out = '{\n  "$schema": "%s",\n%s' % (m_schema.group(1), out.split("{\n", 1)[1])
with open(path, "w", encoding="utf-8") as f:
    f.write(out)
    if not out.endswith("\n"):
        f.write("\n")
PY
    log "OpenCode:  added mcp.ai-r"
}

# --- Antigravity (JSON, mcpServers) ---
patch_antigravity() {
    local file="$HOME/.gemini/antigravity/mcp_config.json"
    if [[ ! -f "$file" ]]; then
        warn "Antigravity config not found: $file (skipping)"
        return 0
    fi
    if json_has_key "$file" "ai-r"; then
        log "Antigravity: ai-r already configured"
        return 0
    fi
    local tmp
    tmp="$(mktemp)"
    if ! jq -e '.mcpServers' "$file" >/dev/null 2>&1; then
        jq '. + {mcpServers: {}}' "$file" > "$tmp"
        atomic_replace "$tmp" "$file"
    fi
    json_set_mcp_key "$file" "ai-r"
    log "Antigravity: added mcpServers.ai-r"
}

# --- Pi (skill, not MCP — Pi has no mcpServers host config) ---
# Pi cannot host ai-r-mcp as an in-process MCP tool (design contract).
# Instead we drop a read-only CLI skill into the shared skills dir that Pi
# already reads; the model then calls `ai-r` via bash. No spawn, no MCP.
patch_pi() {
    # Cleanup: dangling symlink left by the abandoned in-process MCP extension.
    local old_ext="$HOME/.pi/agent/extensions/ai-r"
    if [[ -L "$old_ext" && ! -e "$old_ext" ]]; then
        rm "$old_ext"
        warn "Pi:       removed dangling extension symlink $old_ext"
    fi

    local dest="$HOME/.agents/skills/ai-r/SKILL.md"
    local src="$REPO_DIR/install/pi/skills/ai-r/SKILL.md"
    if [[ ! -f "$src" ]]; then
        warn "Pi:       skill source missing: $src (skipping)"
        return 0
    fi
    if [[ -f "$dest" ]]; then
        log "Pi:       skill already installed (~/.agents/skills/ai-r/)"
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    log "Pi:       installed skill → ~/.agents/skills/ai-r/SKILL.md"
}

# --- entrypoint ---
hdr="==> patching 4 agent MCP configs + Pi skill"
printf "\n%s\n" "$hdr"

# Pre-flight: jq is required for the Claude + Antigravity JSON patches.
# If missing, register what we can (Codex/OpenCode/Pi) and skip the rest
# rather than aborting — matching the README promise.
if ! command -v jq >/dev/null 2>&1; then
    warn "jq not found — skipping Claude and Antigravity (JSON) patches."
    warn "Install jq and re-run, or register ai-r-mcp by hand (see README 'MCP registration')."
    patch_codex
    patch_opencode
    patch_pi
    log "agent-configs.sh: done (jq missing — Claude/Antigravity skipped)"
    exit 0
fi

patch_claude
patch_codex
patch_opencode
patch_antigravity
patch_pi

log "agent-configs.sh: done"
