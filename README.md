# Apo

Local **markdown knowledge-base engine**: hybrid search (sqlite-vec + Ollama embeddings) and an MCP tool surface for Cursor / Claude Code. Files on disk are source of truth; the index is rebuildable.

## Architecture

```
agent (Cursor / Claude Code)
      │  MCP — search / read / surgical write (enqueue index)
      ▼
engine/mcp/server.py
      │  ~/.apo/deferred-*.json
      ▼
apo-engine watch (optional launchd/systemd)  — sole index.db writer
      ▼
fastembed ONNX bge-large (or Ollama)  index: engine/index.db
```

| Layer | Role |
|-------|------|
| **Engine** (`engine/`) | Chunk, embed, hybrid BM25 + vector search; caches frontmatter + a wikilink backlink graph alongside chunks |
| **MCP** | 15 tools (11 with `APO_MCP_LEAN=1`) — agents never talk to sqlite directly for writes |
| **Watcher** | FS events + deferred queue → reindex |

`filter_notes`, `backlinks`, and `recent_activity` are index-backed (query `files.frontmatter` / the `backlinks` table), not vault filesystem walks — they stay fast regardless of vault size.

## Quickstart

See **[docs/quickstart.md](docs/quickstart.md)** (install, MCP registration, verify).

Then run the **[vault onboard prompt](docs/onboard-prompt.md)** against *your* notes so agent instructions match your layout — not a canned template.

```bash
brew install ollama just
cp config.env.example .env   # set APO_NOTES_ROOT
just setup
just index
just search "something you know is in the vault"
```

Optional Metal/GPU embeddings: set `APO_EMBED_BACKEND=ollama`, `APO_MODEL=bge-m3`, then `just ollama && ollama pull bge-m3` and rebuild (`just reindex`). Vectors are not interchangeable across models.

**Cursor:** add the `apo` block from the quickstart doc to `~/.cursor/mcp.json`, then **fully quit and reopen** Cursor.

**Claude Code:**

```bash
claude mcp add -s user apo -- \
  /ABSOLUTE/PATH/TO/apo/engine/.venv/bin/python \
  /ABSOLUTE/PATH/TO/apo/engine/mcp/server.py
```

## Configuration

| Var | Default | Meaning |
|-----|---------|---------|
| `APO_NOTES_ROOT` | (set me) | Vault root to index |
| `APO_INDEX` | `engine/index.db` | sqlite-vec database |
| `APO_COLLECTION` | `notes_global` | Deferred-queue / runtime namespace |
| `APO_INGEST_DIR` | `resources/wiki` | Convention: wiki path for defuddle→`write_note` (advisory) |
| `APO_EMBED_BACKEND` | `fastembed` | `fastembed` (ONNX) or `ollama` (Metal/GPU) |
| `APO_MODEL` | `BAAI/bge-large-en-v1.5` | FastEmbed model id, or `bge-m3` under Ollama |
| `OLLAMA_KEEP_ALIVE` | `0` | Only matters for `ollama` backend; `5m` keeps model warm |
| `WATCH_INTERVAL` | `30` | Periodic mtime scan (seconds) |
| `APO_WATCH_DEBOUNCE` | `2` | Quiet seconds before re-embedding a path |

Tuning: [docs/index-concurrency.md](docs/index-concurrency.md).

**Troubleshooting embed failures (Ollama backend):** if `apo-engine index` throws `HTTP Error 500` from Ollama's `/api/embed`, check `ollama --version` before assuming it's vault content — older Ollama builds (seen: 0.21.1) can emit NaN for realistic-length embedding inputs while trivial strings still work fine. Upgrading (`curl -fsSL https://ollama.com/install.sh | sh`) has resolved this in practice. The indexer bisects failing batches to skip only genuinely poisoned chunks rather than aborting the whole reindex, but that's a safety net, not a fix — a systemically unhealthy backend will silently skip most of the vault.

**Desk default (2026-07-14):** ONNX `BAAI/bge-large-en-v1.5` via fastembed — warm query embeds ~20 ms on M4 Air (vs ~120–150 ms for Ollama `bge-m3`). Full vault rebuild required when switching models.

## Background watcher

```bash
just watch-install
just watch-status
```

After pulling engine changes that touch watch/index code: `just setup && just watch-install`.

Full rebuilds (`just reindex`) commit embeddings in batches with progress lines and clear
the backlinks table — safe to interrupt and restart without duplicating the graph.

With fsevents on, the watcher reconciles the full vault every `WATCH_RECONCILE_INTERVAL`
(default 300s); day-to-day indexing is event + deferred-queue driven.

## Docs map

| Doc | For |
|-----|-----|
| [docs/quickstart.md](docs/quickstart.md) | New install + MCP |
| [docs/onboard-prompt.md](docs/onboard-prompt.md) | Infer vault rules → propose agent persistent instructions |
| [docs/profiles/](docs/profiles/) | Optional presets (PARA, llm-wiki) — layout **and** behaviors |
| [docs/index-concurrency.md](docs/index-concurrency.md) | Indexer / latency internals |
| [docs/mcp-migration.md](docs/mcp-migration.md) | Legacy memsearch → Apo (maintainers) |

## Design notes

- Engine is **convention-agnostic**: vault-relative paths + YAML frontmatter only.
- Opinionated PARA/OKF/thread workflows are **optional vault policy**, not engine requirements.
- Prefer `append_note` / `patch_note` over full-file `write_note` for edits.
- Frontmatter and wikilinks are parsed once per index write and cached (`files.frontmatter`, the `backlinks` table) — catalog tools query sqlite, never the filesystem.
