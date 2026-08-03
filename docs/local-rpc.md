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

Arg names match MCP where possible. Prefer **`limit`** / **`where`** (aliases `top_k` / `filters` still accepted on search/filter).

### Read

| Method | Path | Body | Notes |
|--------|------|------|-------|
| GET | `/health` | — | `{ok, service, version, vaults}` |
| GET/POST | `/v1/stats` | `{vault?}` | index stats |
| POST | `/v1/search` | `{query, limit?, top_k?, folder?, exclude?, snippet_chars?, vault?, no_hybrid?}` | hybrid search — prefer `limit` |
| POST | `/v1/read` | `{path, heading?, start_line?, end_line?, max_chars?, raw?, vault?}` | `frontmatter` sidecar + body `content` (raw=true → byte-exact) |
| POST | `/v1/filter` | `{where, folder?, limit?, offset?, fields?, vault?}` | frontmatter catalog — prefer `where`; `fields` projects FM keys |
| POST | `/v1/expand` | `{chunk_hash, scope?, vault?}` | section or chunk |
| POST | `/v1/backlinks` | `{path, limit?, vault?}` | wiki-link backlinks |
| POST | `/v1/history` | `{limit?, folder?, path?, vault?}` | browse by mtime, or file git log when `path` + git contract |
| POST | `/v1/git_sync` | `{action, message?, vault?}` | git contract sync: `status` \| `run` \| `pull` \| `clear_block` |

### Write

Writes update markdown on disk and **enqueue** reindex for `apo-engine watch` (same single-writer policy as stdio MCP). Prefer `append` / `patch` over full `write`. There is no `index=` flag — enqueue is always on.

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/v1/write` | `{path, content, expected_mtime?, vault?}` | create/overwrite only (`append` key → `append_removed`) |
| POST | `/v1/append` | `{text, path?, heading?, chunk_hash?, position?, create?, expected_mtime?, vault?}` | surgical append (`path` or `chunk_hash` required) |
| POST | `/v1/patch` | `{path, ops, strict?, dry_run?, verbose?, expected_mtime?, vault?}` | batch mutators |
| POST | `/v1/patch_notes` | `{items, strict?, dry_run?, verbose?, vault?}` | multi-path patch batch (`items: [{path, ops, expected_mtime?}]`, max 20) |
| POST | `/v1/place` | `{src, dst, overwrite?, fields?, expected_mtime?, vault?}` | move if src in vault; else copy host `.md` |
| POST | `/v1/move` | same as `/v1/place` | **alias** — prefer `/v1/place` |
| POST | `/v1/send` | same as `/v1/place` | **alias** for host→vault copy — prefer `/v1/place` |
| POST | `/v1/delete` | `{path, vault?}` | delete + purge queue |

All responses are JSON with `ok: true|false`. Error bodies include `error` + `message`. HTTP: `404` not found, `409` stale_write / destination_exists / path_mismatch, `403` forbidden_src / use_move_note / too_large.

## Env

| Var | Default | Meaning |
|-----|---------|---------|
| `APO_RPC_HOST` | `127.0.0.1` | Bind address |
| `APO_RPC_PORT` | `8765` | Bind port |
| `APO_RPC_SOCKET` | (empty) | If set, Unix socket path (overrides host/port) |
| `APO_RPC_TOKEN` | (empty) | Optional bearer; empty = no auth |
| `APO_SEND_ALLOW_ROOTS` | `$HOME` | Colon-separated roots `place_note` may copy host `src` from |
| `APO_SEND_MAX_BYTES` | `5242880` | Max host file size for `place_note` copies |

Vault / index / Ollama settings are the same as the rest of the engine (`APO_NOTES_ROOT`, `APO_INDEX`, …).

## Laravel (`apo-enterprise`)

Set `APO_RPC_URL=http://127.0.0.1:8765` and optional `APO_RPC_TOKEN`. MCP tools call the RPC client. Run the watcher so writes become searchable.

**Authz:** Path ACL (`NotePolicy`) lives in the Laravel gateway and is **not implemented yet** — it depends on Passport identity → vault roles and path-prefix rules. The engine RPC does not enforce per-user path ACL; bind loopback + token for desk pilots until the gateway policy layer exists.
