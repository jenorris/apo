#!/usr/bin/env bash
# Place the return-only vault(action=project) body into the Claude Code
# apo-desk skill file. As of upstream #21 (2026-08-06), the engine/watcher
# no longer write host files themselves — the host is expected to place
# `body`. This is that placement for Claude Code specifically; re-run after
# ~/.apo/desk.yaml or any vault's system/contracts/ changes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="$HOME/.claude/skills/apo-desk/SKILL.md"
mkdir -p "$(dirname "$OUT")"

BODY=$(engine/.venv/bin/apo-engine desk-project | python3 -c "
import json, sys
print(json.load(sys.stdin)['body'], end='')
")

{
  echo "---"
  echo "name: apo-desk"
  echo "description: >-"
  echo "  Apo multi-vault desk policy (generated from vault merge). Vault table,"
  echo "  dual-write, citations, contract pointers. Use with mcp-apo for tool routing."
  echo "---"
  echo
  printf '%s' "$BODY"
} > "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT") bytes)"
