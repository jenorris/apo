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

Write enqueue already wakes the watcher (`wake-*`). Lean desk hides `reindex_deferred`
(`apo_admin` → `memory_status`); use it only for diagnostics. Otherwise the watcher picks up
queues on fsevents or the periodic hash scan (`WATCH_INTERVAL`, default 30s).

## Read-after-write visibility bound

Because MCP never writes `index.db`, a successful write is **durable on disk immediately**
but is **not searchable** until the watcher indexes it. That delay used to be undocumented.
The bound is:

| Situation | Bound on scheduling delay |
|-----------|---------------------------|
| Watcher running, write enqueued with `wake` (**every write op does this**) | `APO_WATCH_DEBOUNCE` (default **2s**) |
| Watcher running, wake missed / fs-event only | `APO_WATCH_DEBOUNCE + WATCH_INTERVAL` (default **32s**) |
| Watcher not running | **Unbounded** — nothing consumes the queue |

This is a bound on **scheduling only**. `embed()` is a network call to Ollama whose duration
depends on batch size and model residency, and is not included.

> [!important] Do not sleep for `bound_seconds`
> A caller that needs read-after-write certainty must **poll for the content**, not sleep for
> the bound. The bound tells you when the watcher will *start*, not when the vector is queryable.

Query it at runtime:

```python
from apo_engine import ops
ops.index_visibility()
# {'watcher_running': True, 'bound_seconds': 2.0, 'path': 'wake',
#  'debounce_seconds': 2.0, 'poll_interval_seconds': 30.0,
#  'note': 'scheduling bound only; embed() time is additional'}
```

It is also reported by `apo_admin(action=invoke, name=memory_status)` under `index_visibility`,
alongside `watcher`. Successful writes already carry a `warning` when no watcher is detected.

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
4. **Batches** ready paths into one `embed()` call; reuses vectors for unchanged chunk bodies
5. Vault poll uses **mtime short-circuit** before read+hash (~1.9k notes)

Purge and rebuild signals stay immediate (not debounced).

MCP writes: `enqueue_index` / `enqueue_many` return the updated queue set (no second
`load_index_queue` re-read). Use `enqueue_many(..., wake=True)` for sweep coalescing.

## Search latency notes

Cold hybrid search is dominated by **Ollama `bge-m3` query embed** (~120–150ms on Apple
Silicon with the model loaded). SQLite vec/FTS is typically &lt;15ms warm. Identical-query
TTL cache ~15ms. To go lower:

- Repeat identical queries hit the embed TTL cache (near-instant)
- FTS runs overlapped with the embed call
- Candidate pool floor is 24 (was hard-coded 50)
- Optional ONNX (`fastembed`) can cut query embed to ~20ms but hurt ticket-ID ranking —
  switch backend/model ⇒ `just reindex`

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
