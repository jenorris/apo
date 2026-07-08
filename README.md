# Apo

Personal knowledge-base gateway. A Laravel **MCP** surface over a local semantic-search **engine**, indexing a markdown (PARA/OKF Obsidian) vault. Embedded, GPU-embedded, no Docker.

This is the personal, clean-room realization of the `apo-kb-gateway` spec: **memsearch** (the engine) as the index, **Laravel MCP** as the agent surface, with the vault-split ACL lever available at query time.

## Architecture

```
agent (Claude Code / Cursor / claude.ai)
      │  MCP
      ▼
gateway/   Laravel + laravel/mcp   ── ApoServer → SearchNotesTool
      │  Process (shell)              (Passport/OAuth · SCIM · wiki routes = future modules)
      ▼
engine/    apo-engine (python)     ── chunk → embed → sqlite-vec KNN → path/ACL post-filter
      │  HTTP
      ▼
Ollama  bge-m3 (1024-dim) on the local GPU        index: one sqlite file, rebuildable
```

- **Engine** (`engine/`): pure-Python. Embeddings via **Ollama/bge-m3** on the GPU (no CUDA/cuDNN system deps — Ollama bundles it); `fastembed` CPU fallback via `pip install -e '.[cpu]'`. One `index.db` (`sqlite-vec`).
- **Gateway** (`gateway/`): Laravel 13 + `laravel/mcp`. `ApoServer` exposes `search-notes-tool`, which shells to `apo-engine search --json`. Ready for the Passport + SCIM + wiki modules from the spec.

## Provenance (clean-room)

Personal reimplementation from the author's own vault specification — **not** derived from any employer codebase. Architecture deliberately diverges from the original (embedded `sqlite-vec` + Ollama, not Milvus/Docker). All deps permissive OSS (sqlite-vec, fastembed/ONNX, Laravel, laravel/mcp; BGE embeddings MIT). Boundaries per `projects/apo-kb-gateway/personal-build-boundaries` in the vault; IP ownership is a counsel question, not asserted here.

## Quickstart

```bash
# prereqs: python3, ollama (running) with `ollama pull bge-m3`, php 8.5, composer, just
just setup
just index                       # embed the whole vault (GPU)
just search "trash pickup day"
just search-personal "..."       # minus employer-mixed paths
just mcp                         # stdio MCP for Claude Code / Cursor
```

Register with Claude Code:

```bash
claude mcp add apo -- php /home/jeremy/Code/apo/gateway/artisan mcp:start apo
```

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
