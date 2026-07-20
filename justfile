# Apo — personal KB engine. Task surface for engine (python) only.
# `just --list` is the self-documenting manifest.

set dotenv-load := true

eng := "engine/.venv/bin/apo-engine"
mcp_py := "engine/.venv/bin/python"
mcp_srv := "engine/mcp/server.py"

default:
    @just --list


# One-time setup: engine venv (editable + MCP).
setup:
    cd engine && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e '.[mcp]'
    @echo "ready — run 'just ollama' then 'just index' then 'just mcp'"

ollama:
    @curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && echo "ollama already up" || (OLLAMA_KEEP_ALIVE=0 /opt/homebrew/opt/ollama/bin/ollama serve &)
    @sleep 1 && ollama list | head -5 || true

index *ARGS:
    {{eng}} index {{ARGS}}

reindex:
    {{eng}} index --rebuild

search *ARGS:
    {{eng}} search {{ARGS}}

stats:
    {{eng}} stats

# Local JSON HTTP RPC for gateways (default http://127.0.0.1:8765).
rpc *ARGS:
    {{eng}} serve {{ARGS}}

watch-fg:
    {{eng}} watch

watch-start:
    bash watch.sh start

watch-stop:
    bash watch.sh stop

watch-status:
    bash watch.sh status

watch-install:
    chmod +x launchd-watch.sh watch.sh
    mkdir -p ~/Library/LaunchAgents
    sed -e "s|__APO_DIR__|$(pwd)|g" -e "s|__HOME__|$HOME|g" \
        com.apo.watch.plist.template > ~/Library/LaunchAgents/com.apo.watch.plist
    launchctl bootout "gui/$(id -u)/com.apo.watch" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.apo.watch.plist
    @echo "installed — log: ~/.apo/watch-launchd.log"

watch-uninstall:
    launchctl bootout "gui/$(id -u)/com.apo.watch" 2>/dev/null || true
    rm -f ~/Library/LaunchAgents/com.apo.watch.plist
    @echo "uninstalled"

mcp:
    {{mcp_py}} {{mcp_srv}}

inspect:
    npx -y @modelcontextprotocol/inspector --cli {{mcp_py}} {{mcp_srv}} --env APO_NOTES_ROOT=${APO_NOTES_ROOT} --method tools/list | rg '"name"' | wc -l
