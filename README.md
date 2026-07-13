# Apo — engine + MCP

Personal knowledge-base **engine**: memsearch-compatible Python MCP over sqlite-vec + Ollama, indexing a markdown (PARA/OKF Obsidian) vault.

**Enterprise gateway** (OAuth, multi-user, family/home deploy) lives in a separate repo: **[apo-enterprise](https://github.com/jenorris/apo-enterprise)**. Not required for local engine use.

Clean-room personal build — not derived from employer code. See Meta vault `projects/apo-kb-gateway/personal-build-boundaries`.

## Architecture

```
agent (Claude Code / Cursor)
      │  MCP — 19 tools (read index.db; enqueue writes)
      ▼
engine/mcp/server.py   FastMCP
      │  enqueue_index / enqueue_many → ~/.apo/deferred-*.json
      ▼
apo-watch (launchd)    sole SQLite writer + reused writer connection
      │  fsevents → debounce → batch embed (reuse unchanged chunks)
      │  + deferred queue + mtime-first periodic scan
      ▼
Ollama  bge-m3 (local Metal)     index: engine/index.db (rebuildable)
```

**Single-writer indexing:** MCP never writes `index.db`. Writes queue paths under `~/.apo/`; `apo-engine watch` (launchd) consumes queues, embeds off-DB, and commits short SQLite transactions. The watcher keeps a process-local SQLite connection across commits.

**Indexing path (hot):**

1. FS events and MCP deferred paths share a per-path quiet window (`APO_WATCH_DEBOUNCE`, default **2s**)
2. Ready paths go through `index_files` — **one** Ollama batch; unchanged chunk bodies reuse stored vectors
3. Unchanged file hash → skip embed entirely
4. Every `WATCH_INTERVAL` (~30s) scan **stats mtime first**; read+hash only when mtime moved
5. Purge and rebuild stay immediate (not debounced)

**MCP writes:** `enqueue_index` / `enqueue_many` return the updated queue set (no second `load_index_queue` re-read). Prefer `enqueue_many(..., wake=True)` for multi-note sweeps.

**Search latency (measured, model warm):** unique query miss ~120–150ms (Ollama floor); identical query within `APO_QUERY_EMBED_TTL` ~15ms. FTS overlaps the embed call; candidate pool floor `APO_SEARCH_CANDIDATES` (default 24). Idle vault scan ~80–130ms on ~1.9k notes (was ~460ms before mtime skip).

| Layer | Location | Role |
|-------|----------|------|
| **Engine** (`engine/`) | this repo | Chunk, embed, hybrid search, incremental index |
| **MCP** (`engine/mcp/server.py`) | this repo | 19 tools — search/read + enqueue index work |
| **Watcher** (`apo-engine watch`) | launchd | fsevents, debounce, batch index, sole DB writer |
| **Enterprise** | `jenorris/apo-enterprise` | Passport OAuth, remote claude.ai, family KB |

## MacBook Air (32 GB) — local Ollama

| Resource | Typical |
|----------|---------|
| `bge-m3` on disk | ~1.2 GB |
| RAM while model loaded | ~2–3 GB |
| After idle (`OLLAMA_KEEP_ALIVE=0`) | ~100 MB (daemon only) |

No Docker/GPU container required — Ollama uses Apple Metal natively.

## Quickstart

```bash
brew install ollama just
cp config.env .env          # edit APO_NOTES_ROOT if needed
just setup
just ollama && ollama pull bge-m3
just index
just search "trash pickup day"
just mcp
```

**Cursor:** `apo` block in `~/.cursor/mcp.json` — see `docs/mcp-migration.md`. **Quit Cursor fully** (Cmd+Q) after MCP config or engine code changes.

**Claude Code:**

```bash
claude mcp add -s user apo -- \
  ~/Code/apo/engine/.venv/bin/python \
  ~/Code/apo/engine/mcp/server.py
```

## Configuration

| Var | Default | Meaning |
|-----|---------|---------|
| `APO_NOTES_ROOT` | `~/Notes`* | Vault to index |
| `APO_INDEX` | `engine/index.db` | sqlite-vec index |
| `APO_EMBED_BACKEND` | `ollama` | `ollama` or `fastembed` (CPU) |
| `OLLAMA_KEEP_ALIVE` | `0` | Unload model after each request |
| `MEMSEARCH_COLLECTION` | `notes_global` | Deferred queue namespace |
| `WATCH_INTERVAL` | `30` | Vault mtime scan interval (fsevents handle hot paths) |
| `APO_WATCH_DEBOUNCE` | `2` | Quiet seconds before embedding a touched path |
| `APO_SEARCH_CANDIDATES` | `24` | Hybrid candidate-pool floor (`max(k*4, this)`) |
| `APO_QUERY_EMBED_TTL` | `120` | Cache identical query embeds (seconds; `0` off) |
| `APO_QUERY_EMBED_CACHE` | `64` | Max cached query vectors |
| `APO_DB_TIMEOUT` | `30` | SQLite busy-handler seconds |
| `APO_WATCH_EVENTS` | `1` | Use fsevents (`0` = poll-only) |

\*MacBook profile in `config.env` uses `~/Notes/MyVault`.

Full concurrency + tuning notes: [docs/index-concurrency.md](docs/index-concurrency.md).

## Background watcher (launchd)

```bash
just watch-install    # com.apo.watch (sole index writer)
just watch-status
tail -f ~/.apo/watch-launchd.log
```

After pulling engine changes that touch `watch.py` / `core.py` / `deferred.py`, re-run `just setup && just watch-install` from `~/Code/apo` so launchd reloads the new code. Confirm the log line includes `debounce 2.0s`.

## Current status

- [x] Engine: hybrid retrieval, incremental index, 19-tool MCP
- [x] MacBook cutover (Ollama bge-m3, launchd watcher)
- [x] Single-writer index + watch debounce / path coalescing
- [x] Query-embed TTL cache + overlapped FTS for search latency
- [x] mtime vault scan, partial chunk reuse, batch embed, `enqueue_many`, writer conn reuse
- [ ] Bedrock embed backend (for ECS — see apo-enterprise `docs/deploy-ecs.md`)

## Enterprise / remote MCP

OAuth, SCIM, wiki routes, and deploy docs: **`github.com/jenorris/apo-enterprise`**.

Work machine: **engine only**. Grid: engine + enterprise for family/home vaults.
