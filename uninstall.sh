#!/usr/bin/env bash
# uninstall.sh — remove everything mining-manager installed
# Run as root: sudo ./uninstall.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="mining-manager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[uninstall]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }

[[ "$EUID" -ne 0 ]] && { echo -e "${RED}[✗]${NC} Please run as root: sudo ./uninstall.sh" >&2; exit 1; }

echo ""
echo -e "${RED}╔══════════════════════════════════════════╗${NC}"
echo -e "${RED}║       mining-manager uninstaller         ║${NC}"
echo -e "${RED}╚══════════════════════════════════════════╝${NC}"
echo ""

# ─── Stop systemd service ─────────────────────────────────────────────────────

info "Stopping systemd service…"
systemctl stop "$SERVICE_NAME" 2>/dev/null && success "Service stopped" || info "Service was not running"
systemctl disable "$SERVICE_NAME" 2>/dev/null && success "Service disabled" || info "Service was not enabled"

if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    success "Service file removed"
fi

# ─── Kill any stray miner processes ──────────────────────────────────────────

info "Killing any stray miner processes…"
pkill -x lolMiner      2>/dev/null && success "lolMiner killed"      || true
pkill -f RainbowMiner  2>/dev/null && success "RainbowMiner killed"  || true
pkill -f "pwsh.*RainbowMiner" 2>/dev/null || true

# ─── Reset GPU clocks ─────────────────────────────────────────────────────────

if command -v nvidia-smi &>/dev/null; then
    info "Resetting GPU clocks to stock…"
    nvidia-smi --reset-gpu-clocks    2>/dev/null || true
    nvidia-smi --reset-memory-clocks 2>/dev/null || true
    nvidia-smi -pm 0                 2>/dev/null || true
    success "GPU clocks reset"
else
    warn "nvidia-smi not found — skipping GPU clock reset"
fi

# ─── Remove downloaded binaries and RainbowMiner ─────────────────────────────

if [[ -f "$REPO_DIR/bin/lolMiner" ]]; then
    rm -f "$REPO_DIR/bin/lolMiner"
    rmdir --ignore-fail-on-non-empty "$REPO_DIR/bin" 2>/dev/null || true
    success "lolMiner binary removed"
fi

if [[ -d "$REPO_DIR/rainbowminer" ]]; then
    read -r -p "Remove RainbowMiner directory ($REPO_DIR/rainbowminer)? [y/N] " REMOVE_RBM
    if [[ "${REMOVE_RBM,,}" == "y" ]]; then
        rm -rf "$REPO_DIR/rainbowminer"
        success "RainbowMiner directory removed"
    else
        warn "Keeping rainbowminer/ (your pool config is preserved there)"
    fi
fi

# ─── Optionally remove PowerShell Core ───────────────────────────────────────

if command -v pwsh &>/dev/null; then
    read -r -p "Remove PowerShell Core (pwsh)? [y/N] " REMOVE_PWSH
    if [[ "${REMOVE_PWSH,,}" == "y" ]]; then
        apt-get remove -y powershell 2>/dev/null && success "PowerShell removed" || warn "Could not remove PowerShell via apt"
    fi
fi

# ─── Optionally remove the repo ───────────────────────────────────────────────

echo ""
read -r -p "Remove the entire repo directory ($REPO_DIR)? [y/N] " REMOVE_REPO
if [[ "${REMOVE_REPO,,}" == "y" ]]; then
    bash -c "sleep 1 && rm -rf '$REPO_DIR'" &
    success "Repo scheduled for removal"
else
    info "Repo kept at $REPO_DIR (.env preserved for reinstall)"
fi

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}[✓] Uninstall complete.${NC}"
echo ""
echo "Removed:"
echo "  • mining-manager systemd service"
echo "  • lolMiner binary (bin/lolMiner)"
echo "  • GPU clocks reset to stock"
echo ""
echo "NOT removed (shared system packages):"
echo "  • Python packages (requests, python-dotenv)"
echo "  • Nvidia drivers"
echo ""
