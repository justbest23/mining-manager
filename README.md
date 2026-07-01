# mining-manager

RTX 4080 auto-mining daemon. Runs **RainbowMiner** (profit-switching across MiningPoolHub and Zpool) whenever the GPU is idle, pauses the moment anything else uses it (Plex/Jellyfin NVENC, any CUDA app), and keeps you updated on Discord and Home Assistant.

**Payout:** LTC → your Luno deposit address  
**Pools:** MiningPoolHub (primary, 1.1% fee) + Zpool (comparison, 2% fee) — RainbowMiner picks whichever pays more per algorithm in real time

---

## Quick start

**Prerequisites:** Linux x86_64, Nvidia drivers + `nvidia-smi`, `python3`, `pip3`, `curl`, `unzip`

```bash
git clone https://github.com/justbest23/mining-manager.git
cd mining-manager
sudo ./setup.sh
```

`setup.sh` installs: PowerShell Core, RainbowMiner, lolMiner (backend), Python deps, systemd service.

### After setup

**1. Fill in `.env`**
```bash
nano .env
```

Required values:

| Variable | Where to get it |
|----------|----------------|
| `MPH_USERNAME` | Your MiningPoolHub account username |
| `MPH_API_KEY` | MiningPoolHub → Account → Edit Account → API Key |
| `MPH_LTC_ADDRESS` | Your Luno LTC deposit address |
| `ZPOOL_LTC_ADDRESS` | Same Luno LTC deposit address |
| `DISCORD_WEBHOOK_URL` | Discord channel → Edit → Integrations → Webhooks → New Webhook |
| `HA_TOKEN` | Home Assistant → Profile → Long-Lived Access Tokens |

**2. Write RainbowMiner config from .env**
```bash
python3 configure_rainbowminer.py
```

**3. Start**
```bash
sudo systemctl start mining-manager
journalctl -u mining-manager -f
```

---

## How it works

```
GPU idle for 60s  →  apply OC  →  start RainbowMiner  →  mine on best pool/algo
GPU gets used     →  stop RainbowMiner  →  reset clocks  →  wait for idle
```

**GPU idle detection** (generic — no process name lists):
1. NVENC/NVDEC encoder sessions > 0 → transcoding (Plex, Jellyfin)
2. Any CUDA compute process other than RainbowMiner's child miners → GPU in use
3. Raw GPU utilisation above threshold (fallback, only when no miner running)

**Profit switching** is handled by RainbowMiner internally. It benchmarks each algorithm, queries both pool APIs every 10 minutes, and switches to whichever algorithm/pool combination earns the most after electricity cost.

**OC profile** is applied via `nvidia-smi` on start and reset on stop:

| Setting | Value |
|---------|-------|
| Core clock (locked) | 2205 MHz |
| Power limit | 350W (actual draw ~153W at locked clocks) |
| Efficiency | ~8.3 MH/W on kHeavyHash |

---

## Pool setup

### MiningPoolHub
1. Create account at [miningpoolhub.com](https://miningpoolhub.com)
2. Go to **Account → Edit Account → Auto Exchange** → set target coin to **LTC**
3. Set your Luno LTC deposit address as the payout address
4. Copy your API key from the account page

### Zpool
No registration. Your LTC address is your username. Payouts go directly to your Luno LTC address.

---

## Discord

Create a webhook in your mining updates channel:
1. Right-click channel → **Edit Channel → Integrations → Webhooks → New Webhook**
2. Copy URL → paste into `.env` as `DISCORD_WEBHOOK_URL`

Notifications:
- ⛏️ Mining started (worker, algorithm, pool)
- ⏸️ Mining paused (reason, duration, estimated earnings)
- 📊 Status report ×2/day (hashrate, kWh, cost, net profit)
- 🚨 Unprofitable alert (auto-stop)
- ✅ Profitability restored (auto-resume)

---

## Home Assistant sensors

Auto-created on first run — no HA configuration needed.

| Sensor | Description |
|--------|-------------|
| `sensor.mining_status` | `mining` / `idle` / `paused` |
| `sensor.mining_gpu_power_w` | GPU power draw (W) |
| `sensor.mining_kwh_today` | Energy today (kWh) |
| `sensor.mining_elec_cost_zar` | Electricity cost today (ZAR) |
| `sensor.mining_net_profit_zar` | Net profit today (ZAR) |
| `sensor.mining_session_minutes` | Current session duration |
| `sensor.mining_algorithm` | Current algorithm/pool |
| `sensor.mining_hashrate` | Current hashrate |

### Dashboard card

```yaml
type: entities
title: Mining
entities:
  - entity: sensor.mining_status
  - entity: sensor.mining_algorithm
  - entity: sensor.mining_hashrate
  - entity: sensor.mining_gpu_power_w
  - entity: sensor.mining_kwh_today
  - entity: sensor.mining_elec_cost_zar
  - entity: sensor.mining_net_profit_zar
  - entity: sensor.mining_session_minutes
```

---

## Updating RainbowMiner

```bash
rm -rf rainbowminer/
sudo ./setup.sh        # re-downloads latest release
python3 configure_rainbowminer.py   # rewrites config from .env
sudo systemctl restart mining-manager
```

## Updating tariff rate

Edit `.env` → change `ELECTRICITY_RATE_ZAR` → re-run `configure_rainbowminer.py` (updates the PowerPrice in RainbowMiner config too) → restart service.

---

## Uninstall

```bash
sudo ./uninstall.sh
```

Stops service, kills miners, resets GPU clocks, removes binaries. Optionally removes RainbowMiner, PowerShell, and the repo directory.

---

## Troubleshooting

**Service won't start**
```bash
journalctl -u mining-manager -f
```

**RainbowMiner not found / not configured**
```bash
python3 configure_rainbowminer.py   # regenerate config from .env
```

**GPU not resetting clocks**
```bash
systemctl status nvidia-persistenced
nvidia-smi --reset-gpu-clocks
```

**Discord not working**
```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"content":"test"}' "$DISCORD_WEBHOOK_URL"
```

**HA sensors not appearing** — wait 60s after start; check token:
```bash
curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/"
```
