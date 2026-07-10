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
| **Engine** (`engine/`) | Chunk, embed, hybrid search. Ollama `bge-m3` (default) or `fastembed` CPU (`pip install -e '.[cpu]'`). One rebuildable `index.db`. Scores are rank-normalized RRF (1.0 = top hit; comparable within a result set, not across queries). |
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

**Cursor:** `apo` block in `~/.cursor/mcp.json` — full snippet in `docs/mcp-migration.md`. **Quit Cursor fully** (Cmd+Q) after changing MCP config.

**Claude Code:**

```bash
claude mcp add -s user apo -- \
  ~/Code/apo/engine/.venv/bin/python \
  ~/Code/apo/engine/mcp/server.py
# Set env in ~/.claude.json: APO_NOTES_ROOT, APO_INDEX, OLLAMA_KEEP_ALIVE=0
```

## Deployment environments

| Profile | Config | Use when |
|---------|--------|----------|
| **local-ollama** (default) | `config.env` | MacBook / Desma — Cursor & Claude Code via stdio MCP |
| **ecs-aws** (future) | `config.env.ecs` | Fargate — remote claude.ai via Laravel gateway + Bedrock embeddings |

Full ECS task layout (ALB, EFS, IAM, unified container, optional watcher service): **`docs/deploy-ecs.md`**.

Local profile is the day-to-day dev path. ECS profile targets OAuth remote MCP only — stdio MCP does not run in Fargate.

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

**Ignore rules:** glob patterns (relative to the vault root) from `engine/.indexignore` (or `APO_IGNORE`) **and** `<vault>/.indexignore` are merged at index time. Keep vault-specific exclusions (e.g. `system/agent-client/*`) in the vault-root file.

**Gateway only:** `gateway/.env` sets `APO_ENGINE_BIN` (read via `config/services.php`, so it survives `config:cache`) for the Laravel search tool shell-out.

## Background watcher (launchd)

Incremental reindex when vault files change (complements MCP `index=False` + `reindex_deferred`).

```bash
just watch-install    # LaunchAgent com.apo.watch — RunAtLoad + KeepAlive
just watch-status     # manual watcher via watch.sh
tail -f ~/.apo/watch-launchd.log
```

Requires **Ollama** running (`just ollama` or brew services). The watcher waits up to 120s for Ollama at startup.

Manual control: `bash watch.sh {start|stop|restart|status}` · uninstall: `just watch-uninstall`

When retiring legacy memsearch: `launchctl bootout gui/$(id -u)/com.example.memsearch-watch`

## Current status

- [x] Engine: chunk · embed (Ollama bge-m3) · **hybrid retrieval (sqlite-vec dense + FTS5 BM25, RRF)** · incremental · path post-filter
- [x] **MCP: memsearch-compatible surface (19 tools)** — `engine/mcp/server.py`; Cursor cutover documented in `docs/mcp-migration.md`
- [x] Gateway: `laravel/mcp` + `search-notes-tool`, verified over stdio JSON-RPC
- [x] Remote OAuth: Passport + `Mcp::oauthRoutes()` — discovery, DCR, bearer-protected `/mcp/apo`, verified local
- [x] 2026-07-10 fitness-review fixes: true chunk heading levels · committed purge of deleted notes · boundary-aware folder filter · single patch module · single-vault registry (multi-vault façade removed) · rank-consistent scores · non-blocking async I/O · engine core test suite (25 tests, no Ollama needed)
- [ ] Deploy for claude.ai (Caddy/TLS or **ECS** per `docs/deploy-ecs.md`) · SCIM · wiki routes · Quartz static tier — **blocked on Known gaps below**
- [ ] ECS: Dockerfile · Bedrock embed backend · Terraform module

## Known gaps (from the 2026-07-10 fitness review)

Both of these gate the claude.ai deployment — close them before exposing the gateway publicly:

1. **Privacy boundary is advisory.** Employer-mixed paths (`private/*`, `private/work-*`) are excluded only by optional query-time globs; nothing stops a remote client from searching the whole vault. Fix: split the vault (north star), or enforce a server-side exclude list in `SearchNotesTool` that clients cannot override.
2. **No token-scope enforcement.** `/mcp/apo` sits behind `auth:api` only — any Passport token of any scope reaches the MCP endpoint. Fix: register an `mcp:use` scope and add the `scopes:mcp:use` middleware in `routes/ai.php`.

Minor residuals (accepted at personal scale): `find_notes` / `backlinks` scan the vault synchronously per call; the gateway has no runtime verification on machines without `composer` (`vendor/` not installed); gateway test suite is still the Laravel stubs.

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
