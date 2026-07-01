#!/usr/bin/env python3
"""
mining_manager.py — RTX 4080 auto-mining daemon

Starts RainbowMiner (profit-switching across MiningPoolHub + Zpool) whenever
the GPU is idle. Applies OC profile on start, resets on stop. Sends Discord
notifications and pushes Home Assistant sensors.
"""

import json
import logging
import os
import signal
import subprocess
import sys
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

RAINBOWMINER_DIR      = Path(_env("RAINBOWMINER_DIR", "./rainbowminer")).resolve()
EXCLUDED_PROCESS_PATTERNS = [
    p.strip() for p in os.getenv("EXCLUDED_PROCESS_PATTERNS", "").split(",") if p.strip()
]
RAINBOWMINER_API_PORT = _env("RAINBOWMINER_API_PORT", "4000", int)
WORKER_NAME           = _env("WORKER_NAME", "trog-4080")
GPU_CORE_CLOCK_LOCK   = _env("GPU_CORE_CLOCK_LOCK", "2205")
GPU_POWER_LIMIT_W     = _env("GPU_POWER_LIMIT_W", "350")
CHECK_INTERVAL        = _env("CHECK_INTERVAL", "30", int)
GPU_UTIL_THRESHOLD    = _env("GPU_UTIL_THRESHOLD", "15", int)
IDLE_GRACE_PERIOD     = _env("IDLE_GRACE_PERIOD", "60", int)
BUSY_GRACE_PERIOD     = _env("BUSY_GRACE_PERIOD", "15", int)
ELECTRICITY_RATE_ZAR  = _env("ELECTRICITY_RATE_ZAR", "4.09", float)
USD_ZAR_RATE          = _env("USD_ZAR_RATE", "16.52", float)
DISCORD_WEBHOOK_URL   = _env("DISCORD_WEBHOOK_URL")
REPORT_TIMES          = _env("REPORT_TIMES", "08:00,20:00").split(",")
PROFITABILITY_CHECK_TIME = _env("PROFITABILITY_CHECK_TIME", "06:00")
HA_URL                = _env("HA_URL", "https://homeass.trog.co.za").rstrip("/")
HA_TOKEN              = _env("HA_TOKEN")
HA_UPDATE_INTERVAL    = _env("HA_UPDATE_INTERVAL", "60", int)

RAINBOWMINER_SCRIPT = RAINBOWMINER_DIR / "RainbowMiner.ps1"
RBM_API             = f"http://localhost:{RAINBOWMINER_API_PORT}"

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

miner_proc = None           # subprocess.Popen | None
mining_start_time = None    # datetime | None
mining_paused_reason = ""
profitability_ok = True

idle_since = None           # datetime | None
busy_since = None           # datetime | None

total_kwh_today = 0.0
last_power_sample_time = None
last_power_report_date = None

shutdown_event = threading.Event()

# ─── GPU queries ───────────────────────────────────────────────────────────────

def _smi(*args) -> str:
    r = subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def get_gpu_state() -> tuple:
    """Returns ('idle', '') or ('busy', reason_str)."""

    # 1. Encoder / decoder sessions — query separately (decoder field absent on some drivers)
    enc, dec = 0, 0
    for field, slot in [("encoder.stats.sessionCount", "enc"), ("decoder.stats.sessionCount", "dec")]:
        try:
            val = int(_smi(f"--query-gpu={field}", "--format=csv,noheader,nounits").strip())
            if slot == "enc":
                enc = val
            else:
                dec = val
        except Exception:
            pass
    if enc > 0 or dec > 0:
        parts = []
        if enc: parts.append(f"{enc} encoder{'s' if enc > 1 else ''}")
        if dec: parts.append(f"{dec} decoder{'s' if dec > 1 else ''}")
        return "busy", f"Hardware transcoding ({', '.join(parts)})"

    # 2. CUDA compute processes — exclude our own miner
    try:
        raw = _smi("--query-compute-apps=pid,process_name,used_gpu_memory", "--format=csv,noheader")
        others = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            name = parts[1] if len(parts) > 1 else "unknown"
            if miner_proc and pid == miner_proc.pid:
                continue
            # Skip child processes of our miner (pwsh spawns the actual miner binary)
            if miner_proc and _is_child_of(pid, miner_proc.pid):
                continue
            # Skip processes matching user-configured exclusion patterns
            if any(pat in name for pat in EXCLUDED_PROCESS_PATTERNS):
                continue
            others.append(f"{name} (pid {pid})")
        if others:
            return "busy", f"GPU compute: {', '.join(others)}"
    except Exception as e:
        log.warning(f"compute-apps query failed: {e}")

    # 3. Raw utilisation fallback — only when no miner is running
    if miner_proc is None:
        try:
            util = int(_smi("--query-gpu=utilization.gpu", "--format=csv,noheader,nounits").strip())
            if util >= GPU_UTIL_THRESHOLD:
                return "busy", f"GPU utilisation {util}% (threshold {GPU_UTIL_THRESHOLD}%)"
        except Exception:
            pass

    return "idle", ""


