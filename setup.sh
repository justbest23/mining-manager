#!/usr/bin/env bash
# setup.sh — one-shot setup for mining-manager
# Run as root: sudo ./setup.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$REPO_DIR/bin"
SERVICE_SRC="$REPO_DIR/systemd/mining-manager.service"
SERVICE_DST="/etc/systemd/system/mining-manager.service"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[setup]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
die()     { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ─── Checks ────────────────────────────────────────────────────────────────────

[[ "$EUID" -ne 0 ]] && die "Please run as root: sudo ./setup.sh"
[[ "$(uname -s)" != "Linux" ]] && die "Linux only"
[[ "$(uname -m)" != "x86_64" ]] && die "x86_64 only (lolMiner Lin64)"

command -v nvidia-smi &>/dev/null || die "nvidia-smi not found. Install Nvidia drivers first."
command -v python3    &>/dev/null || die "python3 not found. Install with: apt install python3"
command -v pip3       &>/dev/null || die "pip3 not found. Install with: apt install python3-pip"
command -v curl       &>/dev/null || die "curl not found. Install with: apt install curl"

info "Nvidia GPU detected:"
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | sed 's/^/  /'

# ─── lolMiner ──────────────────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"

if [[ -x "$BIN_DIR/lolMiner" ]]; then
    EXISTING_VER="$("$BIN_DIR/lolMiner" --version 2>/dev/null | grep -oP '\d+\.\d+\w*' | head -1 || echo unknown)"
    warn "lolMiner already installed (version $EXISTING_VER). Skipping download."
    warn "Delete $BIN_DIR/lolMiner and re-run to force update."
else
    info "Fetching latest lolMiner release info…"
    RELEASE_JSON="$(curl -sf https://api.github.com/repos/Lolliedieb/lolMiner-releases/releases/latest)"
    VERSION="$(echo "$RELEASE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")"
    ASSET_URL="$(echo "$RELEASE_JSON" | python3 -c "
import json, sys
assets = json.load(sys.stdin)['assets']
url = next(a['browser_download_url'] for a in assets if 'Lin64' in a['name'] and a['name'].endswith('.tar.gz'))
print(url)
")"
    info "Downloading lolMiner v$VERSION from GitHub…"
    TMPDIR_LM="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_LM"' EXIT

    curl -L --progress-bar "$ASSET_URL" -o "$TMPDIR_LM/lolMiner.tar.gz"
    tar -xzf "$TMPDIR_LM/lolMiner.tar.gz" -C "$TMPDIR_LM"
    BINARY="$(find "$TMPDIR_LM" -name lolMiner -type f | head -1)"
    [[ -z "$BINARY" ]] && die "lolMiner binary not found in archive"
    cp "$BINARY" "$BIN_DIR/lolMiner"
    chmod +x "$BIN_DIR/lolMiner"
    success "lolMiner v$VERSION installed to $BIN_DIR/lolMiner"
fi

# ─── Python dependencies ───────────────────────────────────────────────────────

info "Installing Python dependencies…"
pip3 install -q -r "$REPO_DIR/requirements.txt"
success "Python dependencies installed"

# ─── .env ──────────────────────────────────────────────────────────────────────

ENV_FILE="$REPO_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists — not overwriting. Edit it manually if needed."
else
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    # Set the lolMiner path to the local bin
    sed -i "s|LOLMINER_PATH=.*|LOLMINER_PATH=$BIN_DIR/lolMiner|" "$ENV_FILE"
    success ".env created from .env.example"
    echo ""
    echo -e "${YELLOW}  You must now edit .env and fill in:${NC}"
    echo "    NICEHASH_BTC_ADDRESS  — your BTC address from NiceHash"
    echo "    DISCORD_WEBHOOK_URL   — from Discord channel → Integrations → Webhooks"
    echo "    HA_TOKEN              — from Home Assistant → Profile → Long-Lived Access Tokens"
    echo ""
fi

# ─── systemd service ───────────────────────────────────────────────────────────

if [[ -f "$SERVICE_DST" ]]; then
    warn "systemd service already installed. Reloading…"
else
    info "Installing systemd service…"
fi

# Patch WorkingDirectory and ExecStart to absolute paths
TMP_SERVICE="$(mktemp)"
sed \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
    -e "s|ExecStart=.*|ExecStart=/usr/bin/python3 $REPO_DIR/mining_manager.py|" \
    "$SERVICE_SRC" > "$TMP_SERVICE"
cp "$TMP_SERVICE" "$SERVICE_DST"
rm -f "$TMP_SERVICE"

systemctl daemon-reload
systemctl enable mining-manager
success "systemd service installed and enabled"

# ─── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       mining-manager setup complete      ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""

if grep -q "your_btc_address_here" "$ENV_FILE" 2>/dev/null; then
    echo -e "${YELLOW}Next steps:${NC}"
    echo "  1. Edit .env:  nano $ENV_FILE"
    echo "  2. Start:      systemctl start mining-manager"
    echo "  3. Watch logs: journalctl -u mining-manager -f"
else
    echo -e "${GREEN}Looks like .env is already configured.${NC}"
    echo "  Start:      systemctl start mining-manager"
    echo "  Watch logs: journalctl -u mining-manager -f"
fi
echo ""
