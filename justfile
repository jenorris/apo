# Apo — personal KB engine. Task surface for engine (python) only.
# `just --list` is the self-documenting manifest.

set dotenv-load := true

# PATH binaries from `uv tool install --editable "engine[mcp]"` — override via env
# if your install lives elsewhere. These recipes deliberately keep shelling out to
# apo-engine/apo-local/apo-mcp by their original names (not the unified `apo`/`apo
# engine ...` entry point added alongside them) so `just` keeps working immediately
# after a fresh `just setup`, before any manual re-registration of MCP/launchd/shell
# config — see README.md's "CLI entry points" table for the equivalent `apo`
# commands to reach for by hand (`just search` == `apo search` [apo-local backend],
# `just stats` == `apo engine stats`, etc).
eng := env_var_or_default("APO_ENGINE_BIN", "apo-engine")
local_bin := env_var_or_default("APO_LOCAL_BIN", "apo-local")
mcp_bin := env_var_or_default("APO_MCP_BIN", "apo-mcp")
# Dev-venv python — only for `inspect`/`tool-list`, which load the server module
# straight from source so they always reflect uncommitted local changes.
mcp_py := "engine/.venv/bin/python"
mcp_srv := "engine/src/apo_engine/mcp/server.py"

default:
    @just --list


# One-time setup: engine venv (editable + MCP + test deps) for dev/test, plus a
# PATH-installed copy (apo-engine, apo-mcp) for MCP hosts and the watcher.
setup:
    cd engine && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e '.[mcp,dev]'
    command -v uv >/dev/null 2>&1 || { echo "uv not found on PATH — install it first (e.g. brew install uv)"; exit 1; }
    uv tool install --editable "engine[mcp]"
    @echo "ready — run 'just ollama' then 'just index' then 'just mcp'"

# Re-sync the PATH-installed apo-engine/apo-mcp after engine dependency changes
# (source edits are picked up live via --editable; this is only for new deps).
tool-reinstall:
    uv tool install --editable "engine[mcp]" --reinstall

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

# apo-local's search (ops.search) — apo-engine's own duplicate `search` subcommand
# was removed (same backend, subset of flags); use apo-local's flags (--limit, not
# -k; --folder/--vaults/etc.) going forward.
search *ARGS:
    {{local_bin}} search {{ARGS}}

# Labeled search-quality eval (hit@k / MRR). File format: docs/examples/search-eval.example.yaml
search-eval *ARGS:
    {{eng}} search-eval {{ARGS}}

# Run every discovered search-eval fixture (docs/examples/ + ~/.apo/search-eval-*.yaml):
# fails loudly on stale `expect` paths (vault reorg silently zeroed a fixture) or a
# hit@k drop past --regress-threshold points from each fixture's <name>.baseline.json.
# First run per fixture: `just check-evals --write-baseline` to seed a baseline.
check-evals *ARGS:
    {{eng}} check-evals {{ARGS}}

stats:
    {{eng}} stats

# Render apo-desk text from ~/.apo/desk.yaml + vault contracts (JSON to stdout).
# Watcher logs when desk/contracts change — re-run desk-project to render.
desk-project *ARGS:
    {{eng}} desk-project {{ARGS}}

# Return-only as of #21 — place the body into the Claude Code skill file
# (the host now owns placement; nothing does this automatically).
desk-project-claude:
    ./scripts/write-claude-skill.sh

# Compact index → Cursor always-on apo-desk.mdc (~1.8KB).
desk-project-cursor:
    ./scripts/write-cursor-rule.sh

# Lint markdown for bad filter_notes({...}) wire examples (missing where=).
check-filter-notes-wire:
    ./scripts/check-filter-notes-wire.sh

# OKF bundle: validate | fix | init | export | ingest.
# `okf validate --profile okf` checks SPEC §11 conformance exactly;
# `--profile apo` (default) is the stricter house producer profile.
okf *ARGS:
    {{eng}} okf {{ARGS}}

# Contract-gated vault batch tools (index regen, linkify). See vault-tools/README.md
# OKF lint/fix/export there are shims over `just okf …`.
vault-tools *ARGS:
    just --justfile "{{ justfile_directory() }}/vault-tools/justfile" {{ARGS}}

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
    {{mcp_bin}}

# Shared long-lived MCP server over HTTP instead of one stdio subprocess per client
# (qmd's `qmd mcp --http` pattern) — foreground, Ctrl-C to stop. DNS-rebinding guard
# is on (FastMCP host_origin_protection="auto"); APO_MCP_ALLOWED_HOSTS /
# APO_MCP_ALLOWED_ORIGINS (comma-separated) add a non-loopback client if needed.
mcp-http:
    APO_MCP_TRANSPORT=http {{mcp_bin}}

# One-time pull + build of qmd's own query-expansion model (APO_QUERY_EXPAND=1).
# See docs/models/qmd-query-expansion.md.
setup-query-expand-model:
    ollama pull hf.co/tobil/qmd-query-expansion-1.7B-gguf:Q4_K_M
    ollama create apo-query-expand -f docs/models/qmd-query-expansion.Modelfile

inspect:
    # Needs Node (npx) + ripgrep (rg). Prefer `just tool-list` if those are missing.
    npx -y @modelcontextprotocol/inspector --cli {{mcp_py}} {{mcp_srv}} --env APO_NOTES_ROOT=${APO_NOTES_ROOT} --method tools/list | rg '"name"' | wc -l

# Pure-Python MCP tool count (no Node). Expect 15 tools.
tool-list:
    {{mcp_py}} -c 'import asyncio; from importlib.util import spec_from_file_location, module_from_spec; from pathlib import Path; p=Path("{{mcp_srv}}"); s=spec_from_file_location("apo_mcp_server", p); m=module_from_spec(s); s.loader.exec_module(m); tools=asyncio.run(m.mcp.list_tools()); print(len(tools)); print("\n".join(sorted(t.name for t in tools)))'