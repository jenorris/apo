# Apo — personal KB engine. Task surface for engine (python) only.
# `just --list` is the self-documenting manifest.

set dotenv-load := true

eng := "engine/.venv/bin/apo-engine"
mcp_py := "engine/.venv/bin/python"
mcp_srv := "engine/mcp/server.py"

default:
    @just --list


# One-time setup: engine venv (editable + MCP + test deps).
setup:
    cd engine && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e '.[mcp,dev]'
    @echo "ready — run 'just ollama' then 'just index' then 'just mcp'"

# Engine test suite (hermetic — never touches ~/.apo or your vault).
# PYTHONPATH=src so git worktrees hit this tree's sources (venv may be
# editable-installed against the primary clone).
test *ARGS:
    cd engine && PYTHONPATH=src .venv/bin/python -m pytest tests {{ARGS}}

ollama:
    @command -v ollama >/dev/null 2>&1 || { echo "ollama not found on PATH — install it first (e.g. brew install ollama)"; exit 1; }
    @curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && echo "ollama already up" || (OLLAMA_KEEP_ALIVE=0 ollama serve &)
    @sleep 1 && ollama list | head -5 || true

index *ARGS:
    {{eng}} index {{ARGS}}

reindex:
    {{eng}} index --rebuild

search *ARGS:
    {{eng}} search {{ARGS}}

# Labeled search-quality eval (hit@k / MRR). File format: docs/examples/search-eval.example.yaml
search-eval *ARGS:
    {{eng}} search-eval {{ARGS}}

stats:
    {{eng}} stats

tool-stats *ARGS:
    {{eng}} tool-stats {{ARGS}}

# Render apo-desk text from ~/.apo/desk.yaml + vault contracts (JSON to stdout).
# Watcher logs when desk/contracts change — re-run desk-project to render.
desk-project *ARGS:
    {{eng}} desk-project {{ARGS}}

# Return-only as of #21 — place the body into the Claude Code skill file
# (the host now owns placement; nothing does this automatically).
desk-project-claude:
    ./scripts/write-claude-skill.sh

# Contract-gated vault batch tools (OKF lint/fix/…). See vault-tools/README.md
vault-tools *ARGS:
    just --justfile "{{ justfile_directory() }}/vault-tools/justfile" {{ARGS}}

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
    # Needs Node (npx) + ripgrep (rg). Prefer `just tool-list` if those are missing.
    npx -y @modelcontextprotocol/inspector --cli {{mcp_py}} {{mcp_srv}} --env APO_NOTES_ROOT=${APO_NOTES_ROOT} --method tools/list | rg '"name"' | wc -l

# Pure-Python MCP tool count (no Node). Expect 15 tools.
tool-list:
    {{mcp_py}} -c 'import asyncio; from importlib.util import spec_from_file_location, module_from_spec; from pathlib import Path; p=Path("engine/mcp/server.py"); s=spec_from_file_location("apo_mcp_server", p); m=module_from_spec(s); s.loader.exec_module(m); tools=asyncio.run(m.mcp.list_tools()); print(len(tools)); print("\n".join(sorted(t.name for t in tools)))'