#!/usr/bin/env bash
# Fail if markdown docs show filter_notes({...}) without the where= kwarg.
# Valid: filter_notes(where={...}, ...) or filter_notes({}, ...) — empty predicate ok.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

bad=0
while IFS= read -r line; do
  # Skip lines that already use where=
  if [[ "$line" == *"filter_notes(where="* ]] || [[ "$line" == *"filter_notes({},"* ]]; then
    continue
  fi
  if [[ "$line" =~ filter_notes\(\{ ]]; then
    echo "bad filter_notes wire (missing where=): $line"
    bad=1
  fi
done < <(rg 'filter_notes\(\{' docs/ scripts/ --glob '*.md' 2>/dev/null || true)

if [[ "$bad" -ne 0 ]]; then
  exit 1
fi
echo "ok: filter_notes wire examples in docs/ and scripts/"
