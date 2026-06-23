#!/usr/bin/env bash
# ai-r installer
#
# Usage:
#   bash install.sh           # auto-detect mode (opt if sudo NOPASSWD, else user)
#   bash install.sh opt       # system-wide install to /opt/ai-r (requires sudo)
#   bash install.sh user      # per-user install to ~/.local/share/ai-r
#   INSTALL_MODE=opt bash install.sh
#
# Idempotent: re-running does not break existing installs and does not duplicate
# config entries. Use ./uninstall.sh to remove.
#
# Environment variables:
#   INSTALL_MODE   opt | user | auto (default)
#   PYTHON         python interpreter to use (default: python3)
#   AI_R_CMD  override the absolute path of ai-r-mcp to register in
#                  the agent MCP configs (default: ~/.local/bin/ai-r-mcp)
#   DRY_RUN        if set to 1, the script prints what it would do and exits

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-${INSTALL_MODE:-auto}}"
PYTHON="${PYTHON:-python3}"
BIN_DIR="${HOME}/.local/bin"
INSTALL_DIR=""
VENV_PYTHON_BIN=""
VENV_MCP_BIN=""
DRY_RUN="${DRY_RUN:-0}"

# Colors (auto-disable when not a TTY)
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    RED='\033[0;31m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BOLD=''; NC=''
fi

log()  { printf "${GREEN}[+]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
die()  { printf "${RED}[x]${NC} %s\n" "$*" >&2; exit 1; }
hdr()  { printf "\n${BOLD}==> %s${NC}\n" "$*"; }

run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        printf "${YELLOW}[dry-run]${NC} %s\n" "$*"
    else
        "$@"
    fi
}

# --- 1. mode detection ---
detect_mode() {
    case "$MODE" in
        opt|user) echo "$MODE" ;;
        auto)
            if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
                echo "opt"
            else
                echo "user"
            fi
            ;;
        *) die "unknown mode: $MODE (use: opt | user | auto)" ;;
    esac
}

hdr "ai-r installer"
log "Repo:    $REPO_DIR"
log "Python:  $($PYTHON --version 2>&1)"

MODE="$(detect_mode)"
if [[ "$MODE" == "opt" ]]; then
    INSTALL_DIR="/opt/ai-r"
    USE_SUDO=1
    log "Mode:    system-wide ($INSTALL_DIR, requires sudo)"
else
    INSTALL_DIR="${HOME}/.local/share/ai-r"
    USE_SUDO=0
    log "Mode:    user-local ($INSTALL_DIR, no sudo)"
fi

# --- 2. create install dir ---
hdr "Step 1/6: prepare $INSTALL_DIR"
if [[ -L "$INSTALL_DIR" ]]; then
    log "Removing existing symlink: $INSTALL_DIR"
    if [[ "$USE_SUDO" == "1" ]]; then sudo rm -f "$INSTALL_DIR"; else rm -f "$INSTALL_DIR"; fi
elif [[ -d "$INSTALL_DIR" ]]; then
    warn "$INSTALL_DIR already exists — will reuse in place (no destructive overwrite)"
else
    log "Creating $INSTALL_DIR"
    if [[ "$USE_SUDO" == "1" ]]; then
        run sudo mkdir -p "$INSTALL_DIR"
        run sudo chown "$(id -un)":"$(id -gn)" "$INSTALL_DIR"
    else
        run mkdir -p "$INSTALL_DIR"
    fi
fi

# --- 3. venv OR break-system-packages fallback ---
hdr "Step 2/6: python environment"
USE_VENV=0
# `python3 -m venv --help` only checks the venv module; creating an actual venv
# requires the `python3-venv` / `python3.X-venv` package (ensurepip). Probe by
# trying to create a throwaway venv.
probe_venv() {
    local tmp_venv
    tmp_venv="$(mktemp -d)/probe-venv"
    if "$PYTHON" -m venv "$tmp_venv" >/dev/null 2>&1; then
        rm -rf "$(dirname "$tmp_venv")"
        return 0
    fi
    rm -rf "$(dirname "$tmp_venv")"
    return 1
}
if probe_venv; then
    USE_VENV=1
    log "Strategy: venv (python3-venv available)"
else
    warn "python3-venv not available — falling back to --break-system-packages"
    log "Strategy: editable install to user site-packages"
fi

if [[ "$USE_VENV" == "1" ]]; then
    VENV_DIR="$INSTALL_DIR/.venv"
    if [[ ! -d "$VENV_DIR" ]]; then
        log "Creating venv at $VENV_DIR"
        if [[ "$USE_SUDO" == "1" ]]; then
            run sudo "$PYTHON" -m venv "$VENV_DIR"
        else
            run "$PYTHON" -m venv "$VENV_DIR"
        fi
    else
        log "Reusing existing venv: $VENV_DIR"
    fi
    VENV_PYTHON_BIN="$VENV_DIR/bin/ai-r"
    VENV_MCP_BIN="$VENV_DIR/bin/ai-r-mcp"
    PIP_TARGET="$VENV_DIR/bin/pip"
