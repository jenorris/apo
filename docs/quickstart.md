# Apo quickstart

Get a local Apo engine indexing **your** Markdown vault and talking to Cursor or Claude Code over MCP.

**You need:** macOS or Linux, **Python 3.11+**, Homebrew (or equivalent), [Ollama](https://ollama.com), a folder of `.md` notes, ~3 GB free while `bge-m3` is loaded. `just inspect` also needs Node (`npx`) and [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) — or use `just tool-list` instead.

This guide is **local engine only** — one machine, one vault root. Default embeddings require a running Ollama daemon.

## 1. Install

```bash
git clone https://github.com/jenorris/apo.git ~/Code/apo   # or your preferred path
cd ~/Code/apo
brew install ollama just ripgrep   # Node optional — only for `just inspect`
cp config.env.example .env
```

Edit `.env` (use **absolute paths** — `just dotenv-load` does not expand `${HOME}`):

| Variable | Set to |
|----------|--------|
| `APO_NOTES_ROOT` | Absolute path to **your** markdown vault |
| `APO_INDEX` | Recommend `$HOME/.apo/index.db` — survives clean checkouts; multi-vault indexes default there. To relocate later: move the file, update `APO_INDEX` everywhere it's set (`.env` + MCP config), restart the watcher — or just `just reindex` at the new path |
| `OLLAMA_KEEP_ALIVE` | `5m` while working; `0` to unload the model when idle |
| Search noise filter | Copy [search-contract.schema.yaml](./contracts/search-contract.schema.yaml) to `<vault>/system/contracts/` and set `default_exclude` (see [search-quality.md](./search-quality.md)) |

```bash
just setup
just ollama && ollama pull bge-m3
just index
just search "a phrase you know is in your vault"
```

`just setup` also installs `apo` / `apo-engine` / `apo-local` / `apo-mcp` onto `PATH` via `uv tool install --editable` (needs [uv](https://docs.astral.sh/uv/)) — MCP registration and the watcher below use `apo-mcp`/`apo-engine` by name; the local `engine/.venv` stays around for `just test`/`just inspect`/`just tool-list`. `apo` is the unified entry point (`apo <note-verb>`, `apo engine <cmd>`, `apo mcp`) — reach for it by hand going forward; `apo-engine`/`apo-local`/`apo-mcp` are unchanged and still work, and nothing in this guide's MCP registration needs to change.

Expect a ranked hit for that phrase. If search is empty, confirm `APO_NOTES_ROOT` and re-run `just index`.

## 2. Register MCP — Cursor

Add an `apo` block to `~/.cursor/mcp.json` (merge into existing `mcpServers`):

```json
"apo": {
  "command": "apo-mcp",
  "env": {
    "APO_NOTES_ROOT": "/ABSOLUTE/PATH/TO/YOUR/VAULT",
    "APO_INDEX": "/ABSOLUTE/PATH/TO/HOME/.apo/index.db",
    "APO_EMBED_BACKEND": "ollama",
    "APO_MODEL": "bge-m3",
    "APO_OLLAMA_URL": "http://127.0.0.1:11434",
    "OLLAMA_KEEP_ALIVE": "5m"
  }
}
```

`apo-mcp` resolves via `PATH` (`~/.local/bin`, where `uv tool install` puts it) — no `args`/`cwd` needed. If the host launches without inheriting your shell `PATH` (rare), use the absolute shim path instead: `"command": "/ABSOLUTE/PATH/TO/HOME/.local/bin/apo-mcp"`.

Per-vault search noise filters live in `<vault>/system/contracts/search-contract.schema.yaml` — not MCP env.

Engine admin ops (`memory_status`, `reindex`, `delete_note`, `git_sync`, `list_refs`, `reload_config`) are reached via **`apo_admin(action=list|describe|invoke)`**. Destructive invoke requires **`confirm=true`** (`delete_note` always; `reindex` when `force=true`; `git_sync` for `run`/`pull`/`rebase`).

**Quit Cursor fully** (Cmd+Q on macOS) and reopen. MCP subprocesses do not reliably hot-reload.

## 3. Register MCP — Claude Code

```bash
claude mcp add -s user apo -- apo-mcp
```

Then put the same env block as Cursor into `~/.claude.json` under the `apo` server (or export them in the shell that launches Claude):

```json
"env": {
  "APO_NOTES_ROOT": "/ABSOLUTE/PATH/TO/YOUR/VAULT",
  "APO_INDEX": "/ABSOLUTE/PATH/TO/HOME/.apo/index.db",
  "APO_EMBED_BACKEND": "ollama",
  "APO_MODEL": "bge-m3",
  "APO_OLLAMA_URL": "http://127.0.0.1:11434",
  "OLLAMA_KEEP_ALIVE": "5m"
}
```

Expect **11** top-level tools (includes `vault`, `apo_admin`, `scratchpad`). Optional habit KPIs: `vault(request={action: "stats"})`.

## 4. Verify

```bash
cd /ABSOLUTE/PATH/TO/apo
just tool-list    # pure Python — no Node required
# or: just inspect   # needs npx + rg
```

In the agent, run a known `search_notes` query — the right note should land near the top.

## 5. Background watcher (recommended)

Keeps the index current as notes change and drains the deferred write queue.

**macOS (launchd):**

```bash
just watch-install
just watch-status
```

**Linux / foreground / any OS (no launchd):**

```bash
just watch-start
just watch-status
```

After pulling engine changes that touch watch/index code: `just setup`, then re-run `watch-install` (macOS) or `watch-start`.

Full rebuilds (`just reindex`) commit embeddings in batches and clear the backlinks table — safe to interrupt and restart.

## 6. Agent onboard

Install gets the engine running. **Persistent write habits** should match *your* vault.

1. Open your vault as the agent workspace.
2. **Existing structure:** paste [`onboard-prompt.md`](./onboard-prompt.md) (infer → propose → approve).
3. **Empty vault / want a preset:** pick an optional contract template under [`contracts/`](./contracts/), scaffold (and any machine-readable YAML under `system/contracts/`), *then* run the onboard prompt.
4. Review drafts; approve before anything is written.
5. Throughput habits (decision tree, `folder=`, `expected_mtime`, metrics): [`agent-throughput.md`](./agent-throughput.md).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| 0 Apo tools in Cursor | Full quit/reopen (Cmd+Q); confirm `mcpServers.apo` key; `command -v apo-mcp` resolves (`just setup` installs it via `uv tool install`); `APO_NOTES_ROOT` exists; Ollama up with `bge-m3`; check MCP host logs for subprocess crash |
| Empty / stale search | `just reindex` after model/backend change; confirm `APO_NOTES_ROOT`; `just watch-status` |
| Writes don’t show in search | Ensure watcher is running (`just watch-status`); wait for debounce/poll (enqueue wakes watcher) |
| Ollama `/api/embed` HTTP 500 | Check `ollama --version` and upgrade if needed before blaming vault content |

Health (Ollama + index + watcher):

```bash
curl -sf http://127.0.0.1:11434/api/tags | grep -q bge-m3 && \
  test -f "${APO_INDEX:-$HOME/.apo/index.db}" && \
  just watch-status && echo "Apo OK"
```

## What Apo is (one paragraph)

Agents search and update **your markdown files**. The index is disposable. Prefer surgical writes (`append_note` / `patch_note`) over full-file rewrites. Folders and frontmatter schemas are **yours**; Apo stays path + YAML agnostic. See [patch-note-ops.md](patch-note-ops.md) for `target` vs `scope` roles (aliases only — no new ops).
