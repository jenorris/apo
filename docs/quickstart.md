# Apo quickstart

Get a local Apo engine indexing **your** Markdown vault and talking to Cursor or Claude Code over MCP.

**You need:** macOS or Linux, Homebrew (or equivalent), [Ollama](https://ollama.com), a folder of `.md` notes, ~3 GB free while `bge-m3` is loaded.

This guide is **local engine only** — one machine, one vault root. Default embeddings require a running Ollama daemon.

## 1. Install

```bash
git clone <apo-repo-url> ~/Code/apo   # or your preferred path
cd ~/Code/apo
brew install ollama just
cp config.env.example .env
```

Edit `.env`:

| Variable | Set to |
|----------|--------|
| `APO_NOTES_ROOT` | Absolute path to **your** markdown vault |
| `APO_INDEX` | e.g. `/ABSOLUTE/PATH/TO/apo/engine/index.db` (local to this clone) |
| `OLLAMA_KEEP_ALIVE` | `5m` while working; `0` to unload the model when idle |

```bash
just setup
just ollama && ollama pull bge-m3
just index
just search "a phrase you know is in your vault"
```

Expect a ranked hit for that phrase. If search is empty, confirm `APO_NOTES_ROOT` and re-run `just index`.

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
    "APO_MODEL": "bge-m3",
    "APO_OLLAMA_URL": "http://127.0.0.1:11434",
    "OLLAMA_KEEP_ALIVE": "5m",
    "APO_MCP_LEAN": "1"
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

Ensure the same env vars as the Cursor block are visible to that process (`~/.claude.json` or shell profile). Set `APO_MCP_LEAN=1` unless you need admin tools (`memory_status`, `reindex`, …).

## 4. Verify

```bash
cd /ABSOLUTE/PATH/TO/apo && just inspect
```

With `APO_MCP_LEAN=1`, expect **11** tools. Without lean, expect **15**.

In the agent, run a known `search_notes` query — the right note should land near the top.

## 5. Background watcher (recommended)

Keeps the index current as notes change and drains the deferred write queue:

```bash
just watch-install
just watch-status
```

After pulling engine changes that touch watch/index code: `just setup && just watch-install`.

Full rebuilds (`just reindex`) commit embeddings in batches and clear the backlinks table — safe to interrupt and restart.

## 6. Agent onboard

Install gets the engine running. **Persistent write habits** should match *your* vault.

1. Open your vault as the agent workspace.
2. **Existing structure:** paste [`onboard-prompt.md`](./onboard-prompt.md) (infer → propose → approve).
3. **Empty vault / want a preset:** pick an optional contract template under [`contracts/`](./contracts/), scaffold (and any machine-readable YAML under `system/config/`), *then* run the onboard prompt.
4. Review drafts; approve before anything is written.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| 0 Apo tools in Cursor | Full quit/reopen; confirm `apo` key; Ollama up with `bge-m3` |
| Empty / stale search | `just reindex` after model/backend change; confirm `APO_NOTES_ROOT`; `just watch-status` |
| Writes don’t show in search | Ensure watcher installed; wait for debounce/poll (enqueue wakes watcher) |
| Ollama `/api/embed` HTTP 500 | Check `ollama --version` and upgrade if needed before blaming vault content |

Health one-liner (adjust `APO_INDEX` if you set a custom path):

```bash
curl -sf http://127.0.0.1:11434/api/tags | grep -q bge-m3 && \
  test -f "${APO_INDEX:-$HOME/Code/apo/engine/index.db}" && echo "Apo OK"
```

## What Apo is (one paragraph)

Agents search and update **your markdown files**. The index is disposable. Prefer surgical writes (`append_note` / `patch_note`) over full-file rewrites. Folders and frontmatter schemas are **yours**; Apo stays path + YAML agnostic. See [patch-note-ops.md](patch-note-ops.md) for `target` vs `scope` roles and the `check_item` intent op.
