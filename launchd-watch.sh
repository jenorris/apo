#!/usr/bin/env bash
# launchd entry point for Apo vault watcher.
# Optional Ollama wait when APO_EMBED_BACKEND=ollama; then exec apo-engine watch.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
# shellcheck source=config.env
source "${SCRIPT_DIR}/config.env"
set +a

APO_ENGINE_BIN="${APO_ENGINE_BIN:-${SCRIPT_DIR}/engine/.venv/bin/apo-engine}"
WATCH_PID_DIR="${WATCH_PID_DIR:-${HOME}/.apo}"
OLLAMA_URL="${APO_OLLAMA_URL:-http://127.0.0.1:11434}"
WATCH_INTERVAL="${WATCH_INTERVAL:-30}"
EMBED_BACKEND="${APO_EMBED_BACKEND:-fastembed}"

mkdir -p "${WATCH_PID_DIR}"

if [[ ! -x "${APO_ENGINE_BIN}" ]]; then
  printf '[apo-watch] apo-engine not found: %s\n' "${APO_ENGINE_BIN}" >&2
  exit 1
fi

if [[ ! -d "${APO_NOTES_ROOT:-}" ]]; then
  printf '[apo-watch] vault missing: %s\n' "${APO_NOTES_ROOT:-unset}" >&2
  exit 1
fi

if [[ "${EMBED_BACKEND}" == "ollama" ]]; then
  OLLAMA_WAIT_SECS=120
  deadline=$((SECONDS + OLLAMA_WAIT_SECS))
  while (( SECONDS < deadline )); do
    if curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
      break
    fi
    sleep 3
  done
  if ! curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
    printf '[apo-watch] Ollama not reachable at %s — exiting (retry via KeepAlive)\n' \
      "${OLLAMA_URL}" >&2
    exit 1
  fi
fi

printf '[apo-watch] watching %s → %s (fsevents + %ss scan, embed=%s model=%s)\n' \
  "${APO_NOTES_ROOT}" "${APO_INDEX:-engine/index.db}" "${WATCH_INTERVAL}" \
  "${EMBED_BACKEND}" "${APO_MODEL:-}"

echo $$ > "${WATCH_PID_DIR}/watch.pid"
cd "${SCRIPT_DIR}"
exec "${APO_ENGINE_BIN}" watch --interval "${WATCH_INTERVAL}"
