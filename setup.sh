#!/usr/bin/env bash
# setup.sh — one-shot setup for mining-manager
# Run as root: sudo ./setup.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$REPO_DIR/bin"
RBM_DIR="$REPO_DIR/rainbowminer"
SERVICE_SRC="$REPO_DIR/systemd/mining-manager.service"
SERVICE_DST="/etc/systemd/system/mining-manager.service"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[setup]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
die()     { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ─── Pre-flight checks ─────────────────────────────────────────────────────────

[[ "$EUID" -ne 0 ]] && die "Please run as root: sudo ./setup.sh"
[[ "$(uname -s)" != "Linux" ]] && die "Linux only"
[[ "$(uname -m)" != "x86_64" ]] && die "x86_64 only"

command -v nvidia-smi &>/dev/null || die "nvidia-smi not found — install Nvidia drivers first"
command -v python3    &>/dev/null || die "python3 not found — apt install python3"
command -v pip3       &>/dev/null || die "pip3 not found — apt install python3-pip"
command -v curl       &>/dev/null || die "curl not found — apt install curl"
command -v unzip      &>/dev/null || die "unzip not found — apt install unzip"

info "Detected GPU:"
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | sed 's/^/  /'
echo ""

# ─── PowerShell Core ──────────────────────────────────────────────────────────

if command -v pwsh &>/dev/null; then
    PWSH_VER="$(pwsh --version 2>/dev/null || echo 'unknown')"
    warn "PowerShell already installed ($PWSH_VER). Skipping."
else
    info "Installing PowerShell Core…"
    # Detect distro
    if grep -qi ubuntu /etc/os-release 2>/dev/null || grep -qi debian /etc/os-release 2>/dev/null; then
        # Add Microsoft repo
        DISTRO_CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
        if [[ -z "$DISTRO_CODENAME" ]]; then
            DISTRO_CODENAME="$(lsb_release -cs 2>/dev/null || echo 'jammy')"
        fi
        curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/repos/microsoft-ubuntu-${DISTRO_CODENAME}-prod ${DISTRO_CODENAME} main" \
            > /etc/apt/sources.list.d/microsoft-prod.list
        apt-get update -qq
        apt-get install -y powershell
    else
        die "Unsupported distro. Install PowerShell Core manually: https://learn.microsoft.com/en-us/powershell/scripting/install/installing-powershell-on-linux"
    fi
    success "PowerShell Core installed ($(pwsh --version))"
fi

# ─── RainbowMiner ─────────────────────────────────────────────────────────────

if [[ -f "$RBM_DIR/RainbowMiner.ps1" ]]; then
    RBM_VER="$(grep -m1 'Version' "$RBM_DIR/RainbowMiner.ps1" 2>/dev/null | grep -oP '\d+\.\d+[\.\d]*' | head -1 || echo 'unknown')"
    warn "RainbowMiner already installed (v$RBM_VER). Skipping download."
    warn "To update: rm -rf rainbowminer/ && sudo ./setup.sh"
else
    info "Fetching latest RainbowMiner release…"
    RBM_JSON="$(curl -sf https://api.github.com/repos/RainbowMiner/RainbowMiner/releases/latest)"
    RBM_VERSION="$(echo "$RBM_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")"
    # Prefer the Linux-specific zip; fall back to generic zip (not Windows)
    RBM_URL="$(echo "$RBM_JSON" | python3 -c "
import json, sys
assets = json.load(sys.stdin)['assets']
urls = [a['browser_download_url'] for a in assets if a['name'].endswith('.zip') and '_win' not in a['name'].lower()]
# Prefer _linux build
linux = [u for u in urls if '_linux' in u.lower()]
print((linux or urls or [''])[0])
" 2>/dev/null || echo "")"

    if [[ -z "$RBM_URL" ]]; then
        RBM_URL="https://github.com/RainbowMiner/RainbowMiner/archive/refs/tags/${RBM_VERSION}.zip"
        info "Using source archive: $RBM_URL"
    fi

    info "Downloading RainbowMiner $RBM_VERSION from $RBM_URL…"
    TMPDIR_RBM="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_RBM"' EXIT

    curl -L --progress-bar "$RBM_URL" -o "$TMPDIR_RBM/RainbowMiner.zip"
    unzip -q "$TMPDIR_RBM/RainbowMiner.zip" -d "$TMPDIR_RBM/extracted"

    # Some releases wrap files in a versioned subdirectory; others extract flat
    SUBDIR="$(find "$TMPDIR_RBM/extracted" -maxdepth 1 -type d | grep -v "^$TMPDIR_RBM/extracted$" | head -1)"
    if [[ -f "$SUBDIR/RainbowMiner.ps1" ]]; then
        RBM_EXTRACTED="$SUBDIR"
    elif [[ -f "$TMPDIR_RBM/extracted/RainbowMiner.ps1" ]]; then
        RBM_EXTRACTED="$TMPDIR_RBM/extracted"
    else
        die "Could not find RainbowMiner.ps1 in extracted archive. Contents: $(ls "$TMPDIR_RBM/extracted/")"
    fi

    mkdir -p "$RBM_DIR"
    # rsync-style copy: preserve existing Config/ (user settings) if present
    cp -rn "$RBM_EXTRACTED"/. "$RBM_DIR/"
    success "RainbowMiner $RBM_VERSION installed to $RBM_DIR"
fi

# ─── lolMiner (used by RainbowMiner as a backend miner) ───────────────────────

mkdir -p "$BIN_DIR"

if [[ -x "$BIN_DIR/lolMiner" ]]; then
    warn "lolMiner already installed. Skipping."
else
    info "Fetching latest lolMiner release…"
    LM_JSON="$(curl -sf https://api.github.com/repos/Lolliedieb/lolMiner-releases/releases/latest)"
    LM_VERSION="$(echo "$LM_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")"
    LM_URL="$(echo "$LM_JSON" | python3 -c "
import json, sys
assets = json.load(sys.stdin)['assets']
url = next(a['browser_download_url'] for a in assets if 'Lin64' in a['name'] and a['name'].endswith('.tar.gz'))
print(url)
")"
    info "Downloading lolMiner $LM_VERSION…"
    TMPDIR_LM="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_LM"' EXIT
    curl -L --progress-bar "$LM_URL" -o "$TMPDIR_LM/lolMiner.tar.gz"
    tar -xzf "$TMPDIR_LM/lolMiner.tar.gz" -C "$TMPDIR_LM"
    BINARY="$(find "$TMPDIR_LM" -name lolMiner -type f | head -1)"
    [[ -z "$BINARY" ]] && die "lolMiner binary not found in archive"
    cp "$BINARY" "$BIN_DIR/lolMiner"
    chmod +x "$BIN_DIR/lolMiner"
    success "lolMiner $LM_VERSION installed to $BIN_DIR/lolMiner"
fi

# ─── Python dependencies ───────────────────────────────────────────────────────

info "Installing Python dependencies…"
pip3 install -q -r "$REPO_DIR/requirements.txt"
success "Python dependencies installed"

# ─── .env ─────────────────────────────────────────────────────────────────────

ENV_FILE="$REPO_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists — not overwriting."
else
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    sed -i "s|RAINBOWMINER_DIR=.*|RAINBOWMINER_DIR=$RBM_DIR|" "$ENV_FILE"
    success ".env created — fill in your credentials now"
fi

# ─── systemd service ──────────────────────────────────────────────────────────

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

# ─── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         mining-manager setup complete                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

NEEDS_CREDS=false
grep -q "your_mph_username\|your_luno_ltc" "$ENV_FILE" 2>/dev/null && NEEDS_CREDS=true

if $NEEDS_CREDS; then
    echo -e "${YELLOW}Step 1 — Fill in your credentials:${NC}"
    echo "  nano $ENV_FILE"
    echo ""
    echo -e "${YELLOW}Step 2 — Write RainbowMiner config from .env:${NC}"
    echo "  python3 $REPO_DIR/configure_rainbowminer.py"
    echo ""
    echo -e "${YELLOW}Step 3 — Start mining:${NC}"
    echo "  systemctl start mining-manager"
    echo "  journalctl -u mining-manager -f"
else
    echo -e "${YELLOW}Step 1 — Write RainbowMiner config from .env:${NC}"
    echo "  python3 $REPO_DIR/configure_rainbowminer.py"
    echo ""
    echo -e "${YELLOW}Step 2 — Start mining:${NC}"
    echo "  systemctl start mining-manager"
    echo "  journalctl -u mining-manager -f"
fi
echo ""
