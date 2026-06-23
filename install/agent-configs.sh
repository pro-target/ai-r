#!/usr/bin/env bash
# Patch MCP configs for 4 agents + install a CLI skill for Pi.
# Idempotent: re-running does not duplicate entries.
#
# Why Pi is different: Pi (@earendil-works/pi-coding-agent) has no MCP-server
# host config to patch — it uses an extension/skill model, not an mcpServers
# file. So instead of an MCP entry, patch_pi() drops an ai-reader skill into
# ~/.agents/skills/ai-reader/ (Pi reads that dir). No MCP host, no spawn —
# the skill just teaches the model to call the read-only `ai-reader` CLI.
# Pi sessions are also readable BY ai-reader via CLI/SDK. See install/README.md.
#
# Environment variables:
#   AI_READER_CMD  path to ai-reader-mcp entry point (default: ~/.local/bin/ai-reader-mcp)
#
# This script never deletes any existing keys — it only sets/updates the
# mcpServers / mcp_servers / mcp entries for the "ai-reader" name.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
READER_CMD="${AI_READER_CMD:-$HOME/.local/bin/ai-reader-mcp}"

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

# json_has_key FILE KEY  — true if FILE is a JSON object with .mcpServers.KEY
json_has_key() {
    local file="$1" key="$2"
    [[ -f "$file" ]] || return 1
    jq -e ".mcpServers.\"$key\"" "$file" >/dev/null 2>&1
}

# json_set_key FILE KEY JSON_VALUE — merge KEY=JSON_VALUE under .mcpServers
# Preserves every other top-level key (env, permissions, hooks, mcpServers.*, …).
json_set_mcp_key() {
    local file="$1" key="$2" value="$3"
    local tmp
    tmp="$(mktemp)"
    jq --arg k "$key" --argjson v "$value" '.mcpServers[$k] = $v' "$file" > "$tmp"
    mv "$tmp" "$file"
}

# --- Claude (JSON, mcpServers) ---
patch_claude() {
    local file="$HOME/.claude.json"
    if [[ ! -f "$file" ]]; then
        warn "Claude config not found: $file (skipping)"
        return 0
    fi
    if json_has_key "$file" "ai-reader"; then
        log "Claude:    ai-reader already configured"
        return 0
    fi
    # ensure mcpServers object exists
    local tmp
    tmp="$(mktemp)"
    if ! jq -e '.mcpServers' "$file" >/dev/null 2>&1; then
        jq '. + {mcpServers: {}}' "$file" > "$tmp"
        mv "$tmp" "$file"
    fi
    json_set_mcp_key "$file" "ai-reader" \
        "{\"command\": \"$READER_CMD\", \"args\": [], \"transport\": \"stdio\", \"description\": \"ai-reader: read/list/search local agent sessions\"}"
    log "Claude:    added mcpServers.ai-reader"
}

# --- Codex (TOML, [mcp_servers.ai-reader]) ---
patch_codex() {
    local file="$HOME/.codex/config.toml"
    if [[ ! -f "$file" ]]; then
        warn "Codex config not found: $file (skipping)"
        return 0
    fi
    if grep -Eq '^\[mcp_servers\.ai-reader\]' "$file"; then
        log "Codex:     ai-reader already configured"
        return 0
    fi
    {
        printf '\n# Added by ai-reader installer\n'
        printf '[mcp_servers.ai-reader]\n'
        printf 'command = "%s"\n' "$READER_CMD"
        printf 'args = []\n'
        printf 'description = "ai-reader: read/list/search local agent sessions"\n'
    } >> "$file"
    log "Codex:     added [mcp_servers.ai-reader]"
}

# --- OpenCode (JSONC, mcp.ai-reader) ---
patch_opencode() {
    local file="$HOME/.config/opencode/opencode.jsonc"
    if [[ ! -f "$file" ]]; then
        warn "OpenCode config not found: $file (skipping)"
        return 0
    fi
    # Detect via simple grep on the key (JSONC allows comments)
    if grep -Eq '"ai-reader"' "$file" && grep -Eq '"mcp"\s*:' "$file"; then
        log "OpenCode:  ai-reader already configured"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        err "OpenCode: python3 not found — cannot edit JSONC safely"
        return 1
    fi
    python3 - "$file" "$READER_CMD" <<'PY'
import json, re, sys, os

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
if "ai-reader" in (data.get("mcp") or {}):
    # idempotent
    print("OpenCode:  ai-reader already present (idempotent skip)", file=sys.stderr)
    sys.exit(0)
data["mcp"]["ai-reader"] = {
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
    log "OpenCode:  added mcp.ai-reader"
}

# --- Antigravity (JSON, mcpServers) ---
patch_antigravity() {
    local file="$HOME/.gemini/antigravity/mcp_config.json"
    if [[ ! -f "$file" ]]; then
        warn "Antigravity config not found: $file (skipping)"
        return 0
    fi
    if json_has_key "$file" "ai-reader"; then
        log "Antigravity: ai-reader already configured"
        return 0
    fi
    local tmp
    tmp="$(mktemp)"
    if ! jq -e '.mcpServers' "$file" >/dev/null 2>&1; then
        jq '. + {mcpServers: {}}' "$file" > "$tmp"
        mv "$tmp" "$file"
    fi
    json_set_mcp_key "$file" "ai-reader" \
        "{\"command\": \"$READER_CMD\", \"args\": [], \"description\": \"ai-reader: read/list/search local agent sessions\"}"
    log "Antigravity: added mcpServers.ai-reader"
}

# --- Pi (skill, not MCP — Pi has no mcpServers host config) ---
# Pi cannot host ai-reader-mcp as an in-process MCP tool (design contract).
# Instead we drop a read-only CLI skill into the shared skills dir that Pi
# already reads; the model then calls `ai-reader` via bash. No spawn, no MCP.
patch_pi() {
    # Cleanup: dangling symlink left by the abandoned in-process MCP extension.
    local old_ext="$HOME/.pi/agent/extensions/ai-reader"
    if [[ -L "$old_ext" && ! -e "$old_ext" ]]; then
        rm "$old_ext"
        warn "Pi:       removed dangling extension symlink $old_ext"
    fi

    local dest="$HOME/.agents/skills/ai-reader/SKILL.md"
    local src="$REPO_DIR/install/pi/skills/ai-reader/SKILL.md"
    if [[ ! -f "$src" ]]; then
        warn "Pi:       skill source missing: $src (skipping)"
        return 0
    fi
    if [[ -f "$dest" ]]; then
        log "Pi:       skill already installed (~/.agents/skills/ai-reader/)"
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    log "Pi:       installed skill → ~/.agents/skills/ai-reader/SKILL.md"
}

# --- entrypoint ---
hdr="==> patching 4 agent MCP configs + Pi skill"
printf "\n%s\n" "$hdr"

# Pre-flight: jq is required for the Claude + Antigravity JSON patches.
# If missing, register what we can (Codex/OpenCode/Pi) and skip the rest
# rather than aborting — matching the README promise.
if ! command -v jq >/dev/null 2>&1; then
    warn "jq not found — skipping Claude and Antigravity (JSON) patches."
    warn "Install jq and re-run, or register ai-reader-mcp by hand (see README 'MCP registration')."
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
