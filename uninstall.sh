#!/usr/bin/env bash
# ai-r uninstaller
#
# Removes:
#   - ~/.local/bin/ai-r, ~/.local/bin/ai-r-mcp  (symlinks)
#   - ai-r entries from 4 agent MCP configs
#   - /opt/ai-r or ~/.local/share/ai-r  (only with --purge)
#
# Always preserves ~/dev/ai-r/ — re-clone to install again.
#
# Usage:
#   bash uninstall.sh           # remove symlinks + config entries
#   bash uninstall.sh --purge   # also remove the install dir (/opt or ~/.local/share)
#   bash uninstall.sh --help

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
PURGE=0

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<EOF
ai-r uninstaller

Usage:
  bash uninstall.sh           Remove symlinks + 4 agent config entries
  bash uninstall.sh --purge   Also remove /opt/ai-r or ~/.local/share/ai-r
  bash uninstall.sh --help    Show this help

Note: ~/dev/ai-r (the source repo) is NEVER touched.
EOF
    exit 0
fi
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=1
fi

if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; NC=''
fi
log()  { printf "${GREEN}[+]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
err()  { printf "${RED}[x]${NC} %s\n" "$*" >&2; }
hdr()  { printf "\n${BOLD}==> %s${NC}\n" "$*"; }
BOLD='\033[1m'

# --- remove symlinks ---
hdr "Removing symlinks in $BIN_DIR"
for name in ai-r ai-r-mcp; do
    target="$BIN_DIR/$name"
    if [[ -L "$target" ]]; then
        rm -f "$target"
        log "removed symlink: $target"
    elif [[ -f "$target" ]]; then
        warn "$target is a regular file (not a symlink) — leaving alone"
    else
        log "$target: not present"
    fi
done

# --- remove config entries ---
hdr "Removing ai-r entries from 4 agent configs"

# Claude — canonical user config is ~/.claude.json; also clean the legacy
# ~/.claude/settings.json location in case an older installer wrote there.
for file in "$HOME/.claude.json" "$HOME/.claude/settings.json"; do
    if [[ -f "$file" ]] && command -v jq >/dev/null 2>&1; then
        if jq -e '.mcpServers."ai-r"' "$file" >/dev/null 2>&1; then
            tmp="$(mktemp)"
            jq 'del(.mcpServers."ai-r")' "$file" > "$tmp"
            mv "$tmp" "$file"
            log "Claude:    removed mcpServers.ai-r ($file)"
        fi
    fi
done
[[ -f "$HOME/.claude.json" ]] || warn "Claude config not found: ~/.claude.json"
command -v jq >/dev/null 2>&1 || warn "jq not found — skipping Claude cleanup"

# Codex (TOML: remove [mcp_servers.ai-r] block + immediate comments)
file="$HOME/.codex/config.toml"
if [[ -f "$file" ]]; then
    if grep -Eq '^\[mcp_servers\.ai-r\]' "$file"; then
        python3 - "$file" <<'PY'
import re, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    text = f.read()
# Remove the ai-r block (header line, all following non-blank, non-section lines)
lines = text.splitlines(keepends=True)
out, skip = [], False
for line in lines:
    if re.match(r'^\s*\[mcp_servers\.ai-r\]\s*$', line):
        skip = True
        continue
    if skip:
        if re.match(r'^\s*\[', line):
            skip = False
            out.append(line)
        # else: drop the line (still inside the block)
        continue
    out.append(line)
# Also drop the "# Added by ai-r installer" comment line if present
out = [l for l in out if "Added by ai-r installer" not in l]
with open(path, "w", encoding="utf-8") as f:
    f.write("".join(out))
PY
        log "Codex:     removed [mcp_servers.ai-r]"
    else
        log "Codex:     ai-r not present"
    fi
else
    warn "Codex config not found: $file"
fi

# OpenCode (JSONC: remove mcp.ai-r)
file="$HOME/.config/opencode/opencode.jsonc"
if [[ -f "$file" ]] && command -v python3 >/dev/null 2>&1; then
    python3 - "$file" <<'PY'
import json, re, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"(^|\s)//[^\n]*", r"\1", s)
    return s

clean = strip_comments(text)
data = json.loads(clean) if clean.strip() else {}
mcp = data.get("mcp") or {}
if "ai-r" in mcp:
    del mcp["ai-r"]
    if not mcp:
        data.pop("mcp", None)
    out = json.dumps(data, indent=2, ensure_ascii=False)
    m_schema = re.search(r'"\$schema"\s*:\s*"([^"]+)"', text)
    if m_schema and '"$schema"' not in out:
        out = '{\n  "$schema": "%s",\n%s' % (m_schema.group(1), out.split("{\n", 1)[1])
    with open(path, "w", encoding="utf-8") as f:
        f.write(out + ("\n" if not out.endswith("\n") else ""))
    print("removed", file=sys.stderr)
else:
    print("absent", file=sys.stderr)
PY
    log "OpenCode:  removed mcp.ai-r (if present)"
else
    [[ -f "$file" ]] || warn "OpenCode config not found: $file"
    command -v python3 >/dev/null 2>&1 || warn "python3 not found — skipping OpenCode patch"
fi

# Antigravity
file="$HOME/.gemini/antigravity/mcp_config.json"
if [[ -f "$file" ]] && command -v jq >/dev/null 2>&1; then
    if jq -e '.mcpServers."ai-r"' "$file" >/dev/null 2>&1; then
        tmp="$(mktemp)"
        jq 'del(.mcpServers."ai-r")' "$file" > "$tmp"
        mv "$tmp" "$file"
        log "Antigravity: removed mcpServers.ai-r"
    else
        log "Antigravity: ai-r not present"
    fi
else
    [[ -f "$file" ]] || warn "Antigravity config not found: $file"
    command -v jq >/dev/null 2>&1 || warn "jq not found — skipping Antigravity patch"
fi

# --- optionally purge install dir ---
if [[ "$PURGE" == "1" ]]; then
    hdr "Purging install dirs (--purge)"
    for d in /opt/ai-r "$HOME/.local/share/ai-r"; do
        if [[ -d "$d" ]]; then
            if [[ -L "$d" ]]; then
                # dev-mode symlink
                log "removing symlink: $d"
                rm -f "$d"
            else
                if [[ "$d" == /opt/* ]] && [[ -w "$(dirname "$d")" ]] || [[ -w "$d" ]]; then
                    log "removing dir: $d"
                    rm -rf "$d"
                elif command -v sudo >/dev/null 2>&1; then
                    log "removing dir (sudo): $d"
                    sudo rm -rf "$d"
                else
                    warn "cannot remove $d (no write access, no sudo)"
                fi
            fi
        else
            log "$d: not present"
        fi
    done
else
    log "(run with --purge to also remove the install dir)"
fi

hdr "Uninstall complete"
log "Source repo preserved at: $REPO_DIR"
log "Reinstall with:  bash $REPO_DIR/install.sh"
