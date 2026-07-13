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
set -a
# shellcheck source=config.env
source "${SCRIPT_DIR}/config.env"
set +a

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

  info "Starting watcher for ${APO_NOTES_ROOT} (interval ${WATCH_INTERVAL}s)..."

  nohup "${APO_ENGINE_BIN}" watch --interval "${WATCH_INTERVAL}" \
    >> "$LOG_FILE" 2>&1 &
  local pid=$!
  disown "$pid"
  echo "$pid" > "$PID_FILE"

  success "Watcher started (PID $pid) → $LOG_FILE"
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
