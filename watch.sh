#!/usr/bin/env bash
# watch.sh — Apo vault watcher (manual start/stop; launchd uses launchd-watch.sh)
#
# Usage:
#   bash watch.sh start    start the watcher
#   bash watch.sh stop     stop the watcher
#   bash watch.sh restart  stop then start
#   bash watch.sh status   show running/stopped state

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${SCRIPT_DIR}/config.env"

# Preserve caller overrides (e.g. APO_VAULTS=… just watch-start) across .env source.
_SAVED_APO_VAULTS="${APO_VAULTS-}"
_SAVED_APO_NOTES_ROOT="${APO_NOTES_ROOT-}"
_SAVED_APO_INDEX="${APO_INDEX-}"
_SAVED_APO_COLLECTION="${APO_COLLECTION-}"
_HAD_APO_VAULTS=0; [[ -n "${APO_VAULTS+x}" ]] && _HAD_APO_VAULTS=1
_HAD_APO_NOTES_ROOT=0; [[ -n "${APO_NOTES_ROOT+x}" ]] && _HAD_APO_NOTES_ROOT=1
_HAD_APO_INDEX=0; [[ -n "${APO_INDEX+x}" ]] && _HAD_APO_INDEX=1
_HAD_APO_COLLECTION=0; [[ -n "${APO_COLLECTION+x}" ]] && _HAD_APO_COLLECTION=1

set -a
# shellcheck source=config.env.example
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
set +a

(( _HAD_APO_VAULTS )) && export APO_VAULTS="$_SAVED_APO_VAULTS"
(( _HAD_APO_NOTES_ROOT )) && export APO_NOTES_ROOT="$_SAVED_APO_NOTES_ROOT"
(( _HAD_APO_INDEX )) && export APO_INDEX="$_SAVED_APO_INDEX"
(( _HAD_APO_COLLECTION )) && export APO_COLLECTION="$_SAVED_APO_COLLECTION"
unset _SAVED_APO_VAULTS _SAVED_APO_NOTES_ROOT _SAVED_APO_INDEX _SAVED_APO_COLLECTION
unset _HAD_APO_VAULTS _HAD_APO_NOTES_ROOT _HAD_APO_INDEX _HAD_APO_COLLECTION

APO_ENGINE_BIN="${APO_ENGINE_BIN:-${SCRIPT_DIR}/engine/.venv/bin/apo-engine}"
WATCH_PID_DIR="${WATCH_PID_DIR:-${HOME}/.apo}"
WATCH_INTERVAL="${WATCH_INTERVAL:-30}"

mkdir -p "${WATCH_PID_DIR}"

PID_FILE="${WATCH_PID_DIR}/watch.pid"
LOG_FILE="${WATCH_PID_DIR}/watch.log"

info()    { printf '\033[34m[apo-watch]\033[0m %s\n' "$*"; }
success() { printf '\033[32m[apo-watch]\033[0m %s\n' "$*"; }
warn()    { printf '\033[33m[apo-watch]\033[0m %s\n' "$*"; }

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

cmd_start() {
  if is_running; then
    warn "Watcher already running (PID $(cat "$PID_FILE"))"
    return
  fi

  if [[ ! -d "${APO_NOTES_ROOT:-}" ]]; then
    warn "Vault does not exist: ${APO_NOTES_ROOT:-unset}"
    return 1
  fi

  local label="${APO_VAULTS:-${APO_NOTES_ROOT}}"
  info "Starting watcher for ${label} (interval ${WATCH_INTERVAL}s)..."

  # Double-fork into a new session so Cursor/agent shell teardown cannot
  # reap the watcher with the parent process group.
  local py="${APO_ENGINE_BIN%apo-engine}python"
  [[ -x "$py" ]] || py="python3"
  "$py" - "$APO_ENGINE_BIN" "$WATCH_INTERVAL" "$PID_FILE" "$LOG_FILE" <<'PY'
import os, sys, time
from pathlib import Path

engine, interval, pid_file, log_file = sys.argv[1:5]
if os.fork() > 0:
    # original parent: wait for pid file then exit
    for _ in range(50):
        p = Path(pid_file)
        if p.is_file() and p.read_text(encoding="utf-8").strip().isdigit():
            sys.exit(0)
        time.sleep(0.05)
    sys.stderr.write("watcher did not write pid file\n")
    sys.exit(1)
os.setsid()
if os.fork() > 0:
    os._exit(0)
# grandchild — session leader orphan
os.chdir("/")
Path(pid_file).write_text(str(os.getpid()) + "\n", encoding="utf-8")
log = open(log_file, "a", encoding="utf-8", buffering=1)
os.dup2(log.fileno(), 1)
os.dup2(log.fileno(), 2)
os.execve(engine, [engine, "watch", "--interval", str(interval)], os.environ)
PY

  # Give the grandchild a moment; pid file should already exist.
  sleep 0.2
  if is_running; then
    success "Watcher started (PID $(cat "$PID_FILE")) → $LOG_FILE"
  else
    warn "Watcher failed to start — see $LOG_FILE"
    return 1
  fi
}

cmd_stop() {
  if ! is_running; then
    warn "Watcher is not running."
    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
    return
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null && success "Watcher stopped (PID $pid)" || warn "Could not kill PID $pid"
  rm -f "$PID_FILE"
}

cmd_status() {
  if is_running; then
    success "Watcher RUNNING (PID $(cat "$PID_FILE"))"
    info "  log: $LOG_FILE"
    info "  vault: ${APO_NOTES_ROOT:-unset}"
    [[ -n "${APO_VAULTS:-}" ]] && info "  APO_VAULTS: ${APO_VAULTS}"
  else
    warn "Watcher STOPPED"
  fi
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status)  cmd_status ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
