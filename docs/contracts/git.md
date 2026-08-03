# Contract template: Git (backup / remote)

**Status:** optional template · **Behaviors + machine telegraph + optional sync** · pairs with any layout (PARA, FamilyOS, OKF, llm-wiki)

Use when the vault is (or should be) a **git checkout** with an off-machine remote. Markdown on disk remains the source of truth for agents; the remote is the durable off-machine copy. The Apo **index is rebuildable** — never treat `index.db` as backup.

Encode the live contract in the vault (`system/config/git.md` prose + `system/contracts/git-contract.schema.yaml`). This file is a **template** to copy — not live Apo config.

**Runtime:** Apo loads the YAML to (1) gate `history(path=…)` file-level git log and (2) optionally run **git sync** when `sync.enabled: true` (write-debounce commit+push; idle scheduled `pull --ff-only`; MCP/RPC `git_sync`). Gateway `GitRemote` stays out of scope.

## Stance

| Layer | Role |
|-------|------|
| **Git remote** | Off-machine SoT for the markdown tree (clone/restore) |
| **Vault files** | What humans and agents edit day to day |
| **Apo index** | Rebuildable search cache — not a backup medium |
| **This contract** | Declares remote, never-commit, LFS, restore, optional sync |

## What to encode

| Field | Meaning |
|-------|---------|
| `remote` | Canonical clone URL |
| `host` | `github` \| `forgejo` \| `gitlab` \| `local` |
| `default_branch` | Usually `main` |
| `backup.expectation` / `cadence_hint` | When humans/agents should expect a push |
| `lfs` | Whether Git LFS is in play; `.sha256` sidecars in plain git when binaries exist |
| `never_commit` | Globs that must stay out of the remote |
| `sync` | Opt-in engine sync (`enabled`, debounce, pull interval, message template, block hook) |
| `restore` | Owner + one-line restore drill |

## Machine contract (encode in the vault)

Copy or adapt:

- Prose: `system/config/git.md` (behaviors + pointers)
- YAML: `system/contracts/git-contract.schema.yaml` — starter: [git-contract.schema.yaml](./git-contract.schema.yaml)

Fill `remote` / `host` for *this* vault. Keep `never_commit` aligned with the vault root `.gitignore`.

## Agent behaviors

1. **Never commit** paths matching `never_commit` (especially `*.db`, `.env`, `.apo/`, Passport keys, `*.sqlite`).
2. **Do not** force-push the vault’s default branch.
3. Apo MCP writes update files on disk; with `sync.enabled`, the watcher debounces commit+push. Prefer MCP `git_sync` for status / force run / clear_block.
4. Tool-triggered `git_sync` `action=run` should pass an agent `message`; auto commits use `sync.commit_message_template` (`{iso_local}` → `YYYY-MM-DD HH:MM ET`).
5. When binaries are stored: use Git LFS if `lfs.enabled`; keep `.sha256` digest sidecars in **plain** git (not LFS pointers only).
6. After a bare-metal restore: clone → point `APO_VAULTS` / `APO_NOTES_ROOT` at the checkout → `just index --vault <id>` (or equivalent). Do not copy old `index.db` from backups unless debugging.
7. **History:** prefer MCP/RPC `history`. Browse mode (no `path`) = index mtime with optional `since`/`until`, `preview=first|last`, `heading=`, `exclude=`, `fields=` (returns `chunk_hash`). With `path=` and this contract active (YAML + `.git`), Apo returns **file-level** `git log` commits.

## Runtime

| Feature | Gate |
|---------|------|
| `history(path=)` git log | Contract YAML + work tree |
| Auto commit+push | `sync.enabled` + Apo write debounce (watcher) |
| Auto pull | `sync.enabled` + idle schedule (`git pull --ff-only`) |
| MCP/RPC `git_sync` | Contract active; `action=status\|run\|pull\|clear_block` |

On conflict / non-ff / push reject: **stop and surface** — status in `.apo/git-sync-status.json` (`state=blocked`) and tool payload. No force-push, no auto-resolve.

`blocked` is sticky: every later commit and pull returns early until `git_sync action=clear_block`. Nothing else surfaces it, so set `sync.on_block_command` to get told — it fires once per block episode (not per tick) with `$APO_SYNC_ERROR` and `$APO_VAULT_ROOT` exported, and a failing hook never masks the block.

## Suggested `.gitignore` floor

```gitignore
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.obsidian/workspaces.json
.obsidian/cache/
.trash/
.DS_Store
*.db
*.db-*
.env
.apo/
```

Add vault-specific secrets and local UI state as needed. Match `never_commit` in the YAML.

## Out of scope

- Auto-resolve / stash / reset / rebase pulls
- Laravel gateway `GitRemote` drivers, webhooks, or ECS git-sync sidecars
- Pull-before-commit on the write path (pull is idle/scheduled only)
- Choosing a host for you — fill `remote` when the vault has one

## Mixing

Ship alongside [para.md](./para.md), [okf-bundle.md](./okf-bundle.md), or FamilyOS. One git contract per vault root. Multi-vault: each named vault has its own remote and live `system/config/git*`.
