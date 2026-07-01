# mining-manager

RTX 4080 auto-mining daemon. Mines on NiceHash whenever the GPU is idle, pauses the moment anything else uses it (Plex/Jellyfin transcoding, CUDA apps, etc.), and keeps you updated on Discord.

## Features

- **Auto-detects GPU usage** — checks NVENC encoder sessions and any CUDA compute process; no process name allowlists
- **NiceHash + lolMiner** — kHeavyHash (Kaspa) via NiceHash stratum, paid in BTC
- **Optimised OC profile** — ~153W actual draw, 1270 MH/s (Kryptex-verified)
- **Discord notifications** — start/stop events, twice-daily status reports, profitability alerts
- **Home Assistant sensors** — power draw, earnings, electricity cost, session time
- **Daily profitability check** — auto-stops and alerts if electricity cost exceeds revenue

---

## Quick start

**Prerequisites:** Linux x86_64, Nvidia drivers with `nvidia-smi`, `python3`, `pip3`, `curl`

```bash
git clone https://github.com/justbest23/mining-manager.git
cd mining-manager
sudo ./setup.sh
```

`setup.sh` will:
1. Download the latest lolMiner binary into `bin/`
2. Install Python dependencies
3. Create `.env` from `.env.example`
4. Install and enable the systemd service

Then fill in the three required values:

```bash
nano .env
```

| Variable | Where to find it |
|----------|-----------------|
| `NICEHASH_BTC_ADDRESS` | NiceHash → Mining → Mining Address |
| `DISCORD_WEBHOOK_URL` | Discord channel → Edit → Integrations → Webhooks → New Webhook |
| `HA_TOKEN` | Home Assistant → Profile → Long-Lived Access Tokens → Create Token |

Then start it:

```bash
sudo systemctl start mining-manager
journalctl -u mining-manager -f
```

---

## How it works

```
GPU idle for 60s → apply OC → start lolMiner → mine on NiceHash
GPU gets used    → stop lolMiner → reset clocks → wait for idle
```

The daemon polls every 30 seconds. It checks:
1. **NVENC/NVDEC encoder sessions** — catches Plex and Jellyfin hardware transcoding
2. **CUDA compute processes** — catches any GPU compute app (filters out lolMiner itself)
3. **Raw GPU utilisation** — fallback threshold check when no miner is running

A configurable grace period (default 60s idle before starting, 15s busy before stopping) prevents thrashing.

---

## Discord

You need a **webhook URL** from a Discord channel. In Discord:
1. Right-click the channel → **Edit Channel**
2. **Integrations** → **Webhooks** → **New Webhook**
3. Copy the URL into `.env` as `DISCORD_WEBHOOK_URL`

Notifications sent:
- ⛏️ **Mining started** (with timestamp)
- ⏸️ **Mining paused** (reason, duration, estimated earnings)
- 📊 **Status report** × 2/day (configurable, default 08:00 and 20:00)
- 🚨 **Unprofitable alert** — auto-stops mining and tells you why
- ✅ **Profitability restored** — mining will auto-resume

---

## Home Assistant sensors

Sensors are created automatically on first run via the HA REST API — no configuration needed in HA.

| Sensor | Description |
|--------|-------------|
| `sensor.mining_status` | `mining` / `idle` / `paused` |
| `sensor.mining_gpu_power_w` | Current GPU power draw (W) |
| `sensor.mining_kwh_today` | Energy consumed today (kWh) |
| `sensor.mining_elec_cost_zar` | Electricity cost today (ZAR) |
| `sensor.mining_net_profit_zar` | Estimated net profit today (ZAR) |
| `sensor.mining_session_minutes` | Current session duration (min) |
| `sensor.mining_worker` | Worker name + status attributes |

### Dashboard card

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

---

## Configuration reference

All settings live in `.env`. Defaults are sane — the only required fields are marked above.

```bash
# Check interval and grace periods
CHECK_INTERVAL=30          # seconds between GPU state polls
IDLE_GRACE_PERIOD=60       # seconds idle before mining starts
BUSY_GRACE_PERIOD=15       # seconds busy before mining stops

# Electricity (update when Tshwane tariffs change, usually 1 July)
ELECTRICITY_RATE_ZAR=4.09  # Block 4 prepaid rate
USD_ZAR_RATE=16.52

# Report schedule (24h HH:MM, comma-separated)
REPORT_TIMES=08:00,20:00
PROFITABILITY_CHECK_TIME=06:00
```

---

## OC profile

Applied via lolMiner flags — no separate `nvidia-settings` or `nvidia-smi` OC commands needed.

| Parameter | Value |
|-----------|-------|
| Core clock | 2205 MHz (locked) |
| Memory clock | 810 MHz (locked) |
| Power limit set | 350W |
| Actual draw | ~153W |
| kHeavyHash hashrate | ~1270 MH/s |
| Efficiency | 8.3 MH/W |

Source: [Kryptex community profile](https://www.kryptex.com/en/overclocking/nvidia-rtx-4080-micron-medium-overclock-3), 96% approval.
Clocks are reset to stock automatically when mining stops.

---

## Updating lolMiner

```bash
rm bin/lolMiner
sudo ./setup.sh
```

---

## Troubleshooting

**Miner doesn't start**
```bash
journalctl -u mining-manager -f
```
Check that `LOLMINER_PATH` in `.env` points to an executable binary.

**GPU clocks not resetting**
The service runs as root. Check `nvidia-persistenced` is running:
```bash
systemctl status nvidia-persistenced
```

**Discord not working**
Test your webhook:
```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"content":"test"}' \
  "$DISCORD_WEBHOOK_URL"
```

**HA sensors not appearing**
Test your token:
```bash
curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/"
```
Sensors appear automatically after the first update cycle (~60s after start).
