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

# ─── Stop and kill any running miner processes ─────────────────────────────────

info "Stopping any running lolMiner processes…"
if pkill -x lolMiner 2>/dev/null; then
    success "lolMiner processes killed"
else
    info "No lolMiner processes were running"
fi

# ─── systemd service ───────────────────────────────────────────────────────────

info "Stopping systemd service…"
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    success "Service stopped"
else
    info "Service was not running"
fi

info "Disabling systemd service…"
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    success "Service disabled"
else
    info "Service was not enabled"
fi

if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    success "Service file removed and daemon reloaded"
else
    info "Service file not found (already removed)"
fi

# ─── Reset GPU clocks to stock ─────────────────────────────────────────────────

if command -v nvidia-smi &>/dev/null; then
    info "Resetting GPU clocks to stock…"
    nvidia-smi --reset-gpu-clocks    2>/dev/null && true
    nvidia-smi --reset-memory-clocks 2>/dev/null && true
    nvidia-smi -pm 0                 2>/dev/null && true
    success "GPU clocks reset"
else
    warn "nvidia-smi not found — skipping GPU clock reset"
fi

# ─── lolMiner binary ───────────────────────────────────────────────────────────

if [[ -f "$REPO_DIR/bin/lolMiner" ]]; then
    rm -f "$REPO_DIR/bin/lolMiner"
    rmdir --ignore-fail-on-non-empty "$REPO_DIR/bin" 2>/dev/null || true
    success "lolMiner binary removed"
fi

# ─── Optionally remove the repo itself ─────────────────────────────────────────

echo ""
read -r -p "Remove the entire repo directory ($REPO_DIR)? [y/N] " REMOVE_REPO
if [[ "${REMOVE_REPO,,}" == "y" ]]; then
    # Can't delete the directory we're running from — schedule it
    PARENT="$(dirname "$REPO_DIR")"
    BASENAME="$(basename "$REPO_DIR")"
    bash -c "sleep 1 && rm -rf '$REPO_DIR'" &
    success "Repo directory scheduled for removal"
else
    info "Repo directory kept at $REPO_DIR"
    info "Your .env is preserved there if you want to reinstall later"
fi

# ─── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}[✓] Uninstall complete.${NC}"
echo ""
echo "Removed:"
echo "  • systemd service (mining-manager.service)"
echo "  • lolMiner binary (bin/lolMiner)"
echo "  • GPU clocks reset to stock"
echo ""
echo "NOT removed (system-level, shared with other tools):"
echo "  • Python packages (requests, python-dotenv)"
echo "  • Nvidia drivers"
echo ""
