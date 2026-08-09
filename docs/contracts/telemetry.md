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
| **OTLP spans → collector** | **Session / tool traces** — emitted by the engine itself (`store.backend: otlp`), fanned out to Jaeger + otlp-mcp |

RPC: `POST /v1/vault` with `action=stats` (+ optional `days=`). `POST /v1/telemetry` and `POST /v1/session_stats` are **deprecated** (delegate to `stats` or return `bad_action`).

### Current state (2026-08-09)

The engine **does** now export OTLP directly — see [OTLP export](#otlp-export). This supersedes the earlier note that OTLP existed only as an external Cursor-hook pipeline on another machine; that pipeline is not what runs here.

## Store backends

| `store.backend` | Path / URI |
|-----------------|------------|
| `embedded` (default) | `~/.apo/metrics.duckdb` |
| `otlp` | `store.endpoint` (default `http://localhost:4318/v1/traces`) |
| `both` | Fan-out — DuckDB **and** OTLP. Cutover state; reads resolve to DuckDB |
| `none` | No recording (`APO_TOOL_METRICS=0`) |

Legacy `local` / desk-metrics HTTP ingest is **retired in v0.5.0** — maps to `embedded`, as does the historical spelling `duckdb`. Env override: `APO_METRICS_BACKEND=embedded|otlp|both|none`.

## OTLP export

One span per `tools/call`, named `apo.tool/{tool}`, kind `SERVER`, status `ERROR` when the call failed.

Install: `pip install 'apo-engine[otlp]'`. Without it the backend logs once and no-ops — telemetry never breaks a tool call.

| Span attribute | Source |
|----------------|--------|
| `apo.tool`, `apo.ok`, `apo.error`, `apo.error_shape` | call outcome |
| `apo.vault_id`, `apo.collection`, `apo.version` | binding + engine semver |
| `apo.folder_set`, `apo.expected_mtime_set`, `apo.fields_set`, `apo.used_alias`, `apo.ops_count` | habit flags |
| `apo.req_bytes`, `apo.resp_bytes` | payload sizes |
| `apo.note_path`, `apo.path_hash`, `apo.heading`, `apo.chunk_hash` | **contract-governed** — same `privacy.allow.paths` rules as DuckDB |
| `apo.session_id` | session identity (below) |

Duration is span timing, not an attribute. `TelemetryPolicy` is applied *before* the backend sees the event, so privacy rules hold identically across backends.

**Why spans, not Prometheus metrics.** This is per-event forensic data — "what did session X do, in what order, failing how". Prometheus stores aggregates and cannot answer that, and a session id as a label is unbounded cardinality. Aggregates are derived downstream by the collector's `spanmetrics` connector, so the engine instruments once and both shapes exist.

**Shutdown.** Export is batched (1s) so it stays off the request path, and is flushed from a SIGTERM/SIGINT handler as well as `atexit` — MCP clients signal stdio servers rather than asking them to stop, and `atexit` does not run on a signal.

## Session identity (MCP wire)

Per-call attribution — safe for **multiple concurrent sessions** and **remote Apo** (gateway/RPC):

| Transport | Field | Example |
|-----------|-------|---------|
| MCP `_meta` | `apo/conversation_id` | Standard request meta (preferred for HTTP MCP) |
| MCP / RPC args | `_apo.conversation_id` | Stripped before tool validation |
| RPC body | `_apo` or top-level `conversation_id` | Laravel gateway |
| Legacy | `APO_CONVERSATION_ID` env, `active-session.json` | Local stdio fallback only |
| Process | generated at startup, `APO_SESSION_ID` overrides | **Last resort** — see below |

Engine middleware binds ids in a **request contextvar** (not a global file) before metrics ingest.

**Process fallback.** Clients are expected to supply a conversation id, but Claude Code sends none and the only shipped injector is a Cursor hook — which left `conversation_id` NULL on 100% of recorded calls and made per-session analysis impossible. Under stdio, Apo is spawned as one subprocess per client session, so the process *is* the session and a process-scoped id is correct there, not merely convenient. Every source above takes precedence.

⚠️ The equivalence does **not** hold for a long-lived HTTP/SSE server shared by multiple clients — all of their calls would collapse into one apparent session. Supply real ids via `_meta` on those transports.

## Hooks (Cursor local)

- **`preToolUse`** → inject `_apo.conversation_id` into Apo MCP tool args (`updated_input`)
- **`sessionStart`** → write `active-session.json` for legacy fallbacks
- **OTel export** → Workbench `harness/observability/` (Jaeger UI at `just telemetry ui`)

Example shell: [scripts/cursor-telemetry-hooks.example.sh](../../scripts/cursor-telemetry-hooks.example.sh) · installed: `~/.cursor/hooks/apo-telemetry.sh`

See [Cursor hooks](https://cursor.com/docs/hooks) and [cursor-otel-hook](https://github.com/LangGuard-AI/cursor-otel-hook) for full trace export.
