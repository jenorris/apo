# Telemetry contract

**Template:** [telemetry-contract.schema.yaml](./telemetry-contract.schema.yaml)

Opt-in per vault. When present under `<vault>/system/contracts/`, Apo adjusts **tool-use metrics** ingest and habit KPI surfaces for that vault.

## What it controls

| Knob | Effect |
|------|--------|
| `enabled` | Master switch (`false` → no `record_call` rows for this vault) |
| `privacy.allow.paths` | `none` (flags only) · `hash_only` · `vault_relative` · `absolute` |
| `privacy.allow.headings` / `chunk_hash` | Section-level optimization nodes when paths allowed |
| `agent_access.expose_paths` | Habit rollups may include path-scoped flags when allowed |
| `agent_access.scope` | `session` (conversation_id) · `collection` · `desk` (operator) |
| `privacy.allow.dimensions` | Includes `apo_version` (engine semver per row) for cross-version burn-down |
| `efficiency` | KPI thresholds for `vault(action=stats)` tips |

## Default without contract

Flags and timings only — **no note paths**. Matches pre-telemetry-contract behavior.

## Meta PKB (recommended)

Ship `paths: vault_relative` + `expose_paths: true` so habit rollups can identify hot notes and folder-scoping gaps without storing bodies or search queries.

## Agent tools (v0.5.0+)

| Surface | Role |
|---------|------|
| **`vault(action=stats)`** | **Agent MCP** — habit KPI rollups (`folder_scoped_pct`, chunk-read ratio, validation tips) |
| **OTel hooks → Jaeger** | **Session / tool traces** — Cursor preToolUse/postToolUse via otlp-mcp (Workbench `harness/observability/`) |

RPC: `POST /v1/vault` with `action=stats` (+ optional `days=`). `POST /v1/telemetry` and `POST /v1/session_stats` are **deprecated** (delegate to `stats` or return `bad_action`).

## Store backends

| `store.backend` | Path / URI |
|-----------------|------------|
| `embedded` (default) | `~/.apo/metrics.duckdb` |
| `none` | No recording (`APO_TOOL_METRICS=0`) |

Legacy `local` / desk-metrics HTTP ingest is **retired in v0.5.0** — maps to `embedded`. Env override: `APO_METRICS_BACKEND=embedded|none`.

## Session identity (MCP wire)

Per-call attribution — safe for **multiple concurrent sessions** and **remote Apo** (gateway/RPC):

| Transport | Field | Example |
|-----------|-------|---------|
| MCP `_meta` | `apo/conversation_id` | Standard request meta (preferred for HTTP MCP) |
| MCP / RPC args | `_apo.conversation_id` | Stripped before tool validation |
| RPC body | `_apo` or top-level `conversation_id` | Laravel gateway |
| Legacy | `APO_CONVERSATION_ID` env, `active-session.json` | Local stdio fallback only |

Engine middleware binds ids in a **request contextvar** (not a global file) before metrics ingest.

## Hooks (Cursor local)

- **`preToolUse`** → inject `_apo.conversation_id` into Apo MCP tool args (`updated_input`)
- **`sessionStart`** → write `active-session.json` for legacy fallbacks
- **OTel export** → Workbench `harness/observability/` (Jaeger UI at `just telemetry ui`)

Example shell: [scripts/cursor-telemetry-hooks.example.sh](../../scripts/cursor-telemetry-hooks.example.sh) · installed: `~/.cursor/hooks/apo-telemetry.sh`

See [Cursor hooks](https://cursor.com/docs/hooks) and [cursor-otel-hook](https://github.com/LangGuard-AI/cursor-otel-hook) for full trace export.
