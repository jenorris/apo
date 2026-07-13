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
Ollama bge-m3 (or fastembed)     index: engine/index.db
```

| Layer | Role |
|-------|------|
| **Engine** (`engine/`) | Chunk, embed, hybrid BM25 + vector search |
| **MCP** | 17 tools — agents never talk to sqlite directly for writes |
| **Watcher** | FS events + deferred queue → reindex |

## Quickstart

See **[docs/quickstart.md](docs/quickstart.md)** (install, MCP registration, verify).

Then run the **[vault onboard prompt](docs/onboard-prompt.md)** against *your* notes so agent instructions match your layout — not a canned template.

```bash
brew install ollama just
cp config.env.example .env   # set APO_NOTES_ROOT
just setup
just ollama && ollama pull bge-m3
just index
just search "something you know is in the vault"
```

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
| `APO_INGEST_DIR` | `resources/wiki` | Default `ingest_uri` destination (vault-relative) |
| `APO_EMBED_BACKEND` | `ollama` | `ollama` or `fastembed` |
| `OLLAMA_KEEP_ALIVE` | `5m` | Keep embed model warm; `0` = unload when idle |
| `WATCH_INTERVAL` | `30` | Periodic mtime scan (seconds) |
| `APO_WATCH_DEBOUNCE` | `2` | Quiet seconds before re-embedding a path |

Tuning: [docs/index-concurrency.md](docs/index-concurrency.md).

## Background watcher

```bash
just watch-install
just watch-status
```

After pulling engine changes that touch watch/index code: `just setup && just watch-install`.

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
