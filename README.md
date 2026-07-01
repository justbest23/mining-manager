# mining-manager

RTX 4080 auto-mining daemon for Trog's server.

- Mines on **NiceHash** (lolMiner → kHeavyHash stratum) when the GPU is idle
- Detects any GPU usage (Plex/Jellyfin NVENC, any CUDA process) and pauses automatically
- Applies optimised OC profile (~153W actual draw, 1270 MH/s on Kaspa)
- Sends **Discord** notifications: start/stop events, twice-daily reports, profitability alerts
- Pushes **Home Assistant** sensors every 60s
- Daily profitability check — auto-stops if net profit ≤ $0

---

## Prerequisites

### 1. Nvidia drivers with nvidia-smi

```bash
nvidia-smi  # should show your RTX 4080
```

### 2. lolMiner

Download the latest release from https://github.com/Lolliedieb/lolMiner-releases

```bash
mkdir -p /opt/lolMiner
tar -xf lolMiner_*.tar.gz -C /opt/lolMiner --strip-components=1
chmod +x /opt/lolMiner/lolMiner
```

### 3. Python dependencies

```bash
cd /home/troggoman/mining-manager
pip3 install -r requirements.txt
```

---

## Setup

### 1. Copy and fill in .env

```bash
cp .env.example .env
nano .env
```

Required fields:
- `NICEHASH_BTC_ADDRESS` — your BTC address from NiceHash (Settings → Withdrawal)
- `DISCORD_WEBHOOK_URL` — from Discord channel → Integrations → Webhooks
- `HA_TOKEN` — Home Assistant → Profile → Long-Lived Access Tokens → Create Token

Optional but useful:
- `JELLYFIN_API_KEY` — Jellyfin dashboard → API keys (just for richer stop-reason labels)

### 2. NiceHash account

1. Create account at nicehash.com
2. Go to **Mining → Mining Address** — copy your BTC deposit address
3. Set minimum withdrawal threshold to something sensible (0.001 BTC)
4. Payouts go directly to Luno via BTC deposit address

### 3. Install systemd service

```bash
cp systemd/mining-manager.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable mining-manager
systemctl start mining-manager
```

Check logs:
```bash
journalctl -u mining-manager -f
```

---

## Home Assistant Dashboard Card

Add this to any dashboard YAML:

```yaml
type: entities
title: Mining Manager
entities:
  - entity: sensor.mining_status
    name: Status
    icon: mdi:pickaxe
  - entity: sensor.mining_gpu_power_w
    name: GPU Power
  - entity: sensor.mining_kwh_today
    name: kWh Today
  - entity: sensor.mining_elec_cost_zar
    name: Electricity Cost
  - entity: sensor.mining_net_profit_zar
    name: Net Profit
  - entity: sensor.mining_session_minutes
    name: Session Duration
```

Or use a **Glance card** for a compact view:

```yaml
type: glance
title: Mining
entities:
  - sensor.mining_status
  - sensor.mining_gpu_power_w
  - sensor.mining_net_profit_zar
  - sensor.mining_session_minutes
```

---

## Overclock Profile

Applied automatically via lolMiner flags. No separate nvidia-settings needed.

| Parameter | Value | Note |
|-----------|-------|------|
| Core clock | 2205 MHz | Locked |
| Memory clock | 810 MHz | Locked (compute-bound algo, low mem is fine) |
| Power limit | 350W set | Actual draw ~153W due to locked clocks |
| Hashrate | ~1270 MH/s | kHeavyHash (Kaspa) |
| Efficiency | 8.3 MH/W | Community-verified, 96% approval on Kryptex |

Clocks are reset to stock automatically when mining stops.

---

## Updating tariff rate

When Tshwane updates prepaid tariffs (usually 1 July each year), update `.env`:

```
ELECTRICITY_RATE_ZAR=4.09   # Update this to new Block 4 rate
USD_ZAR_RATE=16.52           # Update periodically
```

No restart needed — values are read at runtime for each report/check.

---

## Troubleshooting

**Miner doesn't start**
- Check `journalctl -u mining-manager -f`
- Verify `LOLMINER_PATH` points to the binary
- Run `nvidia-smi` to confirm GPU is visible

**GPU not resetting clocks after stop**
- Service runs as root; `nvidia-smi --reset-gpu-clocks` requires root
- Check `nvidia-persistenced` is running: `systemctl status nvidia-persistenced`

**Discord notifications not arriving**
- Test webhook manually: `curl -X POST -H 'Content-Type: application/json' -d '{"content":"test"}' YOUR_WEBHOOK_URL`

**HA sensors not appearing**
- Check HA token is valid: `curl -H "Authorization: Bearer TOKEN" https://homeass.trog.co.za/api/`
- Sensors are created automatically on first push — no config needed in HA
