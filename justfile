# Apo — personal KB gateway. Task surface for both the engine (python) and gateway (laravel).
# `just --list` is the self-documenting manifest; point CLAUDE.md/AGENTS.md here.

set dotenv-load := true

eng := "engine/.venv/bin/apo-engine"
mcp_py := "engine/.venv/bin/python"
mcp_srv := "engine/mcp/server.py"

default:
    @just --list

# One-time setup: engine venv (editable + MCP) + gateway composer deps.
setup:
    cd engine && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e '.[mcp]'
    @if command -v composer >/dev/null 2>&1; then cd gateway && composer install; else echo "skip gateway (composer not installed)"; fi
    @echo "ready — run 'just ollama' then 'just index' then 'just mcp'"

# Start Ollama with unload-after-use (background). Idempotent if already running.
ollama:
    @curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && echo "ollama already up" || (OLLAMA_KEEP_ALIVE=0 /opt/homebrew/opt/ollama/bin/ollama serve &)
    @sleep 1 && ollama list | head -5 || true

# Build / update the vector index (Ollama bge-m3; incremental — only changed notes re-embed).
index *ARGS:
    {{eng}} index {{ARGS}}

# Full rebuild from scratch (needed if you change embedding model/dim).
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

# Watch the vault and reindex on change (foreground — Ctrl-C to stop).
watch-fg:
    {{eng}} watch

# Manual background watcher (PID + log under ~/.apo/).
watch-start:
    bash watch.sh start

watch-stop:
    bash watch.sh stop

watch-status:
    bash watch.sh status

# Install LaunchAgent (login auto-start, KeepAlive). Requires Ollama reachable.
watch-install:
    chmod +x launchd-watch.sh watch.sh
    mkdir -p ~/Library/LaunchAgents
    cp com.apo.watch.plist ~/Library/LaunchAgents/
    launchctl bootout "gui/$(id -u)/com.apo.watch" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.apo.watch.plist
    @echo "installed — log: ~/.apo/watch-launchd.log"

watch-uninstall:
    launchctl bootout "gui/$(id -u)/com.apo.watch" 2>/dev/null || true
    rm -f ~/Library/LaunchAgents/com.apo.watch.plist
    @echo "uninstalled"

# memsearch-compatible MCP over stdio (drop-in for ~/.cursor/mcp.json "memsearch" block).
mcp:
    {{mcp_py}} {{mcp_srv}}

# Start the Laravel MCP gateway over stdio (search-only until gateway tools land).
mcp-gateway:
    cd gateway && php artisan mcp:start apo

# Serve the gateway over HTTP — MCP endpoint at http://127.0.0.1:8000/mcp/apo
serve:
    cd gateway && php artisan serve

# Interactive MCP inspector for debugging tools.
inspect:
    npx -y @modelcontextprotocol/inspector --cli {{mcp_py}} {{mcp_srv}} --env MEMSEARCH_NOTES_ROOT=${APO_NOTES_ROOT} --method tools/list | rg '"name"' | wc -l
