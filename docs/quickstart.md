# Apo quickstart (local engine)

Markdown vault as source of truth + hybrid search MCP for Cursor / Claude Code.

**You need:** macOS or Linux, Homebrew (or equivalent), a folder of `.md` notes, ~3 GB free while the embedding model is loaded.

This guide is **local engine only** — one machine, one vault root. No cloud gateway.

## 1. Install

```bash
git clone <apo-repo-url> ~/Code/apo   # or your preferred path
cd ~/Code/apo
brew install ollama just
cp config.env.example .env            # if present; else cp config.env .env
```

Edit `.env` (or `config.env`):

| Variable | Set to |
|----------|--------|
| `APO_NOTES_ROOT` | Absolute path to **your** markdown vault |
| `APO_INDEX` | e.g. `~/Code/apo/engine/index.db` (local to this clone) |
| `OLLAMA_KEEP_ALIVE` | `5m` while working; `0` to unload the model when idle |

```bash
just setup
just ollama && ollama pull bge-m3
just index
just search "a phrase you know is in your vault"
```

Optional background indexer (recommended):

```bash
just watch-install
just watch-status
```

## 2. Register MCP — Cursor

Add an `apo` block to `~/.cursor/mcp.json` (merge into existing `mcpServers`):

```json
"apo": {
  "command": "/ABSOLUTE/PATH/TO/apo/engine/.venv/bin/python",
  "args": ["/ABSOLUTE/PATH/TO/apo/engine/mcp/server.py"],
  "cwd": "/ABSOLUTE/PATH/TO/apo/engine/mcp",
  "env": {
    "APO_NOTES_ROOT": "/ABSOLUTE/PATH/TO/YOUR/VAULT",
    "APO_INDEX": "/ABSOLUTE/PATH/TO/apo/engine/index.db",
    "APO_EMBED_BACKEND": "ollama",
    "APO_OLLAMA_URL": "http://127.0.0.1:11434",
    "OLLAMA_KEEP_ALIVE": "5m"
  }
}
```

**Quit Cursor fully** (Cmd+Q on macOS) and reopen. MCP subprocesses do not reliably hot-reload.

## 3. Register MCP — Claude Code

```bash
claude mcp add -s user apo -- \
  /ABSOLUTE/PATH/TO/apo/engine/.venv/bin/python \
  /ABSOLUTE/PATH/TO/apo/engine/mcp/server.py
```

Ensure the same env vars as above are visible to that process (`~/.claude.json` or shell profile).

## 4. Verify

```bash
cd ~/Code/apo && just inspect    # expect ~16 tools
```

In the agent, run Apo `memory_status` — expect `root_exists: true`, watcher optionally running, index ok.

## 5. Agent onboard (important)

Install gets the engine running. **Persistent write habits** should match *your* vault.

1. Open your vault as the agent workspace.
2. **Existing structure:** paste [`onboard-prompt.md`](./onboard-prompt.md) (infer → propose → approve).
3. **Empty vault / want a preset:** pick an optional profile under [`profiles/`](./profiles/) (PARA life OS, llm-wiki research, …), scaffold, *then* run the onboard prompt.
4. Review drafts; approve before anything is written.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| 0 Apo tools in Cursor | Full quit/reopen; confirm `apo` key (not a stale rename); Ollama up with `bge-m3` |
| Empty / stale search | `just index`; confirm `APO_NOTES_ROOT`; `just watch-status` |
| Writes don’t show in search | Ensure watcher installed; after large batches call `reindex_deferred` (or wait for debounce/poll) |

Health one-liner:

```bash
curl -sf http://127.0.0.1:11434/api/tags | grep -q bge-m3 && \
  test -f "${APO_INDEX:-$HOME/Code/apo/engine/index.db}" && echo "Apo OK"
```

## What Apo is (one paragraph)

Agents search and update **your markdown files**. The index is disposable. Prefer surgical writes (`append_note` / `patch_note`) over full-file rewrites. Folders and frontmatter schemas are **yours**; Apo stays path + YAML agnostic.
