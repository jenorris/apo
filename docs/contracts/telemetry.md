# Telemetry contract

**Template:** [telemetry-contract.schema.yaml](./telemetry-contract.schema.yaml)

Opt-in per vault. When present under `<vault>/system/contracts/`, Apo adjusts **tool-use metrics** ingest and **agent read** surfaces for that vault.

## What it controls

| Knob | Effect |
|------|--------|
| `enabled` | Master switch (`false` → no `record_call` rows for this vault) |
| `privacy.allow.paths` | `none` (flags only) · `hash_only` · `vault_relative` · `absolute` |
| `privacy.allow.headings` / `chunk_hash` | Section-level optimization nodes when paths allowed |
| `agent_access.expose_paths` | Lean MCP `session_stats` includes `by_path` rollups |
| `agent_access.scope` | `session` (conversation_id) · `collection` · `desk` (operator) |

## Default without contract

Flags and timings only — **no note paths**. Matches pre-telemetry-contract behavior.

## Meta PKB (recommended)

Ship `paths: vault_relative` + `expose_paths: true` so agents and `session_stats` can identify hot notes and folder-scoping gaps without storing bodies or search queries.

## Agent tools

| Tool | Lean | Role |
|------|------|------|
| `session_stats` | yes | Session-scoped rollups (+ `by_path` when contract allows) |
| `active_session` | yes | Read `~/.apo/active-session.json` (Cursor hook) |
| `tool_stats` | admin | Desk-wide operator rollups |

## Hooks (optional)

- `sessionStart` → write `active-session.json`
- `beforeMCPExecution` → `export APO_CONVERSATION_ID=…`

Example shell: [scripts/cursor-telemetry-hooks.example.sh](../../scripts/cursor-telemetry-hooks.example.sh)

See [Cursor hooks](https://cursor.com/docs/hooks) and [cursor-otel-hook](https://github.com/LangGuard-AI/cursor-otel-hook) for full trace export.
