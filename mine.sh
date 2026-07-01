#!/usr/bin/env bash
# mine.sh — run RainbowMiner; pause when another process uses the GPU

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        val="${val%%[[:space:]]*#*}"  # strip inline comments
        val="${val%"${val##*[![:space:]]}"}"  # trim trailing space
        export "$key=$val"
    done < "$SCRIPT_DIR/.env"
fi

RBM_DIR="${RAINBOWMINER_DIR:-$SCRIPT_DIR/rainbowminer}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
IDLE_GRACE_PERIOD="${IDLE_GRACE_PERIOD:-10}"
BUSY_GRACE_PERIOD="${BUSY_GRACE_PERIOD:-15}"
GPU_CORE="${GPU_CORE_CLOCK_LOCK:-2205}"
GPU_POWER="${GPU_POWER_LIMIT_W:-350}"
WEBHOOK="${DISCORD_WEBHOOK_URL:-}"
WORKER="${WORKER_NAME:-miner}"

MINER_PID=""
BUSY_SINCE=0
IDLE_SINCE=0
MINING_START_TIME=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Returns "temp,power,clock,util" with nounits
gpu_stats() {
    nvidia-smi --query-gpu=temperature.gpu,power.draw,clocks.sm,utilization.gpu \
        --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' '
}

# Returns the name of the external process with the highest SM% on the GPU
external_proc_name() {
    local best_sm=0 best_name=""

    while IFS= read -r line; do
        local pid sm name
        pid=$(awk '{print $2}' <<< "$line")
        sm=$(awk '{print $4}' <<< "$line")
        name=$(awk '{print $NF}' <<< "$line")
        [[ -z "$pid" || "$pid" == "#" || "$pid" == "-" ]] && continue
        [[ "$sm" == "-" || "$sm" == "%" ]] && sm=0
        is_miner_binary "$pid" && continue
        if (( sm > best_sm )); then
            best_sm=$sm
            best_name=$name
        fi
    done < <(nvidia-smi pmon -c 1 -s u 2>/dev/null | tail -n +3)

    echo "${best_name:-unknown}"
}

uptime_str() {
    [[ $MINING_START_TIME -eq 0 ]] && echo "unknown" && return
    local secs=$(( $(date +%s) - MINING_START_TIME ))
    printf '%dh %02dm' $(( secs / 3600 )) $(( (secs % 3600) / 60 ))
}

discord_embed() {
    [[ -z "$WEBHOOK" ]] && return 0
    curl -s -X POST "$WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "$1" &>/dev/null || true &
}

notify_started() {
    local stats temp power clock util
    stats=$(gpu_stats)
    IFS=',' read -r temp power clock util <<< "$stats"

    discord_embed "$(python3 -c "
import json
print(json.dumps({
    'embeds': [{
        'title': '⛏️  Mining Started',
        'color': 3066993,
        'fields': [
            {'name': 'Worker',      'value': '${WORKER}',       'inline': True},
            {'name': 'GPU Temp',    'value': '${temp}°C',  'inline': True},
            {'name': 'Power',       'value': '${power}W',       'inline': True},
            {'name': 'Core Clock',  'value': '${clock} MHz',    'inline': True},
            {'name': 'GPU Util',    'value': '${util}%',        'inline': True},
        ],
        'footer': {'text': 'Zpool → LTC'},
        'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)'
    }]
}))")"
}

notify_paused() {
    local reason="${1:-GPU needed}"
    local proc="${2:-}"
    local stats temp power clock util
    stats=$(gpu_stats)
    IFS=',' read -r temp power clock util <<< "$stats"
    local uptime
    uptime=$(uptime_str)

    local proc_field=""
    [[ -n "$proc" ]] && proc_field="{'name': 'Taken by', 'value': '${proc}', 'inline': True},"

    discord_embed "$(python3 -c "
import json
fields = [
    {'name': 'Reason',    'value': '${reason}',   'inline': True},
    {'name': 'Uptime',    'value': '${uptime}',   'inline': True},
]
proc = '${proc}'
if proc:
    fields.append({'name': 'Taken by', 'value': proc, 'inline': True})
fields += [
    {'name': 'GPU Temp',  'value': '${temp}°C', 'inline': True},
    {'name': 'Power',     'value': '${power}W',      'inline': True},
]
print(json.dumps({
    'embeds': [{
        'title': '⏸️  Mining Paused',
        'color': 15105570,
        'fields': fields,
        'footer': {'text': '${WORKER}'},
        'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)'
    }]
}))")"
}

notify_crashed() {
    local uptime
    uptime=$(uptime_str)

    discord_embed "$(python3 -c "
import json
print(json.dumps({
    'embeds': [{
        'title': '⚠️  Miner Crashed',
        'color': 15158332,
        'fields': [
            {'name': 'Worker', 'value': '${WORKER}', 'inline': True},
            {'name': 'Uptime', 'value': '${uptime}', 'inline': True},
        ],
        'description': 'RainbowMiner exited unexpectedly — will restart when GPU is free.',
        'footer': {'text': '${WORKER}'},
        'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)'
    }]
}))")"
}

notify_shutdown() {
    local uptime
    uptime=$(uptime_str)

    discord_embed "$(python3 -c "
import json
print(json.dumps({
    'embeds': [{
        'title': '🛑  Miner Stopped',
        'color': 9807270,
        'fields': [
            {'name': 'Worker', 'value': '${WORKER}', 'inline': True},
            {'name': 'Uptime', 'value': '${uptime}', 'inline': True},
        ],
        'footer': {'text': '${WORKER}'},
        'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)'
    }]
}))")"
}

apply_oc() {
    nvidia-smi -lgc "$GPU_CORE" &>/dev/null || true
    nvidia-smi -pl "$GPU_POWER" &>/dev/null || true
}

reset_oc() {
    nvidia-smi -rgc &>/dev/null || true
}

start_miner() {
    log "Starting RainbowMiner"
    apply_oc
    setsid pwsh -NonInteractive -NoProfile \
        -File "$RBM_DIR/RainbowMiner.ps1" \
        -MinerStatusKey "" \
        -MinerStatusURL "" &
    MINER_PID=$!
    MINING_START_TIME=$(date +%s)
    IDLE_SINCE=0
    log "Started PID=$MINER_PID"
    notify_started
}

stop_miner() {
    local reason="${1:-GPU needed}"
    local proc="${2:-}"
    log "Stopping miner: $reason"
    notify_paused "$reason" "$proc"
    reset_oc
    if [[ -n "$MINER_PID" ]]; then
        kill -TERM "$MINER_PID" 2>/dev/null || true
        sleep 5
        kill -KILL "$MINER_PID" 2>/dev/null || true
    fi
    MINER_PID=""
    BUSY_SINCE=0
}

miner_running() {
    [[ -n "$MINER_PID" ]] && kill -0 "$MINER_PID" 2>/dev/null
}

# True if a GPU process's binary lives under the RainbowMiner Bin directory
is_miner_binary() {
    local pid="$1"
    local exe
    exe=$(readlink -f /proc/"$pid"/exe 2>/dev/null || true)
    [[ "$exe" == "$RBM_DIR/Bin"* ]]
}

# True if external (non-miner) processes have SM% above threshold
external_gpu_busy() {
    local threshold="${GPU_UTIL_THRESHOLD:-15}"
    local total=0

    while IFS= read -r line; do
        local pid sm
        pid=$(awk '{print $2}' <<< "$line")
        sm=$(awk '{print $4}' <<< "$line")
        [[ -z "$pid" || "$pid" == "#" || "$pid" == "-" ]] && continue
        [[ "$sm" == "-" || "$sm" == "%" ]] && continue
        is_miner_binary "$pid" && continue
        total=$(( total + sm ))
    done < <(nvidia-smi pmon -c 1 -s u 2>/dev/null | tail -n +3)

    (( total > threshold ))
}

cleanup() {
    log "Shutting down"
    notify_shutdown
    sleep 1  # let discord fire
    stop_miner "shutdown"
    exit 0
}

trap cleanup SIGTERM SIGINT SIGHUP

start_miner
NOW=$(date +%s)
IDLE_SINCE=$NOW

while true; do
    sleep "$CHECK_INTERVAL"
    NOW=$(date +%s)

    if miner_running; then
        if external_gpu_busy; then
            if [[ $BUSY_SINCE -eq 0 ]]; then
                BUSY_SINCE=$NOW
                log "External GPU use detected, waiting grace period"
            elif (( NOW - BUSY_SINCE >= BUSY_GRACE_PERIOD )); then
                proc=$(external_proc_name)
                stop_miner "GPU in use" "$proc"
            fi
        else
            BUSY_SINCE=0
        fi
    else
        if [[ -n "$MINER_PID" ]]; then
            log "Miner exited unexpectedly"
            MINER_PID=""
            notify_crashed
        fi

        if ! external_gpu_busy; then
            if [[ $IDLE_SINCE -eq 0 ]]; then
                IDLE_SINCE=$NOW
                log "GPU free, waiting idle grace period"
            elif (( NOW - IDLE_SINCE >= IDLE_GRACE_PERIOD )); then
                start_miner
            fi
        else
            IDLE_SINCE=0
        fi
    fi
done
