# Index concurrency — single-writer architecture

Apo uses one sqlite-vec file (`index.db`) shared by the MCP server (Cursor/Claude) and the
launchd watcher. SQLite WAL allows concurrent readers but **only one writer** at a time.

## Design (2026-07-13)

| Process | Reads `index.db` | Writes `index.db` |
|---------|------------------|-------------------|
| **MCP** (`engine/mcp/server.py`) | Yes — search, read, backlinks | **Never** |
| **Watcher** (`apo-engine watch`) | Yes | **Sole writer** |

MCP enqueues work under `~/.apo/`:

| File | Purpose |
|------|---------|
| `deferred-{collection}.json` | Absolute paths to index |
| `purge-{collection}.json` | Absolute paths to purge from index |
| `rebuild-{collection}.json` | Full vault scan signal (`{"force": bool}`) |
| `wake-{collection}` | Touch file — watcher processes queues immediately |

Call `reindex_deferred()` after batch sweeps to touch `wake-*`; the watcher otherwise picks
up queues on fsevents or the periodic hash scan (`WATCH_INTERVAL`, default 30s).

## Write transaction shape

Indexing embeds via Ollama **off-DB**, then opens a short SQLite transaction for inserts:

1. Scan / delete stale chunks → commit
2. `embed()` (network — no DB handle held)
3. Insert vectors + FTS rows → commit

Idle vault scans no longer commit when nothing changed.

## Tuning

| Env | Default | Meaning |
|-----|---------|---------|
| `APO_DB_TIMEOUT` | `30` | SQLite busy-handler (seconds) |
| `WATCH_INTERVAL` | `30` | Periodic full hash scan (seconds) |
| `APO_WATCH_EVENTS` | `1` | fsevents via `watchdog` (`0` = poll-only) |

## Recovery

```bash
just watch-status
tail -f ~/.apo/watch-launchd.log
just index          # manual full index from CLI (also writes DB — stop watcher first if lock errors)
```

If MCP and watcher contend during a manual `just index`, stop the watcher first:

```bash
just watch-stop && just index && just watch-start
```
