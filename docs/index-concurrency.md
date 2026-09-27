# Index concurrency — single-writer architecture

Apo uses one sqlite-vec file (`index.db`) shared by the MCP server (Cursor/Claude) and the
launchd watcher. SQLite WAL allows concurrent readers but **only one writer** at a time.

## Design (2026-07-13)

| Process | Reads `index.db` | Writes `index.db` |
|---------|------------------|-------------------|
| **MCP** (`apo-mcp`) | Yes — search, read, backlinks | Only via `apo_admin(reindex)` **when no watcher is live** (see below) |
| **CLI** (`apo-engine index`) | Yes | Only via `--inline`/`--force-inline`, or automatically **when no watcher is live** |
| **Watcher** (`apo-engine watch`) | Yes | **Sole writer whenever it is running** |

A live watcher is still the single source of truth for concurrent writes: MCP and the CLI
never write `index.db` *while a watcher is running* — they only signal it. The only case
either writes directly is when **no watcher is running at all**, so there is no second writer
to race.

MCP enqueues work under `~/.apo/`:

| File | Purpose |
|------|---------|
| `deferred-{collection}.json` | Absolute paths to index |
| `purge-{collection}.json` | Absolute paths to purge from index |
| `rebuild-{collection}.json` | Full vault scan signal (`{"force": bool}`) — cleared the instant the watcher picks it up |
| `rebuild-busy-{collection}.json` | Present only while the watcher is *actively running* the signaled rebuild (scoped marker for `ops.reindex(wait=True)` polling — not a general health file) |
| `wake-{collection}` | Touch file — watcher processes queues immediately |

Write enqueue already wakes the watcher (`wake-*`). Lean desk hides `reindex_deferred`
(`apo_admin` → `memory_status`); use it only for diagnostics. Otherwise the watcher picks up
queues on fsevents or the periodic hash scan (`WATCH_INTERVAL`, default 30s).

## Watcher-aware `index` / `reindex`

`ops.reindex(vault="", mode="flush", force=False, wait=False, timeout=30.0)` is the one place
this logic lives — both `apo-engine index` and the MCP `apo_admin(reindex)` capability call it,
so "reindex" means the same thing everywhere:

* `mode="flush"` — wake the watcher to drain its already-enqueued deferred index queue.
  Does nothing if no watcher is running (nothing will ever consume that queue).
* `mode="rebuild"` — full vault scan (`force=True` drops and rebuilds from scratch, same as
  `core.index_vault(rebuild=...)`).
  * Watcher live → **signals** it (`deferred.signal_rebuild` + wake), the same mechanism the
    admin handler always used. No second writer.
  * No watcher live → runs the scan **inline**, in this process, immediately — no
    concurrent-writer risk since nothing else can be writing `index.db`.
* `wait=True` (rebuild + live watcher only) blocks up to `timeout` seconds, polling the
  `rebuild-{collection}.json` (pending) and `rebuild-busy-{collection}.json` (watcher currently
  inside `index_vault`) markers until both clear or the timeout elapses. `completed: false` in
  the result means the rebuild is still running in the watcher, not that it failed — retry with
  a longer timeout or check back later; it is not cancelled.

`apo-engine index` defaults to `--wait` (30s timeout, `--timeout` to change it; `--no-wait` to
fire-and-forget). `--inline` forces today's direct `core.index_vault` in-process behavior and is
**refused if a watcher is detected running** (a second writer); `--force-inline` overrides that
refusal. `--limit` (smoke-test only) implies `--inline` since a watcher-signaled rebuild can't
honor a note-count cap.

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
| `APO_DB_TIMEOUT` | `30` | SQLite busy-handler for the **writer** (seconds) |
| `APO_DB_READ_TIMEOUT` | `3` | MCP / read-only busy-handler — fail fast (seconds) |
| `APO_WAL_LIMIT_BYTES` | `67108864` (64MiB) | Soft WAL cap; passive checkpoint on finalize; truncate after watch cycle when exceeded |
| `APO_EMBED_FAIL_QUARANTINE` | `5` | Consecutive embed drops of the same file hash before quarantine (no vectors) |
| `APO_EMBED_FAIL_BACKOFF` | `30` | Seconds between embed-drop retries while below quarantine threshold |
| `APO_WATCH_LOCK_BACKOFF_START` | `2` | Initial watcher sleep after sqlite lock/busy (seconds) |
| `APO_WATCH_LOCK_BACKOFF_MAX` | `60` | Cap on watcher lock-error exponential backoff (seconds) |
| `WATCH_INTERVAL` | `30` | Periodic full hash scan (seconds) |
| `APO_WATCH_EVENTS` | `1` | fsevents via `watchdog` (`0` = poll-only) |
| `APO_WATCH_DEBOUNCE` | `2` | Quiet-seconds before embedding a touched path (FS + deferred queue) |
| `APO_SEARCH_CANDIDATES` | `24` | Floor for hybrid vec/FTS candidate pool (`max(k*4, this)`) |
| `APO_EXCLUDE_CANDIDATE_FLOOR` | `500` | Unscoped+exclude: FTS pool floor only (not vec0 `k`) |
| `APO_EXCLUDE_VEC_K` | `64` | Unscoped+exclude: hard cap on global vec0 KNN neighbors |
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
- Unscoped+exclude: FTS widens to ``APO_EXCLUDE_CANDIDATE_FLOOR`` (500); vec0 ``k``
  stays under ``APO_EXCLUDE_VEC_K`` (64) — do not inherit the FTS floor
- Optional ONNX (`fastembed`) can cut query embed to ~20ms but hurt ticket-ID ranking —
  switch backend/model ⇒ `just reindex`

## Recovery

```bash
just watch-status
tail -f ~/.apo/watch-launchd.log
just index          # watcher-aware: signals + waits (30s) if a watcher is live, else runs inline
```

`apo-engine index --rebuild` (what `just reindex` runs) is watcher-aware the same way — it no
longer races the watcher as a second writer. Use `--no-wait` to fire-and-forget instead of
blocking, or `--inline`/`--force-inline` to force today's old direct-write behavior (refused
with a live watcher unless you pass `--force-inline`).

### Lock / WAL runaway (last resort)

If the watcher spins on `database is locked` and `index-*.db-wal` grows without bound:

```bash
just watch-stop
sqlite3 ~/.apo/index-<vault>.db "PRAGMA wal_checkpoint(TRUNCATE);"
just watch-install   # or just watch-start after merging a fixed engine
```

Normal operation: the watcher calls `writer_reset()` on sqlite errors, exponential backoff on
lock/busy, `PRAGMA wal_checkpoint(PASSIVE)` after each successful index finalize, and
`wal_checkpoint(TRUNCATE)` when the `-wal` file exceeds `APO_WAL_LIMIT_BYTES`. MCP readers use
`mode=ro` with the short read timeout and return `{ok: false, error: "index_busy"}` instead of
hanging until Cursor kills stdio.

Embed drops retry with backoff; after `APO_EMBED_FAIL_QUARANTINE` consecutive failures of the
same content hash the file is stamped `embed_quarantined` (no vectors) until the hash changes.
Git idle pull is skipped while the index is in lock-backoff or over the WAL limit.

A manual full index no longer needs the watcher stopped first — `apo-engine index` (no
`--inline`) signals the watcher and waits, rather than writing `index.db` itself, whenever a
watcher is live. Stopping the watcher first is only needed if you deliberately want
`--force-inline`.
