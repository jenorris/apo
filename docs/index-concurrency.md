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
| `APO_WATCH_DEBOUNCE` | `2` | Quiet-seconds before embedding a touched path (FS + deferred queue) |
| `APO_SEARCH_CANDIDATES` | `24` | Floor for hybrid vec/FTS candidate pool (`max(k*4, this)`) |
| `APO_QUERY_EMBED_TTL` | `120` | Seconds to reuse identical query embeddings (`0` disables) |
| `APO_QUERY_EMBED_CACHE` | `64` | Max cached query vectors |

## Debounce / coalescing (2026-07-13)

Rapid Obsidian saves and MCP `enqueue_index` bursts used to re-embed the same path many
times per second. The watcher now:

1. Merges fsevents **and** drained `deferred-*.json` paths into a per-path timer
2. Indexes a path only after `APO_WATCH_DEBOUNCE` seconds without another touch
3. Skips Ollama entirely in `index_file` when the content hash is unchanged

Purge and rebuild signals stay immediate (not debounced).

## Search latency notes

Cold hybrid search is dominated by **Ollama `bge-m3` query embed** (~120–150ms on M4 Air
with the model loaded). SQLite vec/FTS is typically &lt;15ms warm. To go lower:

- Repeat identical queries hit the embed TTL cache (near-instant)
- FTS runs overlapped with the embed call
- Candidate pool floor is 24 (was hard-coded 50)
- Further gains need a warmer keep-alive / smaller embed model — product tradeoffs, not
  free SQLite wins

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
