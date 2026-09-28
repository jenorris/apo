#!/usr/bin/env bash
# Place the return-only vault(request={action: "project", mode: "index"}) body into the
# Cursor always-on apo-desk rule. Re-run after ~/.apo/desk.yaml or vault contract changes.
# Registry: --registry vaults.json / APO_DESK_REGISTRY (see scripts/desk-project-json.sh).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="$HOME/.cursor/rules/apo-desk.mdc"
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
  echo "description: Apo multi-vault desk (compact index)"
  echo "alwaysApply: true"
  echo "---"
  echo
  printf '%s' "$BODY"
} > "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes, sources $SOURCES)"
