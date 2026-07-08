# Apo — personal KB gateway. Task surface for both the engine (python) and gateway (laravel).
# `just --list` is the self-documenting manifest; point CLAUDE.md/AGENTS.md here.

eng := "engine/.venv/bin/apo-engine"

default:
    @just --list

# One-time setup: engine venv (editable) + gateway composer deps.
setup:
    cd engine && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e .
    cd gateway && composer install
    @echo "ready — run 'just index' then 'just mcp'"

# Build / update the vector index (Ollama bge-m3 on GPU; incremental — only changed notes re-embed).
index *ARGS:
    {{eng}} index {{ARGS}}

# Full rebuild from scratch (needed if you change SIFT model/dim).
reindex:
    {{eng}} index --rebuild

# CLI semantic search. Usage: just search "how did I fix the milvus ENOSPC hang"
search *ARGS:
    {{eng}} search {{ARGS}}

# Search minus employer-mixed paths (north-star vault split, at query time).
search-personal QUERY:
    {{eng}} search {{QUERY}} --exclude 'private/*' 'private/work-*' '**/threads/*'

# Index stats (notes, chunks, model, backend, dim).
stats:
    {{eng}} stats

# Watch the vault and reindex on change.
watch:
    {{eng}} watch

# Start the Laravel MCP gateway over stdio (register in Claude Code / Cursor).
mcp:
    cd gateway && php artisan mcp:start apo

# Serve the gateway over HTTP — MCP endpoint at http://127.0.0.1:8000/mcp/apo
# (front with Caddy + oauth2-proxy / Passport for claude.ai).
serve:
    cd gateway && php artisan serve

# Interactive MCP inspector for debugging tools.
inspect:
    cd gateway && php artisan mcp:inspector apo
