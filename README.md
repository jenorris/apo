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
      │  ~/.apo/deferred-*.json + wake signals
      ▼
apo-watch (launchd)    sole SQLite writer
      │  fsevents + queue consumer + periodic hash scan
      ▼
Ollama  bge-m3 (local Metal)     index: engine/index.db (rebuildable)
```

**Single-writer indexing:** MCP never writes `index.db`. Writes queue paths under `~/.apo/`; `apo-engine watch` (launchd) consumes queues, embeds off-DB, and commits short SQLite transactions.

| Layer | Location | Role |
|-------|----------|------|
| **Engine** (`engine/`) | this repo | Chunk, embed, hybrid search, incremental index |
| **MCP** (`engine/mcp/server.py`) | this repo | 19 tools — search/read + enqueue index work |
| **Watcher** (`apo-engine watch`) | launchd | fsevents, deferred queue, sole index writer |
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

**Cursor:** `apo` block in `~/.cursor/mcp.json` — see `docs/mcp-migration.md`. **Quit Cursor fully** (Cmd+Q) after MCP config changes.

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
| `WATCH_INTERVAL` | `30` | Vault hash-scan interval (fsevents handle hot paths) |
| `APO_DB_TIMEOUT` | `30` | SQLite busy-handler seconds |
| `APO_WATCH_EVENTS` | `1` | Use fsevents (`0` = poll-only) |

\*MacBook profile in `config.env` uses `~/Notes/MyVault`.

## Background watcher (launchd)

```bash
just watch-install    # com.apo.watch
tail -f ~/.apo/watch-launchd.log
```

Concurrency model: [docs/index-concurrency.md](docs/index-concurrency.md)

## Current status

- [x] Engine: hybrid retrieval, incremental index, 19-tool MCP
- [x] MacBook cutover (Ollama bge-m3, launchd watcher)
- [ ] Bedrock embed backend (for ECS — see apo-enterprise `docs/deploy-ecs.md`)

## Enterprise / remote MCP

OAuth, SCIM, wiki routes, and deploy docs: **`github.com/jenorris/apo-enterprise`**.

Work machine: **engine only**. Grid: engine + enterprise for family/home vaults.
