#!/usr/bin/env python3
"""
mining_manager.py — RTX 4080 auto-mining daemon

Mines on NiceHash (lolMiner → kHeavyHash stratum) whenever the GPU is idle.
Sends Discord notifications on state changes and scheduled reports.
Pushes sensors to Home Assistant.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# ─── Config ────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

def _env(key, default=None, cast=str):
    val = os.getenv(key, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return cast(val) if cast else val

NICEHASH_BTC_ADDRESS   = _env("NICEHASH_BTC_ADDRESS")
NICEHASH_WORKER        = _env("NICEHASH_WORKER", "trog-4080")
NICEHASH_REGION        = _env("NICEHASH_REGION", "eu")
LOLMINER_PATH          = _env("LOLMINER_PATH", "/opt/lolMiner/lolMiner")
LOLMINER_API_PORT      = _env("LOLMINER_API_PORT", "42000", int)
GPU_CORE_CLOCK         = _env("GPU_CORE_CLOCK", "2205")
GPU_MEM_CLOCK          = _env("GPU_MEM_CLOCK", "810")
GPU_POWER_LIMIT        = _env("GPU_POWER_LIMIT", "350")
CHECK_INTERVAL         = _env("CHECK_INTERVAL", "30", int)
GPU_UTIL_THRESHOLD     = _env("GPU_UTIL_THRESHOLD", "15", int)
IDLE_GRACE_PERIOD      = _env("IDLE_GRACE_PERIOD", "60", int)
BUSY_GRACE_PERIOD      = _env("BUSY_GRACE_PERIOD", "15", int)
ELECTRICITY_RATE_ZAR   = _env("ELECTRICITY_RATE_ZAR", "4.09", float)
USD_ZAR_RATE           = _env("USD_ZAR_RATE", "16.52", float)
DISCORD_WEBHOOK_URL    = _env("DISCORD_WEBHOOK_URL")
REPORT_TIMES           = _env("REPORT_TIMES", "08:00,20:00").split(",")
PROFITABILITY_CHECK_TIME = _env("PROFITABILITY_CHECK_TIME", "06:00")
HA_URL                 = _env("HA_URL", "https://homeass.trog.co.za").rstrip("/")
HA_TOKEN               = _env("HA_TOKEN")
HA_UPDATE_INTERVAL     = _env("HA_UPDATE_INTERVAL", "60", int)
JELLYFIN_URL           = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY       = os.getenv("JELLYFIN_API_KEY", "")

NICEHASH_STRATUM = f"stratum+tcp://kheavyhash.{NICEHASH_REGION}.nicehash.com:3381"
NICEHASH_USER    = f"{NICEHASH_BTC_ADDRESS}.{NICEHASH_WORKER}"

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "mining_manager.log"),
    ],
)
log = logging.getLogger(__name__)

# ─── State ─────────────────────────────────────────────────────────────────────

miner_proc = None  # type: subprocess.Popen | None
mining_start_time: datetime | None = None
mining_paused_reason: str = ""
profitability_ok: bool = True

idle_since: datetime | None = None   # when GPU first became idle
busy_since: datetime | None = None   # when GPU first became busy

# Accumulated stats for reports
session_kwh: float = 0.0             # kWh consumed this session
total_kwh_today: float = 0.0         # kWh consumed today (resets midnight)
last_power_sample_time: datetime | None = None

shutdown_event = threading.Event()

# ─── GPU detection ─────────────────────────────────────────────────────────────

def _nvidia_smi(*args) -> str:
    result = subprocess.run(
        ["nvidia-smi", *args],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip()


def get_gpu_state() -> tuple[str, str]:
    """
    Returns (state, detail).
    state: 'idle' | 'busy'
    detail: human-readable reason if busy, empty string if idle
    """
    # 1. Encoder/decoder sessions (Plex, Jellyfin NVENC transcoding)
    try:
        raw = _nvidia_smi(
            "--query-gpu=encoder.stats.sessionCount,decoder.stats.sessionCount",
            "--format=csv,noheader,nounits"
        )
        enc, dec = (int(x.strip()) for x in raw.split(","))
        if enc > 0 or dec > 0:
            parts = []
            if enc: parts.append(f"{enc} encoder session{'s' if enc>1 else ''}")
            if dec: parts.append(f"{dec} decoder session{'s' if dec>1 else ''}")
            return "busy", f"Hardware transcoding: {', '.join(parts)}"
    except Exception as e:
        log.warning(f"Could not query encoder sessions: {e}")

    # 2. Compute processes (CUDA apps) — exclude our own miner
    try:
        raw = _nvidia_smi(
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader"
        )
        other_procs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            pid_str, name = parts[0], parts[1] if len(parts) > 1 else "unknown"
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if miner_proc and pid == miner_proc.pid:
                continue
            other_procs.append(f"{name} (pid {pid})")
        if other_procs:
            return "busy", f"GPU compute: {', '.join(other_procs)}"
    except Exception as e:
        log.warning(f"Could not query compute apps: {e}")

    # 3. Raw GPU utilisation above threshold (catches things nvidia-smi misses)
    #    Only checked when miner is not running to avoid false positives.
    if miner_proc is None:
        try:
            raw = _nvidia_smi(
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits"
            )
            util = int(raw.strip())
            if util >= GPU_UTIL_THRESHOLD:
                return "busy", f"GPU utilisation {util}% (threshold {GPU_UTIL_THRESHOLD}%)"
        except Exception as e:
            log.warning(f"Could not query GPU utilisation: {e}")

    return "idle", ""


def get_gpu_power_watts() -> float | None:
    try:
        raw = _nvidia_smi("--query-gpu=power.draw", "--format=csv,noheader,nounits")
        return float(raw.strip())
    except Exception:
        return None

# ─── GPU Clocks restore ────────────────────────────────────────────────────────
# OC is applied via lolMiner flags; we only need to reset on stop.

def restore_gpu_clocks():
    try:
        subprocess.run(["nvidia-smi", "--reset-gpu-clocks"], check=True, capture_output=True)
        subprocess.run(["nvidia-smi", "--reset-memory-clocks"], check=True, capture_output=True)
        log.info("GPU clocks reset to stock")
    except subprocess.CalledProcessError as e:
        log.warning(f"Could not reset GPU clocks: {e}")

# ─── Mining control ────────────────────────────────────────────────────────────

def start_mining():
    global miner_proc, mining_start_time, session_kwh, last_power_sample_time

    if miner_proc is not None:
        return

    if not profitability_ok:
        log.info("Mining suppressed — profitability check failed")
        return

    cmd = [
        LOLMINER_PATH,
        "--algo", "KASPA",
        "--pool", NICEHASH_STRATUM,
        "--user", NICEHASH_USER,
        "--pass", "x",
        "--cclk", GPU_CORE_CLOCK,
        "--mclk", GPU_MEM_CLOCK,
        "--pl", GPU_POWER_LIMIT,
        "--apiport", str(LOLMINER_API_PORT),
        "--watchdog", "0",   # disable lolMiner's own watchdog; we manage the process
    ]

    log.info(f"Starting miner: {' '.join(cmd)}")
    try:
        subprocess.run(["nvidia-smi", "-pm", "1"], check=True, capture_output=True)
        miner_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        mining_start_time = datetime.now()
        session_kwh = 0.0
        last_power_sample_time = mining_start_time
        log.info(f"Miner started (pid {miner_proc.pid})")
        notify_mining_started()
        update_ha_sensors()
    except FileNotFoundError:
        log.error(f"lolMiner not found at {LOLMINER_PATH}. Check LOLMINER_PATH in .env")
    except Exception as e:
        log.error(f"Failed to start miner: {e}")
        miner_proc = None


def stop_mining(reason: str = "GPU in use"):
    global miner_proc, mining_paused_reason

    if miner_proc is None:
        return

    log.info(f"Stopping miner (reason: {reason})")
    try:
        miner_proc.terminate()
        try:
            miner_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            miner_proc.kill()
            miner_proc.wait()
    except Exception as e:
        log.warning(f"Error stopping miner: {e}")

    mining_paused_reason = reason
    duration = (datetime.now() - mining_start_time) if mining_start_time else timedelta(0)
    earnings = estimate_session_earnings()
    miner_proc = None

    restore_gpu_clocks()
    notify_mining_stopped(reason, duration, earnings)
    update_ha_sensors()


def get_lolminer_stats() -> dict:
    """Query lolMiner local API for hashrate and share stats."""
    try:
        r = requests.get(f"http://localhost:{LOLMINER_API_PORT}", timeout=3)
        return r.json()
    except Exception:
        return {}


def estimate_session_earnings() -> float:
    """Rough USD earnings for current/last session based on power draw and known net/kWh."""
    if mining_start_time is None:
        return 0.0
    hours = (datetime.now() - mining_start_time).total_seconds() / 3600
    # 153W average draw (optimised profile)
    kwh = 0.153 * hours
    # Use last known NiceHash profitability if available, else rough $3.20/day gross
    daily_gross_usd = 3.20
    gross = daily_gross_usd * (hours / 24)
    elec_cost_usd = kwh * (ELECTRICITY_RATE_ZAR / USD_ZAR_RATE)
    return gross - elec_cost_usd

# ─── Discord ───────────────────────────────────────────────────────────────────

def _discord_post(payload: dict):
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Discord notification failed: {e}")


def _color(hex_str: str) -> int:
    return int(hex_str.lstrip("#"), 16)


def notify_mining_started():
    _discord_post({
        "embeds": [{
            "title": "⛏️ Mining Started",
            "color": _color("00c853"),
            "fields": [
                {"name": "Worker", "value": NICEHASH_WORKER, "inline": True},
                {"name": "Pool", "value": f"NiceHash kHeavyHash ({NICEHASH_REGION.upper()})", "inline": True},
                {"name": "Started", "value": datetime.now().strftime("%H:%M:%S"), "inline": True},
            ],
            "footer": {"text": "GPU was idle — mining resumed"},
        }]
    })


def notify_mining_stopped(reason: str, duration: timedelta, earnings_usd: float):
    h, rem = divmod(int(duration.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    dur_str = f"{h}h {m}m {s}s"
    earnings_zar = earnings_usd * USD_ZAR_RATE
    _discord_post({
        "embeds": [{
            "title": "⏸️ Mining Paused",
            "color": _color("ff6d00"),
            "fields": [
                {"name": "Reason",   "value": reason,   "inline": False},
                {"name": "Duration", "value": dur_str,  "inline": True},
                {"name": "Est. Earnings", "value": f"${earnings_usd:.3f} / R{earnings_zar:.2f}", "inline": True},
            ],
            "footer": {"text": "Will resume when GPU is idle"},
        }]
    })


def send_status_report():
    power = get_gpu_power_watts()
    power_str = f"{power:.1f}W" if power else "N/A"

    hours_today = total_kwh_today / 0.153 if total_kwh_today > 0 else 0  # rough
    elec_cost_zar = total_kwh_today * ELECTRICITY_RATE_ZAR

    # Gross estimate: $3.20/day, prorated
    gross_usd = 3.20 * (total_kwh_today / (0.153 * 24))
    net_usd   = gross_usd - (elec_cost_zar / USD_ZAR_RATE)
    net_zar   = net_usd * USD_ZAR_RATE

    status_str = "⛏️ Mining" if miner_proc else f"⏸️ Paused ({mining_paused_reason or 'profitability'})"

    stats = get_lolminer_stats()
    hashrate_str = "N/A"
    if stats:
        try:
            hr = stats["Algorithms"][0]["Speed"]
            hashrate_str = f"{hr/1e6:.1f} MH/s"
        except (KeyError, IndexError, TypeError):
            pass

    _discord_post({
        "embeds": [{
            "title": "📊 Mining Status Report",
            "color": _color("1565c0"),
            "fields": [
                {"name": "Status",        "value": status_str,                        "inline": False},
                {"name": "Hashrate",      "value": hashrate_str,                      "inline": True},
                {"name": "GPU Power",     "value": power_str,                         "inline": True},
                {"name": "kWh Today",     "value": f"{total_kwh_today:.3f} kWh",      "inline": True},
                {"name": "Elec. Cost",    "value": f"R{elec_cost_zar:.2f}",           "inline": True},
                {"name": "Gross Est.",    "value": f"${gross_usd:.3f}",               "inline": True},
                {"name": "Net Est.",      "value": f"${net_usd:.3f} / R{net_zar:.2f}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }]
    })
    log.info("Status report sent to Discord")


def send_profitability_alert(net_usd: float, stopped: bool):
    if stopped:
        msg = f"Net profit dropped to **${net_usd:.4f}/day** at R{ELECTRICITY_RATE_ZAR}/kWh. Mining stopped."
        title = "🚨 Mining Stopped — Unprofitable"
        color = _color("b71c1c")
    else:
        msg = f"Net profit is **${net_usd:.4f}/day** — still profitable."
        title = "✅ Profitability Check Passed"
        color = _color("2e7d32")

    _discord_post({
        "embeds": [{
            "title": title,
            "color": color,
            "description": msg,
            "timestamp": datetime.utcnow().isoformat(),
        }]
    })

# ─── NiceHash profitability check ─────────────────────────────────────────────

def check_profitability():
    global profitability_ok

    log.info("Running daily profitability check…")
    try:
        r = requests.get(
            "https://api2.nicehash.com/main/api/v2/public/simplemultialgo/info",
            timeout=15
        )
        r.raise_for_status()
        data = r.json()

        # Find kHeavyHash
        paying = None
        for algo in data.get("miningAlgorithms", []):
            if algo.get("algorithm", "").lower() == "kheavyhash":
                paying = float(algo["paying"])  # BTC per GH per day
                break

        if paying is None:
            log.warning("kHeavyHash not found in NiceHash algo list")
            return

        # RTX 4080 at optimised profile: ~1270 MH/s = 1.27 GH/s
        hashrate_gh = 1.27
        # BTC price from Coingecko (public, no key)
        btc_r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            timeout=10
        )
        btc_usd = btc_r.json()["bitcoin"]["usd"]

        gross_usd = paying * hashrate_gh * btc_usd
        # Electricity: 153W × 24h = 3.672 kWh/day
        kwh_per_day   = 0.153 * 24
        elec_cost_usd = kwh_per_day * ELECTRICITY_RATE_ZAR / USD_ZAR_RATE
        net_usd       = gross_usd - elec_cost_usd

        log.info(
            f"Profitability: gross=${gross_usd:.4f}/day, "
            f"elec=${elec_cost_usd:.4f}/day, net=${net_usd:.4f}/day "
            f"(BTC=${btc_usd:,.0f}, paying={paying:.8f} BTC/GH/day)"
        )

        if net_usd <= 0 and profitability_ok:
            profitability_ok = False
            stop_mining("Daily profitability check: net ≤ $0")
            send_profitability_alert(net_usd, stopped=True)
        elif net_usd > 0 and not profitability_ok:
            profitability_ok = True
            log.info("Profitability restored — mining will resume")
            send_profitability_alert(net_usd, stopped=False)
        else:
            send_profitability_alert(net_usd, stopped=False)

    except Exception as e:
        log.error(f"Profitability check failed: {e}")

# ─── Home Assistant ────────────────────────────────────────────────────────────

def _ha_post(entity_id: str, state: str, attributes: dict = None):
    try:
        requests.post(
            f"{HA_URL}/api/states/{entity_id}",
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"state": state, "attributes": attributes or {}},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"HA update failed for {entity_id}: {e}")


def update_ha_sensors():
    power = get_gpu_power_watts()
    status = "mining" if miner_proc else ("paused" if not profitability_ok else "idle")
    kwh_today = total_kwh_today
    elec_cost_zar = kwh_today * ELECTRICITY_RATE_ZAR
    gross_usd = 3.20 * (kwh_today / (0.153 * 24)) if kwh_today > 0 else 0
    net_usd   = gross_usd - (elec_cost_zar / USD_ZAR_RATE)
    net_zar   = net_usd * USD_ZAR_RATE

    duration_min = 0
    if mining_start_time:
        duration_min = int((datetime.now() - mining_start_time).total_seconds() / 60)

    _ha_post("sensor.mining_status", status, {
        "friendly_name": "Mining Status",
        "icon": "mdi:pickaxe",
    })
    _ha_post("sensor.mining_gpu_power_w", str(round(power, 1)) if power else "unavailable", {
        "friendly_name": "Mining GPU Power",
        "unit_of_measurement": "W",
        "device_class": "power",
        "icon": "mdi:flash",
    })
    _ha_post("sensor.mining_kwh_today", str(round(kwh_today, 3)), {
        "friendly_name": "Mining kWh Today",
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "icon": "mdi:meter-electric",
    })
    _ha_post("sensor.mining_elec_cost_zar", str(round(elec_cost_zar, 2)), {
        "friendly_name": "Mining Electricity Cost",
        "unit_of_measurement": "ZAR",
        "icon": "mdi:currency-usd",
    })
    _ha_post("sensor.mining_net_profit_zar", str(round(net_zar, 2)), {
        "friendly_name": "Mining Net Profit (ZAR)",
        "unit_of_measurement": "ZAR",
        "icon": "mdi:trending-up",
    })
    _ha_post("sensor.mining_session_minutes", str(duration_min), {
        "friendly_name": "Current Mining Session",
        "unit_of_measurement": "min",
        "icon": "mdi:timer",
    })
    _ha_post("sensor.mining_worker", NICEHASH_WORKER, {
        "friendly_name": "Mining Worker",
        "icon": "mdi:server",
        "paused_reason": mining_paused_reason,
        "profitability_ok": profitability_ok,
    })

# ─── Scheduler ─────────────────────────────────────────────────────────────────

def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


class Scheduler:
    """Minimal scheduler: fires callbacks at configured HH:MM times each day."""

    def __init__(self):
        self._jobs: list[tuple[int, int, callable]] = []
        self._fired_today: set[str] = set()

    def add(self, time_str: str, fn: callable):
        h, m = _parse_hhmm(time_str)
        self._jobs.append((h, m, fn))

    def tick(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        midnight = now.hour == 0 and now.minute == 0
        if midnight:
            self._fired_today.clear()

        for h, m, fn in self._jobs:
            key = f"{today}-{h:02d}{m:02d}-{fn.__name__}"
            if now.hour == h and now.minute == m and key not in self._fired_today:
                self._fired_today.add(key)
                threading.Thread(target=fn, daemon=True).start()

# ─── Power accounting ──────────────────────────────────────────────────────────

def _accrue_power():
    global session_kwh, total_kwh_today, last_power_sample_time

    if miner_proc is None or last_power_sample_time is None:
        return

    now = datetime.now()
    # Reset daily total at midnight
    if now.date() != last_power_sample_time.date():
        total_kwh_today = 0.0

    elapsed_h = (now - last_power_sample_time).total_seconds() / 3600
    power = get_gpu_power_watts() or 153.0  # fall back to rated draw
    kwh = power / 1000 * elapsed_h
    session_kwh += kwh
    total_kwh_today += kwh
    last_power_sample_time = now

# ─── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global idle_since, busy_since

    log.info("mining_manager starting up")

    scheduler = Scheduler()
    for t in REPORT_TIMES:
        scheduler.add(t, send_status_report)
    scheduler.add(PROFITABILITY_CHECK_TIME, check_profitability)

    # HA background updater
    def ha_loop():
        while not shutdown_event.is_set():
            update_ha_sensors()
            shutdown_event.wait(HA_UPDATE_INTERVAL)

    threading.Thread(target=ha_loop, daemon=True).start()

    # Initial profitability check
    check_profitability()

    while not shutdown_event.is_set():
        scheduler.tick()
        _accrue_power()

        gpu_state, detail = get_gpu_state()

        if gpu_state == "idle":
            busy_since = None
            if idle_since is None:
                idle_since = datetime.now()
                log.info("GPU became idle")

            idle_secs = (datetime.now() - idle_since).total_seconds()
            if idle_secs >= IDLE_GRACE_PERIOD and miner_proc is None and profitability_ok:
                start_mining()

        else:  # busy
            idle_since = None
            if busy_since is None:
                busy_since = datetime.now()
                log.info(f"GPU in use: {detail}")

            busy_secs = (datetime.now() - busy_since).total_seconds()
            if busy_secs >= BUSY_GRACE_PERIOD and miner_proc is not None:
                stop_mining(detail)

        # Check if miner died unexpectedly
        if miner_proc is not None and miner_proc.poll() is not None:
            log.warning(f"Miner exited unexpectedly (code {miner_proc.returncode})")
            duration = (datetime.now() - mining_start_time) if mining_start_time else timedelta(0)
            earnings = estimate_session_earnings()
            miner_proc = None
            restore_gpu_clocks()
            notify_mining_stopped("Miner crashed/exited", duration, earnings)
            update_ha_sensors()

        shutdown_event.wait(CHECK_INTERVAL)

    # Graceful shutdown
    log.info("Shutdown signal received")
    if miner_proc is not None:
        stop_mining("Daemon shutting down")


def _handle_signal(sig, frame):
    log.info(f"Signal {sig} received — shutting down")
    shutdown_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    main()
