#!/usr/bin/env python3
"""
configure_rainbowminer.py — write RainbowMiner config files from .env

Run once after setup.sh to pre-configure RainbowMiner without the interactive
wizard. Safe to re-run — regenerates config files from current .env values.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

def _env(key, default=None):
    val = os.getenv(key, default)
    if val is None:
        print(f"[error] Missing required .env value: {key}", file=sys.stderr)
        sys.exit(1)
    return val

RAINBOWMINER_DIR  = Path(_env("RAINBOWMINER_DIR", "./rainbowminer")).resolve()
WORKER_NAME       = _env("WORKER_NAME", "trog-4080")
ZPOOL_LTC_ADDRESS = _env("ZPOOL_LTC_ADDRESS")
ELECTRICITY_RATE_ZAR = float(_env("ELECTRICITY_RATE_ZAR", "4.09"))
USD_ZAR_RATE      = float(_env("USD_ZAR_RATE", "16.52"))
RBM_API_PORT      = _env("RAINBOWMINER_API_PORT", "4000")

# MiningPoolHub is optional — skip if credentials are still placeholders
MPH_USERNAME  = os.getenv("MPH_USERNAME", "")
MPH_API_KEY   = os.getenv("MPH_API_KEY", "")
MPH_LTC_ADDRESS = os.getenv("MPH_LTC_ADDRESS", ZPOOL_LTC_ADDRESS)

PLACEHOLDERS = {"your_mph_username", "your_mph_api_key", ""}
MPH_ENABLED = MPH_USERNAME not in PLACEHOLDERS and MPH_API_KEY not in PLACEHOLDERS

CONFIG_DIR = RAINBOWMINER_DIR / "Config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

power_price_usd = round(ELECTRICITY_RATE_ZAR / USD_ZAR_RATE, 6)

active_pools = ("MiningPoolHub,Zpool" if MPH_ENABLED else "Zpool")

# ─── config.json ───────────────────────────────────────────────────────────────

config = {
    "Wallet":                ZPOOL_LTC_ADDRESS,
    "WorkerName":            WORKER_NAME,
    "Username":              MPH_USERNAME if MPH_ENABLED else "",
    "Currency":              "USD",
    "Pools":                 active_pools,
    "PoolBalances":          active_pools,
    "Algorithm":             "",
    "ExcludeAlgorithm":      "",
    "MinerName":             "",
    "ExcludeMinerName":      "",
    "DeviceName":            "GPU",
    "ExcludeDeviceName":     "",
    "PowerPrice":            str(power_price_usd),
    "PowerOffset":           "0",
    "PowerPriceCurrency":    "USD",
    "CheckProfitability":    "0",
    "EnableMiningHeatControl": "0",
    "APIport":               RBM_API_PORT,
    "APIthrottle":           "0",
    "APIauth":               "0",
    "APIuser":               "",
    "APIpassword":           "",
    "UIstyle":               "full",
    "RunMode":               "server",
    "StartPaused":           "0",
    "EnableAutoUpdate":      "1",
    "EnableAutoAlgorithmAdd": "1",
    "Watchdog":              "1",
    "BenchmarkInterval":     "60",
    "MinimumMiningIntervals": "1",
    "DisableExtendInterval": "0",
    "SwitchingPrevention":   "2",
    "PoolStatAverage":       "Minute_10",
    "MaxLogfileDays":        "5",
    "MaxDownloadRetries":    "3",
}

config_path = CONFIG_DIR / "config.json"
config_path.write_text(json.dumps(config, indent=4))
print(f"[✓] Written {config_path}")

# ─── pools.config.json ─────────────────────────────────────────────────────────

pools_config = {}

if MPH_ENABLED:
    pools_config["MiningPoolHub"] = {
        "User":             MPH_USERNAME,
        "API_Key":          MPH_API_KEY,
        "Worker":           WORKER_NAME,
        "Wallets":          {"LTC": MPH_LTC_ADDRESS},
        "Penalty":          "0",
        "Algorithm":        "",
        "ExcludeAlgorithm": "",
        "SSL":              "0",
        "Enable":           "1",
    }

pools_config["Zpool"] = {
    "Wallets":          {"LTC": ZPOOL_LTC_ADDRESS},
    "Worker":           WORKER_NAME,
    "Penalty":          "0",
    "Algorithm":        "",
    "ExcludeAlgorithm": "",
    "SSL":              "0",
    "Enable":           "1",
}

pools_path = CONFIG_DIR / "pools.config.json"
pools_path.write_text(json.dumps(pools_config, indent=4))
print(f"[✓] Written {pools_path}")

# miners.config.json — let RainbowMiner manage its own miner binaries
# (it downloads them automatically; we don't write this file)

# ─── Summary ───────────────────────────────────────────────────────────────────

print()
print("RainbowMiner configured:")
if MPH_ENABLED:
    print(f"  Pools  : MiningPoolHub ({MPH_USERNAME}) + Zpool")
else:
    print(f"  Pools  : Zpool only (add MPH_USERNAME/MPH_API_KEY to .env to enable MiningPoolHub)")
print(f"  Payout : LTC → {ZPOOL_LTC_ADDRESS[:12]}…")
print(f"  Power  : ${power_price_usd:.4f}/kWh (R{ELECTRICITY_RATE_ZAR}/kWh ÷ R{USD_ZAR_RATE})")
print(f"  API    : http://localhost:{RBM_API_PORT}")
print()
print("Next: sudo systemctl start mining-manager")
