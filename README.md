# Apo

Personal knowledge-base gateway: **memsearch-compatible Python MCP** over a local semantic-search **engine** (sqlite-vec + Ollama), indexing a markdown (PARA/OKF Obsidian) vault. Optional **Laravel gateway** for OAuth remote access (claude.ai).

Clean-room personal build — not derived from employer code. Architecture diverges from upstream memsearch (no Milvus/Docker/plugins). See vault note `projects/apo-kb-gateway/personal-build-boundaries` in Meta.

## Architecture

```
agent (Claude Code / Cursor)
      │  MCP — 19 memsearch-compatible tools (primary)
      ▼
engine/mcp/server.py   FastMCP — drop-in for legacy memsearch MCP
      │  apo-engine CLI + sqlite-vec hybrid (dense + FTS5 RRF)
      ▼
Ollama  bge-m3 (local Metal)     index: engine/index.db (rebuildable)

gateway/ (optional)  Laravel + laravel/mcp — OAuth remote path; search-only today
```

| Layer | Role |
|-------|------|
| **Engine** (`engine/`) | Chunk, embed, hybrid search. Ollama `bge-m3` (default) or `fastembed` CPU (`pip install -e '.[cpu]'`). One rebuildable `index.db`. |
| **MCP** (`engine/mcp/server.py`) | **19 tools** — same names as `~/Code/ai-tools/memsearch/mcp/server.py`. Cursor/Claude Code entry point. See `docs/mcp-migration.md`. |
| **Gateway** (`gateway/`) | Laravel 13 + Passport OAuth for remote claude.ai (search-only via `ApoServer` today). |

## MacBook Air (32 GB) — local Ollama

| Resource | Typical |
|----------|---------|
| `bge-m3` on disk | ~1.2 GB |
| RAM while model loaded | ~2–3 GB |
| After idle (`OLLAMA_KEEP_ALIVE=0`) | ~100 MB (daemon only) |

No Docker/GPU container required — Ollama uses Apple Metal natively.

## Quickstart

```bash
# prereqs: python3, ollama, just
brew install ollama just
cp config.env .env          # edit APO_NOTES_ROOT if needed (.env is gitignored)
just setup
just ollama                 # start serve with unload-after-use
ollama pull bge-m3          # once
just index
just search "trash pickup day"
just search-personal "..."  # exclude employer-mixed paths at query time
just mcp                    # stdio MCP (memsearch-compatible)
```

**Cursor:** repoint the `memsearch` block in `~/.cursor/mcp.json` — full snippet in `docs/mcp-migration.md`. **Quit Cursor fully** (Cmd+Q) after changing MCP config.

**Claude Code:**

```bash
claude mcp add -s user memsearch -- \
  ~/Code/apo/engine/.venv/bin/python \
  ~/Code/apo/engine/mcp/server.py
# Set env in claude.json or shell: APO_NOTES_ROOT, APO_INDEX, OLLAMA_KEEP_ALIVE=0
```

## Configuration

Copy `config.env` → `.env` at repo root (justfile loads `.env` automatically). Or export vars in your shell / MCP host env block.

| Var | Default | Meaning |
|-----|---------|---------|
| `APO_NOTES_ROOT` | `~/Notes`* | Vault to index |
| `APO_INDEX` | `engine/index.db` | sqlite-vec index file |
| `APO_EMBED_BACKEND` | `ollama` | `ollama` (Metal) or `fastembed` (CPU) |
| `APO_MODEL` | `bge-m3` | Embedding model |
| `APO_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama HTTP endpoint |
| `OLLAMA_KEEP_ALIVE` | `0` | Unload model after each request (saves RAM) |

**Memsearch drop-in aliases** (MCP server accepts either name):

| Var | Meaning |
|-----|---------|
| `MEMSEARCH_NOTES_ROOT` | Alias for `APO_NOTES_ROOT` |
| `MEMSEARCH_COLLECTION` | Deferred-queue namespace (`notes_global`) |
| `MEMSEARCH_INGEST_DIR` | Relative ingest target (`resources/wiki`) |

\*Jeremy's MacBook profile in `config.env` uses `~/Notes/MyVault`.

**Gateway only:** `gateway/.env` sets `APO_ENGINE_BIN` for the Laravel search tool shell-out.

## Current status

- [x] Engine: chunk · embed (Ollama bge-m3) · **hybrid retrieval (sqlite-vec dense + FTS5 BM25, RRF)** · incremental · path post-filter
- [x] **MCP: memsearch-compatible surface (19 tools)** — `engine/mcp/server.py`; Cursor cutover documented in `docs/mcp-migration.md`
- [x] Gateway: `laravel/mcp` + `search-notes-tool`, verified over stdio JSON-RPC
- [x] Remote OAuth: Passport + `Mcp::oauthRoutes()` — discovery, DCR, bearer-protected `/mcp/apo`, verified local
- [ ] Deploy for claude.ai (Caddy/TLS + public origin + allowlisted users) · SCIM · wiki routes · Quartz static tier

## Remote MCP (claude.ai)

The **gateway** (not the Python MCP server) implements OAuth for remote agents:

```
claude.ai ─▶ GET /.well-known/oauth-protected-resource/mcp/apo
          ─▶ GET /.well-known/oauth-authorization-server
          ─▶ POST /oauth/register
          ─▶ GET  /oauth/authorize
          ─▶ POST /oauth/token
          ─▶ POST /mcp/apo   Authorization: Bearer …
```

To go live: front with Caddy (TLS) on a public hostname (or Tailscale Funnel), set `APP_URL` to that origin, create allowlisted user accounts, add in claude.ai → Settings → Connectors.