def _is_child_of(pid: int, parent_pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                    return ppid == parent_pid or _is_child_of(ppid, parent_pid)
    except Exception:
        pass
    return False


def get_gpu_power_watts() -> float | None:
    try:
        return float(_smi("--query-gpu=power.draw", "--format=csv,noheader,nounits").strip())
    except Exception:
        return None

# ─── GPU overclock ─────────────────────────────────────────────────────────────

def apply_oc():
    try:
        subprocess.run(["nvidia-smi", "-pm", "1"], check=True, capture_output=True)
        subprocess.run(
            ["nvidia-smi", "--lock-gpu-clocks", f"{GPU_CORE_CLOCK_LOCK},{GPU_CORE_CLOCK_LOCK}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["nvidia-smi", "-pl", GPU_POWER_LIMIT_W],
            check=True, capture_output=True,
        )
        log.info(f"OC applied: core locked {GPU_CORE_CLOCK_LOCK} MHz, PL {GPU_POWER_LIMIT_W}W")
    except subprocess.CalledProcessError as e:
        log.warning(f"OC apply failed (non-fatal): {e}")


def restore_oc():
    try:
        subprocess.run(["nvidia-smi", "--reset-gpu-clocks"],    check=True, capture_output=True)
        subprocess.run(["nvidia-smi", "--reset-memory-clocks"], check=True, capture_output=True)
        subprocess.run(["nvidia-smi", "-pm", "0"],              check=True, capture_output=True)
        log.info("GPU clocks reset to stock")
    except subprocess.CalledProcessError as e:
        log.warning(f"OC restore failed (non-fatal): {e}")

# ─── RainbowMiner control ──────────────────────────────────────────────────────

def start_mining():
    global miner_proc, mining_start_time, total_kwh_today, last_power_sample_time

    if miner_proc is not None:
        return
    if not profitability_ok:
        log.info("Mining suppressed — profitability check flagged negative")
        return
    if not RAINBOWMINER_SCRIPT.exists():
        log.error(f"RainbowMiner not found at {RAINBOWMINER_SCRIPT}. Run setup.sh first.")
        return

    config_file = RAINBOWMINER_DIR / "Config" / "config.json"
    if not config_file.exists():
        log.error("RainbowMiner not configured yet. Run: pwsh rainbowminer/RainbowMiner.ps1")
        return

    apply_oc()

    cmd = [
        "pwsh", "-NonInteractive", "-File", str(RAINBOWMINER_SCRIPT),
        "-APIport", str(RAINBOWMINER_API_PORT),
    ]
    log.info(f"Starting RainbowMiner: {' '.join(cmd)}")
    try:
        miner_proc = subprocess.Popen(
            cmd,
            cwd=str(RAINBOWMINER_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        mining_start_time = datetime.now()
        last_power_sample_time = mining_start_time
        log.info(f"RainbowMiner started (pid {miner_proc.pid})")
        notify_mining_started()
        update_ha_sensors()
    except FileNotFoundError:
        log.error("pwsh not found. Install PowerShell Core: apt install powershell")
        miner_proc = None
    except Exception as e:
        log.error(f"Failed to start RainbowMiner: {e}")
        miner_proc = None


def stop_mining(reason: str = "GPU in use"):
    global miner_proc, mining_paused_reason

    if miner_proc is None:
        return

    log.info(f"Stopping RainbowMiner (reason: {reason})")
    duration = (datetime.now() - mining_start_time) if mining_start_time else timedelta(0)
    earnings = get_rbm_session_earnings()

    try:
        miner_proc.terminate()
        try:
            miner_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            miner_proc.kill()
            miner_proc.wait()
    except Exception as e:
        log.warning(f"Error stopping miner: {e}")

    miner_proc = None
    mining_paused_reason = reason
    restore_oc()
    notify_mining_stopped(reason, duration, earnings)
    update_ha_sensors()

# ─── RainbowMiner API ──────────────────────────────────────────────────────────

def get_rbm_api(endpoint: str) -> dict:
    try:
        r = requests.get(f"{RBM_API}/{endpoint}", timeout=5)
        return r.json()
    except Exception:
        return {}


def get_rbm_summary() -> dict:
    return get_rbm_api("api=summary")


def get_rbm_session_earnings() -> float:
    """USD earnings for the current session from RainbowMiner, or a power-based estimate."""
    data = get_rbm_summary()
    try:
        return float(data.get("Profit_Avg", data.get("CurrentProfit", 0)))
    except (TypeError, ValueError):
        pass
    # Fallback: estimate from power draw
    if mining_start_time:
        hours = (datetime.now() - mining_start_time).total_seconds() / 3600
        kwh = 0.153 * hours
        gross = 3.20 * (hours / 24)
        return gross - kwh * (ELECTRICITY_RATE_ZAR / USD_ZAR_RATE)
    return 0.0


def get_rbm_hashrate() -> str:
    data = get_rbm_api("api=miners")
    try:
        miners = data if isinstance(data, list) else data.get("Miners", [])
        total = sum(float(m.get("Speed_Live", m.get("Speed", 0))) for m in miners)
        if total > 1e9:
            return f"{total/1e9:.2f} GH/s"
        elif total > 1e6:
            return f"{total/1e6:.2f} MH/s"
        elif total > 0:
            return f"{total:.0f} H/s"
    except Exception:
        pass
    return "N/A"


def get_rbm_current_algo() -> str:
    data = get_rbm_api("api=miners")
    try:
        miners = data if isinstance(data, list) else data.get("Miners", [])
        if miners:
            m = miners[0]
            algo = m.get("Algorithm", "")
            coin = m.get("CoinName", m.get("CoinSymbol", ""))
            pool = m.get("PoolName", "")
            return f"{algo}/{coin} @ {pool}" if coin else f"{algo} @ {pool}"
    except Exception:
        pass
    return "N/A"


def check_profitability():
    global profitability_ok

    log.info("Running daily profitability check…")
    data = get_rbm_summary()
    if not data:
        log.warning("RainbowMiner API unreachable — skipping profitability check")
        return

    try:
        # RainbowMiner already factors in electricity (PowerPrice in config)
        net_usd = float(data.get("Profit_Avg", data.get("CurrentProfit", 1)))
    except (TypeError, ValueError):
        log.warning("Could not parse profit from RainbowMiner API")
        return

    log.info(f"RainbowMiner net profit: ${net_usd:.6f}/day")

    if net_usd <= 0 and profitability_ok:
        profitability_ok = False
        stop_mining("Daily profitability check: net ≤ $0 after electricity")
        send_profitability_alert(net_usd, stopped=True)
    elif net_usd > 0 and not profitability_ok:
        profitability_ok = True
        log.info("Profitability restored — mining will resume on next idle window")
        send_profitability_alert(net_usd, stopped=False)
    else:
        send_profitability_alert(net_usd, stopped=False)

# ─── Power accounting ──────────────────────────────────────────────────────────

def _accrue_power():
    global total_kwh_today, last_power_sample_time, last_power_report_date

    if miner_proc is None or last_power_sample_time is None:
        return

    now = datetime.now()
    if last_power_report_date and now.date() != last_power_report_date:
        total_kwh_today = 0.0
    last_power_report_date = now.date()

    elapsed_h = (now - last_power_sample_time).total_seconds() / 3600
    power = get_gpu_power_watts() or 153.0
    total_kwh_today += (power / 1000) * elapsed_h
    last_power_sample_time = now

# ─── Discord ───────────────────────────────────────────────────────────────────

def _color(h: str) -> int:
    return int(h.lstrip("#"), 16)


def _discord(payload: dict):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        log.warning(f"Discord failed: {e}")


def notify_mining_started():
    algo = get_rbm_current_algo()
    _discord({"embeds": [{
        "title": "⛏️ Mining Started",
        "color": _color("00c853"),
        "fields": [
            {"name": "Worker",  "value": WORKER_NAME, "inline": True},
            {"name": "Mining",  "value": algo or "starting up…", "inline": True},
            {"name": "Started", "value": datetime.now().strftime("%H:%M:%S"), "inline": True},
        ],
        "footer": {"text": "GPU was idle — mining resumed"},
    }]})


def notify_mining_stopped(reason: str, duration: timedelta, earnings_usd: float):
    h, rem = divmod(int(duration.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    _discord({"embeds": [{
        "title": "⏸️ Mining Paused",
        "color": _color("ff6d00"),
        "fields": [
            {"name": "Reason",        "value": reason,                                      "inline": False},
            {"name": "Duration",      "value": f"{h}h {m}m {s}s",                           "inline": True},
            {"name": "Est. Earnings", "value": f"${earnings_usd:.4f} / R{earnings_usd*USD_ZAR_RATE:.2f}", "inline": True},
        ],
        "footer": {"text": "Will resume when GPU is idle"},
    }]})


def send_status_report():
    power = get_gpu_power_watts()
    elec_cost_zar = total_kwh_today * ELECTRICITY_RATE_ZAR
    summary = get_rbm_summary()
    net_usd = 0.0
    try:
        net_usd = float(summary.get("Profit_Avg", summary.get("CurrentProfit", 0)))
    except (TypeError, ValueError):
        pass
    net_zar = net_usd * USD_ZAR_RATE

    status_str = "⛏️ Mining" if miner_proc else f"⏸️ Paused ({mining_paused_reason or 'unknown'})"
    duration_min = 0
    if mining_start_time:
        duration_min = int((datetime.now() - mining_start_time).total_seconds() / 60)

    _discord({"embeds": [{
        "title": "📊 Mining Status Report",
        "color": _color("1565c0"),
        "fields": [
            {"name": "Status",         "value": status_str,                              "inline": False},
            {"name": "Algorithm",      "value": get_rbm_current_algo(),                  "inline": True},
            {"name": "Hashrate",       "value": get_rbm_hashrate(),                      "inline": True},
            {"name": "GPU Power",      "value": f"{power:.1f}W" if power else "N/A",     "inline": True},
            {"name": "Session",        "value": f"{duration_min} min",                   "inline": True},
            {"name": "kWh Today",      "value": f"{total_kwh_today:.3f} kWh",            "inline": True},
            {"name": "Elec. Cost",     "value": f"R{elec_cost_zar:.2f}",                 "inline": True},
            {"name": "Net Profit/day", "value": f"${net_usd:.4f} / R{net_zar:.2f}",     "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }]})
    log.info("Status report sent to Discord")


def send_profitability_alert(net_usd: float, stopped: bool):
    if stopped:
        title, color = "🚨 Mining Stopped — Unprofitable", _color("b71c1c")
        desc = f"Net profit **${net_usd:.4f}/day** after electricity at R{ELECTRICITY_RATE_ZAR}/kWh. Mining stopped."
    else:
        title, color = "✅ Profitability Check Passed", _color("2e7d32")
        desc = f"Net profit **${net_usd:.4f}/day** — still profitable. Mining continues."
    _discord({"embeds": [{"title": title, "color": color, "description": desc,
                          "timestamp": datetime.utcnow().isoformat()}]})

# ─── Home Assistant ────────────────────────────────────────────────────────────

def _ha(entity_id: str, state: str, attributes: dict = None):
    try:
        requests.post(
            f"{HA_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
            json={"state": state, "attributes": attributes or {}},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"HA update failed ({entity_id}): {e}")


def update_ha_sensors():
    power = get_gpu_power_watts()
    status = "mining" if miner_proc else ("paused" if not profitability_ok else "idle")
    elec_cost_zar = total_kwh_today * ELECTRICITY_RATE_ZAR
    summary = get_rbm_summary()
    net_usd = 0.0
    try:
        net_usd = float(summary.get("Profit_Avg", summary.get("CurrentProfit", 0)))
    except (TypeError, ValueError):
        pass
    net_zar = net_usd * USD_ZAR_RATE
    duration_min = int((datetime.now() - mining_start_time).total_seconds() / 60) if mining_start_time else 0

    _ha("sensor.mining_status",          status,                    {"friendly_name": "Mining Status",          "icon": "mdi:pickaxe"})
    _ha("sensor.mining_gpu_power_w",     str(round(power, 1)) if power else "unavailable",
                                                                    {"friendly_name": "Mining GPU Power",        "unit_of_measurement": "W",   "device_class": "power"})
    _ha("sensor.mining_kwh_today",       str(round(total_kwh_today, 3)),
                                                                    {"friendly_name": "Mining kWh Today",        "unit_of_measurement": "kWh", "device_class": "energy"})
    _ha("sensor.mining_elec_cost_zar",   str(round(elec_cost_zar, 2)),
                                                                    {"friendly_name": "Mining Electricity Cost", "unit_of_measurement": "ZAR", "icon": "mdi:lightning-bolt"})
    _ha("sensor.mining_net_profit_zar",  str(round(net_zar, 2)),   {"friendly_name": "Mining Net Profit",       "unit_of_measurement": "ZAR", "icon": "mdi:trending-up"})
    _ha("sensor.mining_session_minutes", str(duration_min),         {"friendly_name": "Mining Session",          "unit_of_measurement": "min", "icon": "mdi:timer"})
    _ha("sensor.mining_algorithm",       get_rbm_current_algo(),    {"friendly_name": "Mining Algorithm",        "icon": "mdi:code-braces"})
    _ha("sensor.mining_hashrate",        get_rbm_hashrate(),        {"friendly_name": "Mining Hashrate",         "icon": "mdi:speedometer"})

# ─── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    def __init__(self):
        self._jobs = []
        self._fired = set()

    def add(self, time_str: str, fn):
        h, m = (int(x) for x in time_str.strip().split(":"))
        self._jobs.append((h, m, fn))

    def tick(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour == 0 and now.minute < 1:
            self._fired = {k for k in self._fired if not k.startswith(today[:8])}
        for h, m, fn in self._jobs:
            key = f"{today}-{h:02d}{m:02d}-{fn.__name__}"
            if now.hour == h and now.minute == m and key not in self._fired:
                self._fired.add(key)
                threading.Thread(target=fn, daemon=True).start()

# ─── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global idle_since, busy_since, miner_proc, mining_start_time

    log.info("mining_manager starting up")
    log.info(f"RainbowMiner dir: {RAINBOWMINER_DIR}")
    if EXCLUDED_PROCESS_PATTERNS:
        log.info(f"Ignoring GPU processes matching: {', '.join(EXCLUDED_PROCESS_PATTERNS)}")
    else:
        log.info("No process exclusions configured (EXCLUDED_PROCESS_PATTERNS is empty)")

    sched = Scheduler()
    for t in REPORT_TIMES:
        sched.add(t, send_status_report)
    sched.add(PROFITABILITY_CHECK_TIME, check_profitability)

    # HA updater thread
    def ha_loop():
        while not shutdown_event.is_set():
            update_ha_sensors()
            shutdown_event.wait(HA_UPDATE_INTERVAL)

    threading.Thread(target=ha_loop, daemon=True).start()

    while not shutdown_event.is_set():
        sched.tick()
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
        else:
            idle_since = None
            if busy_since is None:
                busy_since = datetime.now()
                log.info(f"GPU in use: {detail}")
            busy_secs = (datetime.now() - busy_since).total_seconds()
            if busy_secs >= BUSY_GRACE_PERIOD and miner_proc is not None:
                stop_mining(detail)

        # Miner died unexpectedly
        if miner_proc is not None and miner_proc.poll() is not None:
            code = miner_proc.returncode
            log.warning(f"RainbowMiner exited unexpectedly (code {code})")
            duration = (datetime.now() - mining_start_time) if mining_start_time else timedelta(0)
            earnings = get_rbm_session_earnings()
            miner_proc = None
            restore_oc()
            notify_mining_stopped(f"RainbowMiner exited (code {code})", duration, earnings)
            update_ha_sensors()

        shutdown_event.wait(CHECK_INTERVAL)

    log.info("Shutdown signal received")
    if miner_proc is not None:
        stop_mining("Daemon shutting down")


def _handle_signal(sig, _frame):
    log.info(f"Signal {sig} — shutting down")
    shutdown_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    main()
