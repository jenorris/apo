#!/usr/bin/env bash
# Place the compact desk index into the Claude Code apo-desk skill file.
# Full per-vault policy: vault(request={action: "project", vaults: ["<id>"]}) on demand.
# Registry: --registry vaults.json / APO_DESK_REGISTRY (see scripts/desk-project-json.sh).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="$HOME/.claude/skills/apo-desk/SKILL.md"
mkdir -p "$(dirname "$OUT")"

JSON=$(./scripts/desk-project-json.sh --mode index "$@")
BODY=$(printf '%s' "$JSON" | python3 -c "
import json, sys
print(json.load(sys.stdin)['body'], end='')
")
SOURCES=$(printf '%s' "$JSON" | python3 -c "
import json, sys
print(json.load(sys.stdin).get('sources') or '-', end='')
")

{
  echo "---"
  echo "name: apo-desk"
  echo "description: >-"
  echo "  Apo desk compact index (vault table + project directive)."
  echo "  Call vault(request={action: project, vaults: [id]}) before writing. Use with mcp-apo."
  echo "---"
  echo
  printf '%s' "$BODY"
} > "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes, sources $SOURCES)"