else
    if "$PYTHON" -m pip --version >/dev/null 2>&1; then
        PIP_TARGET="$PYTHON -m pip"
    elif command -v pip3 >/dev/null 2>&1 && pip3 --version >/dev/null 2>&1; then
        PIP_TARGET="$(command -v pip3)"
    elif command -v pip >/dev/null 2>&1 && pip --version >/dev/null 2>&1; then
        PIP_TARGET="$(command -v pip)"
    else
        die "pip not found. Install python3-venv (preferred) or python3-pip, then rerun install.sh"
    fi
    if [[ "$USE_SUDO" == "1" ]]; then
        VENV_PYTHON_BIN="/usr/local/bin/ai-r"
        VENV_MCP_BIN="/usr/local/bin/ai-r-mcp"
    else
        VENV_PYTHON_BIN="$HOME/.local/bin/ai-r"
        VENV_MCP_BIN="$HOME/.local/bin/ai-r-mcp"
    fi
fi

# --- 4. pip install ---
hdr "Step 3/6: pip install -e $REPO_DIR[dev]"
PIP_ARGS=(install --quiet -e "$REPO_DIR[dev]")
if [[ "$USE_VENV" == "0" ]]; then
    PIP_ARGS+=(--break-system-packages)
    if [[ "$USE_SUDO" == "0" ]]; then
        PIP_ARGS+=(--user)
    fi
fi
if [[ "$USE_SUDO" == "1" && "$USE_VENV" == "1" ]]; then
    run sudo "$PIP_TARGET" "${PIP_ARGS[@]}"
else
    run $PIP_TARGET "${PIP_ARGS[@]}"
fi
log "pip install: OK"

# --- 5. symlink binaries ---
hdr "Step 4/6: symlink binaries → $BIN_DIR"
run mkdir -p "$BIN_DIR"
if [[ "$USE_VENV" == "1" ]]; then
    # in venv mode, VENV_*_BIN point to actual files inside venv
    if [[ "$DRY_RUN" != "1" ]]; then
        if [[ ! -x "$VENV_PYTHON_BIN" ]]; then
            die "expected entry point missing: $VENV_PYTHON_BIN (pip install failed?)"
        fi
        if [[ ! -x "$VENV_MCP_BIN" ]]; then
            die "expected entry point missing: $VENV_MCP_BIN (pip install failed?)"
        fi
    fi
    run ln -sf "$VENV_PYTHON_BIN" "$BIN_DIR/ai-r"
    run ln -sf "$VENV_MCP_BIN"   "$BIN_DIR/ai-r-mcp"
else
    # in break-system-packages mode, files are placed by pip in BIN_DIR (or /usr/local/bin)
    if [[ "$DRY_RUN" != "1" ]]; then
        for src in "$VENV_PYTHON_BIN" "$VENV_MCP_BIN"; do
            if [[ ! -e "$src" ]]; then
                die "expected entry point missing: $src (pip install failed?)"
            fi
        done
    fi
    # If pip wrote to /usr/local/bin, also expose a user-mode symlink so `which` finds it
    if [[ "$USE_SUDO" == "1" ]]; then
        run ln -sf "$VENV_PYTHON_BIN" "$BIN_DIR/ai-r"
        run ln -sf "$VENV_MCP_BIN"   "$BIN_DIR/ai-r-mcp"
    fi
fi
log "Symlinks:"
log "  $BIN_DIR/ai-r      → $VENV_PYTHON_BIN"
log "  $BIN_DIR/ai-r-mcp  → $VENV_MCP_BIN"

# --- 6. patch 4 agent configs ---
hdr "Step 5/6: patch 4 agent MCP configs"
if [[ -f "$REPO_DIR/install/agent-configs.sh" ]]; then
    AI_R_CMD="${AI_R_CMD:-$BIN_DIR/ai-r-mcp}" \
        run bash "$REPO_DIR/install/agent-configs.sh" \
        || warn "agent-configs.sh returned non-zero (some patches may have failed)"
else
    warn "install/agent-configs.sh not found — skipping agent config patches"
fi

# --- 7. smoke test ---
hdr "Step 6/6: smoke test"
if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] would run: $BIN_DIR/ai-r --version"
    log "[dry-run] would run: $BIN_DIR/ai-r-mcp --version || true"
else
    if "$BIN_DIR/ai-r" --version 2>&1; then
        log "ai-r: OK"
    else
        warn "ai-r --version failed (entry point not on PATH yet?)"
        warn "try:  export PATH=\"$BIN_DIR:\$PATH\""
    fi
    # ai-r-mcp is a stdio JSON-RPC server with no --version/--help,
    # so verify it imports and starts by feeding it EOF (/dev/null) under a
    # timeout. Exit 0 (clean EOF) or 124 (timed out — happily serving) both
    # mean it started; any other exit means an import/startup error.
    if timeout 5 "$BIN_DIR/ai-r-mcp" </dev/null >/dev/null 2>&1; then
        log "ai-r-mcp: importable"
    else
        rc=$?
        if [[ "$rc" -eq 124 ]]; then
            log "ai-r-mcp: importable (started, then timed out as expected)"
        else
            warn "ai-r-mcp: failed to start (exit $rc)"
            warn "try:  export PATH=\"$BIN_DIR:\$PATH\""
        fi
    fi
fi

# --- done ---
hdr "Install complete"
cat <<EOF
${GREEN}✓${NC} ai-r installed in ${BOLD}$MODE${NC} mode

Quick test:
    $BIN_DIR/ai-r list --agent claude

MCP server (for your agents):
    command: $BIN_DIR/ai-r-mcp

If 'ai-r' is not found, add ~/.local/bin to PATH:
    export PATH="\$HOME/.local/bin:\$PATH"

To uninstall:
    bash $REPO_DIR/uninstall.sh
EOF
