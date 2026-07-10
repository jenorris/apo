# Apo

Personal knowledge-base gateway. Laravel **MCP** surface + **memsearch-compatible** Python MCP over a local semantic-search **engine** (sqlite-vec + Ollama). Indexes a markdown (PARA/OKF Obsidian) vault.

## Architecture

```
agent (Claude Code / Cursor / claude.ai)
      │  MCP (memsearch-compatible tools)
      ▼
engine/mcp/server.py   FastMCP — drop-in for legacy memsearch MCP
      │  apo-engine CLI + sqlite-vec hybrid (dense + FTS5 RRF)
      ▼
Ollama  bge-m3 (local Metal)     index: engine/index.db (rebuildable)
      │
gateway/ (optional)  Laravel + laravel/mcp — OAuth remote path; search-only today
```

- **Engine** (`engine/`): Python. Embeddings via **Ollama/bge-m3** (default) or `fastembed` CPU (`pip install -e '.[cpu]'`). One `index.db`.
- **MCP** (`engine/mcp/server.py`): **19 tools** — same names as `~/Code/ai-tools/memsearch/mcp/server.py` for seamless Cursor swap. See `docs/mcp-migration.md`.
- **Gateway** (`gateway/`): Laravel 13 + Passport OAuth (remote claude.ai path).

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
just setup
just ollama          # start serve with unload-after-use
ollama pull bge-m3   # once
just index
just search "trash pickup day"
just mcp             # stdio MCP (memsearch-compatible)
```

Copy `.env` from repo root or set `APO_NOTES_ROOT`. Migration: `docs/mcp-migration.md`.

## Configuration (engine env)

| Var | Default | Meaning |
|-----|---------|---------|
| `APO_NOTES_ROOT` | `~/Notes` | vault to index |
| `APO_INDEX` | `engine/index.db` | index file |
| `APO_EMBED_BACKEND` | `ollama` | `ollama` (GPU) or `fastembed` (CPU) |
| `APO_MODEL` | `bge-m3` | embedding model |
| `APO_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |

`gateway/.env` sets `APO_ENGINE_BIN` to the engine console script.

## Current status

- [x] Engine: chunk · embed (Ollama bge-m3) · **hybrid retrieval (sqlite-vec dense + FTS5 BM25, RRF)** · incremental · ACL post-filter
- [x] Gateway: `laravel/mcp` server + `search-notes-tool`, verified over stdio JSON-RPC
- [x] Remote OAuth: Passport + `Mcp::oauthRoutes()` — discovery, dynamic client registration, bearer-protected `/mcp/apo` (401 + `WWW-Authenticate`), verified local
- [ ] Deploy for claude.ai (Caddy/TLS + public origin + allowlisted users) · SCIM · wiki routes · Quartz static tier

## Remote MCP (claude.ai)

The gateway implements the MCP OAuth flow end-to-end:

```
claude.ai ─▶ GET /.well-known/oauth-protected-resource/mcp/apo   (from the 401 WWW-Authenticate)
          ─▶ GET /.well-known/oauth-authorization-server         (issuer, endpoints, S256, scope mcp:use)
          ─▶ POST /oauth/register                                (dynamic client registration → client_id)
          ─▶ GET  /oauth/authorize                               (Passport consent; user login)
          ─▶ POST /oauth/token                                   (authorization_code + PKCE → bearer)
          ─▶ POST /mcp/apo   Authorization: Bearer …             (scope mcp:use)
```

To go live: front with Caddy (TLS) on a public hostname (or Tailscale Funnel), set `APP_URL` to that origin (it becomes the OAuth issuer), and create allowlisted user accounts. Then add it in claude.ai → Settings → Connectors as a custom server.
