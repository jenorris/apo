#!/usr/bin/env bash
# Place the compact desk index into the Claude Code apo-desk skill file.
# Full per-vault policy: vault(action=project, vaults=["<id>"]) on demand.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="$HOME/.claude/skills/apo-desk/SKILL.md"
mkdir -p "$(dirname "$OUT")"

BODY=$(engine/.venv/bin/apo-engine desk-project --mode index | python3 -c "
import json, sys
print(json.load(sys.stdin)['body'], end='')
")

{
  echo "---"
  echo "name: apo-desk"
  echo "description: >-"
  echo "  Apo desk compact index (vault table + project directive)."
  echo "  Call vault(action=project, vaults=[id]) before writing. Use with mcp-apo."
  echo "---"
  echo
  printf '%s' "$BODY"
} > "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT") bytes)"
