#!/usr/bin/env bash
# Place the return-only vault(request={action: "project", mode: "index"}) body into the
# Cursor always-on apo-desk rule. Re-run after ~/.apo/desk.yaml or vault contract changes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="$HOME/.cursor/rules/apo-desk.mdc"
mkdir -p "$(dirname "$OUT")"

BODY=$(engine/.venv/bin/apo-engine desk-project --mode index | python3 -c "
import json, sys
print(json.load(sys.stdin)['body'], end='')
")

{
  echo "---"
  echo "description: Apo multi-vault desk (compact index)"
  echo "alwaysApply: true"
  echo "---"
  echo
  printf '%s' "$BODY"
} > "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT") bytes)"
