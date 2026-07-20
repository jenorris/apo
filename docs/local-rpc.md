"""Docs for apo-engine local JSON RPC (`apo-engine serve`)."""

# Local RPC

Gateway and other non-stdio clients talk to the engine over **loopback HTTP** (or an optional Unix domain socket). The stdio MCP for Cursor/Claude Code is unchanged.

## Start

```bash
# from apo repo root (env from .env / config.env)
just rpc                 # apo-engine serve — http://127.0.0.1:8765
# or:
apo-engine serve --host 127.0.0.1 --port 8765
```

Optional shared secret (recommended when anything besides localhost clients exist on the host):

```bash
export APO_RPC_TOKEN=dev-secret
apo-engine serve
```

Clients send `Authorization: Bearer <token>` or `X-Apo-Token: <token>`.

## Endpoints

### Read

| Method | Path | Body | Notes |
|--------|------|------|-------|
| GET | `/health` | — | `{ok, service, version, vaults}` |
| GET/POST | `/v1/stats` | `{vault?}` | index stats |
| POST | `/v1/search` | `{query, top_k?, folder?, exclude?, snippet_chars?, vault?, no_hybrid?}` | hybrid search |
| POST | `/v1/read` | `{path, heading?, vault?}` | note body / section |
| POST | `/v1/filter` | `{where, folder?, limit?, vault?}` | frontmatter catalog |
| POST | `/v1/expand` | `{chunk_hash, scope?, vault?}` | section or chunk |
| POST | `/v1/backlinks` | `{path, limit?, vault?}` | wiki-link backlinks |
| POST | `/v1/recent` | `{limit?, folder?, vault?}` | recent notes |

### Write

Writes update markdown on disk and **enqueue** reindex for `apo-engine watch` (same single-writer policy as stdio MCP). Prefer `append` / `patch` over full `write`.

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/v1/write` | `{path, content, append?, expected_mtime?, vault?}` | create/overwrite (or raw tail if `append`) |
| POST | `/v1/append` | `{path, text, heading?, chunk_hash?, position?, create?, expected_mtime?, vault?}` | surgical append |
| POST | `/v1/patch` | `{path, ops, strict?, dry_run?, verbose?, expected_mtime?, vault?}` | batch mutators |
| POST | `/v1/move` | `{src, dst, overwrite?, vault?}` | atomic rename |
| POST | `/v1/delete` | `{path, vault?}` | delete + purge queue |

All responses are JSON with `ok: true|false`. Error bodies include `error` + `message`. HTTP: `404` not found, `409` stale_write / destination_exists / path_mismatch.

## Env

| Var | Default | Meaning |
|-----|---------|---------|
| `APO_RPC_HOST` | `127.0.0.1` | Bind address |
| `APO_RPC_PORT` | `8765` | Bind port |
| `APO_RPC_SOCKET` | (empty) | If set, Unix socket path (overrides host/port) |
| `APO_RPC_TOKEN` | (empty) | Optional bearer; empty = no auth |

Vault / index / Ollama settings are the same as the rest of the engine (`APO_NOTES_ROOT`, `APO_INDEX`, …).

## Laravel (`apo-enterprise`)

Set `APO_RPC_URL=http://127.0.0.1:8765` and optional `APO_RPC_TOKEN`. MCP tools call the RPC client. Run the watcher so writes become searchable.

**Authz:** Path ACL (`NotePolicy`) lives in the Laravel gateway and is **not implemented yet** — it depends on Passport identity → vault roles and path-prefix rules. The engine RPC does not enforce per-user path ACL; bind loopback + token for desk pilots until the gateway policy layer exists.
