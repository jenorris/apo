#!/usr/bin/env bash
# Example Cursor hooks for Apo session telemetry (optional).
# Install: copy snippets into ~/.cursor/hooks.json — see docs/contracts/telemetry.md
set -euo pipefail

APO_ACTIVE="${APO_ACTIVE_SESSION_FILE:-$HOME/.apo/active-session.json}"

# sessionStart — record active Cursor session for active_session MCP tool
session_start() {
  mkdir -p "$(dirname "$APO_ACTIVE")"
  python3 - <<'PY'
import json, os, sys, time
payload = json.load(sys.stdin)
out = {
    "conversation_id": payload.get("conversation_id") or payload.get("session_id"),
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "workspace_roots": payload.get("workspace_roots") or [],
}
path = os.path.expanduser(os.environ.get("APO_ACTIVE_SESSION_FILE", "~/.apo/active-session.json"))
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
PY
}

# beforeMCPExecution — stamp conversation id for DuckDB session_stats attribution
before_mcp() {
  python3 - <<'PY'
import json, os, sys
payload = json.load(sys.stdin)
cid = payload.get("conversation_id") or payload.get("session_id") or ""
if cid:
    print(f"export APO_CONVERSATION_ID={cid!r}")
PY
}

case "${1:-}" in
  sessionStart) session_start ;;
  beforeMCPExecution) before_mcp ;;
  *) echo "usage: $0 sessionStart|beforeMCPExecution" >&2; exit 1 ;;
esac
