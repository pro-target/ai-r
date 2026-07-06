#!/usr/bin/env bash
# ai-r installer
#
# Usage:
#   bash install.sh           # user install to ~/.local/share/ai-r
#   bash install.sh opt       # system-wide install to /opt/ai-r (requires sudo)
#   bash install.sh user      # per-user install to ~/.local/share/ai-r
#   INSTALL_MODE=opt bash install.sh
#
# Idempotent: re-running does not break existing installs and does not duplicate
# config entries. Use ./uninstall.sh to remove.
#
# Environment variables:
#   INSTALL_MODE   opt | user | auto (default, same as user)
#   PYTHON         python interpreter to use (default: newest python3.x >= 3.11 on PATH)
#   AI_R_CMD  override the absolute path of ai-r-mcp to register in
#                  the agent MCP configs (default: ~/.local/bin/ai-r-mcp)
#   AI_R_EXTRAS    optional pip extras, comma-separated (e.g. "tokens" to add
#                  tiktoken for better token estimates in session_stats
#                  with_tokens; "semantic" to add onnxruntime+tokenizers AND
#                  download the local embedding model for sort="semantic";
#                  "http" to add uvicorn for the shared streamable-http MCP
#                  transport (AI_R_MCP_TRANSPORT=http, one server not a
#                  per-agent stdio swarm); default: none — ai-r works without
#                  extras)
#   AI_R_SEMANTIC_MODEL_DIR
#                  where the semantic model files are stored (default:
#                  ~/.cache/ai-r/semantic/multilingual-e5-small)
#   DRY_RUN        if set to 1, the script prints what it would do and exits

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-${INSTALL_MODE:-auto}}"

# ai-r requires Python >= 3.11 (pyproject requires-python). On distros whose
# default python3 is older (e.g. Ubuntu 22.04 ships 3.10), probe for a newer
# interpreter on PATH before giving up. PYTHON env var overrides the probe.
MIN_PY_MINOR=11
find_python() {
    local cand
    for cand in python3 python3.13 python3.12 python3.11; do
        if command -v "$cand" >/dev/null 2>&1 &&
           "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, ${MIN_PY_MINOR}) else 1)" 2>/dev/null; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}
if [[ -z "${PYTHON:-}" ]]; then
    PYTHON="$(find_python)" || PYTHON="python3"
fi
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
        auto) echo "user" ;;
        *) die "unknown mode: $MODE (use: opt | user | auto)" ;;
    esac
}

hdr "ai-r installer"
log "Repo:    $REPO_DIR"
log "Python:  $($PYTHON --version 2>&1)"

if ! "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3, ${MIN_PY_MINOR}) else 1)" 2>/dev/null; then
    die "ai-r requires Python >= 3.${MIN_PY_MINOR}; '$PYTHON' is $($PYTHON --version 2>&1). Install a newer python (e.g. apt install python3.${MIN_PY_MINOR} python3.${MIN_PY_MINOR}-venv) or set PYTHON=/path/to/python3.x"
fi

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
    if [[ "$MODE" == "opt" ]]; then
        die "python3-venv not available; opt mode refuses sudo pip fallback"
    fi
    warn "python3-venv not available — falling back to --break-system-packages"
    log "Strategy: runtime install to user site-packages"
fi

if [[ "$USE_VENV" == "1" ]]; then
    VENV_DIR="$INSTALL_DIR/.venv"
    if [[ ! -d "$VENV_DIR" ]]; then
        log "Creating venv at $VENV_DIR"
        run "$PYTHON" -m venv "$VENV_DIR"
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
# Optional extras (pyproject [project.optional-dependencies]), e.g.
# AI_R_EXTRAS=tokens adds tiktoken for better token estimates; the core
# never requires them.
PIP_INSTALL_TARGET="$REPO_DIR"
if [[ -n "${AI_R_EXTRAS:-}" ]]; then
    PIP_INSTALL_TARGET="${REPO_DIR}[${AI_R_EXTRAS}]"
    log "Extras:  ${AI_R_EXTRAS}"
fi
hdr "Step 3/6: pip install $PIP_INSTALL_TARGET"
PIP_ARGS=(install --quiet "$PIP_INSTALL_TARGET")
if [[ "$USE_VENV" == "0" ]]; then
    PIP_ARGS+=(--break-system-packages)
    if [[ "$USE_SUDO" == "0" ]]; then
        PIP_ARGS+=(--user)
    fi
fi
run $PIP_TARGET "${PIP_ARGS[@]}"
log "pip install: OK"

# --- 4b. optional semantic model (only when AI_R_EXTRAS contains "semantic") ---
# sort="semantic" needs the local embedding model files next to the extra's
# python packages. Downloaded once, idempotent; a failed download only warns
# (ai-r then falls back to BM25 order with an honest notice — never a crash).
if [[ ",${AI_R_EXTRAS:-}," == *",semantic,"* ]]; then
    hdr "Optional: semantic model (intfloat/multilingual-e5-small, int8 ONNX, ~118 MB)"
    SEM_DIR="${AI_R_SEMANTIC_MODEL_DIR:-$HOME/.cache/ai-r/semantic/multilingual-e5-small}"
    SEM_BASE="https://huggingface.co/intfloat/multilingual-e5-small/resolve/main"
    run mkdir -p "$SEM_DIR"
    fetch_model_file() { # <url> <dest>
        local url="$1" dest="$2"
        if [[ -s "$dest" ]]; then
            log "already present: $dest"
            return 0
        fi
        if command -v curl >/dev/null 2>&1; then
            run curl -fL --retry 3 -o "${dest}.part" "$url" && run mv "${dest}.part" "$dest"
        elif command -v wget >/dev/null 2>&1; then
            run wget -q -O "${dest}.part" "$url" && run mv "${dest}.part" "$dest"
        else
            warn "neither curl nor wget found — cannot download the model"
            return 1
        fi
    }
    if fetch_model_file "$SEM_BASE/onnx/model_qint8_avx512_vnni.onnx" "$SEM_DIR/model_qint8_avx512_vnni.onnx" \
       && fetch_model_file "$SEM_BASE/tokenizer.json" "$SEM_DIR/tokenizer.json"; then
        log "semantic model ready: $SEM_DIR"
    else
        warn "semantic model download failed — sort=\"semantic\" will honestly fall back to BM25."
        warn "Manual download into $SEM_DIR :"
        warn "  $SEM_BASE/onnx/model_qint8_avx512_vnni.onnx"
        warn "  $SEM_BASE/tokenizer.json"
    fi
fi

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
