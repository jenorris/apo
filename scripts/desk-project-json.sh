#!/usr/bin/env bash
# Print vault(request={action: "project"}) JSON against an explicit vault registry.
# Shared by write-cursor-rule.sh / write-claude-skill.sh (placement scripts).
#
# Registry resolution (first wins):
#   1. --registry PATH / APO_DESK_REGISTRY — a vaults.json, i.e. the same file an
#      MCP host's APO_VAULTS would point at (Workbench: harness/mcp/vaults.workbench.json).
#      Discovery vars inherited from the shell or the repo .env (`just` dotenv-loads
#      it) are dropped so the render matches the registry the MCP server serves.
#   2. APO_VAULTS / APO_VAULT_PATHS / APO_COLLECTION_ROOT already in the environment
#      — used as-is, with a stderr note naming which one, so an inherited dev
#      profile is visible rather than silent.
#   3. Nothing set → error. Never falls through to the legacy single-root desk.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

registry="${APO_DESK_REGISTRY:-}"
mode="index"
while [ $# -gt 0 ]; do
  case "$1" in
    --registry) registry="$2"; shift 2 ;;
    --registry=*) registry="${1#--registry=}"; shift ;;
    --mode) mode="$2"; shift 2 ;;
    --mode=*) mode="${1#--mode=}"; shift ;;
    *) echo "usage: $0 [--registry vaults.json] [--mode index|full]" >&2; exit 2 ;;
  esac
done

eng="${APO_ENGINE_BIN:-engine/.venv/bin/apo-engine}"

if [ -n "$registry" ]; then
  if [ ! -f "$registry" ]; then
    echo "error: registry not found: $registry" >&2
    exit 1
  fi
  echo "registry: $registry (explicit)" >&2
  exec env -u APO_COLLECTION_ROOT -u APO_VAULT_PATHS -u APO_DEFAULT_VAULT \
           -u APO_NOTES_ROOT -u APO_INDEX -u APO_COLLECTION \
           APO_VAULTS="$registry" "$eng" desk-project --mode "$mode"
fi

for var in APO_VAULTS APO_VAULT_PATHS APO_COLLECTION_ROOT; do
  if [ -n "${!var:-}" ]; then
    echo "registry: $var=${!var} (inherited — pass --registry / set APO_DESK_REGISTRY to pin the MCP host's vaults.json)" >&2
    exec "$eng" desk-project --mode "$mode"
  fi
done

echo "error: no vault registry — pass --registry vaults.json, set APO_DESK_REGISTRY, or export APO_VAULT_PATHS / APO_COLLECTION_ROOT" >&2
exit 1
