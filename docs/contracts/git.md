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
3. Apo MCP writes update files on disk; with `sync.enabled`, the watcher debounces commit+push. Prefer MCP `git_sync` for status / force run / rebase / clear_block.
4. Tool-triggered `git_sync` `action=run` should pass an agent `message` (subject). Empty message / auto commits use `sync.commit_message_template` with `{iso_local}` → `YYYY-MM-DD HH:MM ET`, plus path tokens `{path_count}`, `{top_folders}` / `{paths_summary}`. A capped `Paths:` body trailer is always attached.
5. When binaries are stored: use Git LFS if `lfs.enabled`; keep `.sha256` digest sidecars in **plain** git (not LFS pointers only).
6. After a bare-metal restore: clone → point `APO_COLLECTION_ROOT` / `APO_VAULT_PATHS` (or legacy `APO_VAULTS` / `APO_NOTES_ROOT`) at the checkout → `just index --vault <id>` (or equivalent). Do not copy old `index.db` from backups unless debugging.
7. **History:** prefer MCP/RPC `history`. Browse mode (no `path`) = index mtime with optional `since`/`until`, `preview=first\|last`, `heading=`, `exclude=`, `fields=` (returns `chunk_hash`). With `path=` and this contract active (YAML + `.git`), Apo returns **file-level** `git log` commits.
8. **`ref=` catalog (read-only):** `filter_notes(…, ref=)`, `read_note(path, ref=)`, and `search_notes(…, ref=)` project notes from a reachable git tip at the **vault registry root** (`git -C <vault.root>`). Catalog is frontmatter/YAML; search is FTS-only (no embeddings / no `chunk_hash`). Writes never take `ref=`.

## `ref=` semantics

| | |
|--|--|
| **Source** | Exported git ref (branch, bookmark → `refs/heads/*`, or commit OID) |
| **Not source** | Dirty working tree / uncommitted jj workspace files |
| **`filter_notes`** | Builds/caches `ref_files` by `tree_oid` (LRU 8); `sort=mtime` = git last-touch, not FS mtime |
| **`read_note`** | Blob via `git cat-file`; `mode=toc` from ATX headings (no `chunk_hash`); no WT `mtime` / region hashes (uses `git_tip_mtime` for display only); does not poison WT write CAS |
| **`search_notes`** | FTS5 over note bodies at the tip (`ref_fts`, same LRU as catalog); no embeddings / hybrid / `chunk_hash`. Follow up with `read_note(path, ref=)` |
| **Default when omitted** | Indexed **working tree** at the registry root (primary checkout) |

**jj + colocated vaults (e.g. compliance):** commit → bookmark (`feature/COMP-…`) → colocated export makes `refs/heads/feature/COMP-…` visible on the primary `.git` (no fetch). Then:

```text
filter_notes(where=…, folder=…, ref="feature/COMP-…", vault="compliance")
search_notes("retention", folder=…, ref="feature/COMP-…", vault="compliance")
read_note("policies/….md", ref="feature/COMP-…", vault="compliance")
```

Omit `ref=` only when intentionally querying the indexed primary working tree. After merge + pull + reindex, `ref=` is optional.

**Discover tips:** `apo_admin(action=invoke, name=list_refs, vault=…)` (or RPC `POST /v1/list_refs`) lists `refs/heads/*` at the registry root. Unknown `ref=` errors name a few reachable heads and point at `list_refs` instead of a bare `not_found`.

## Runtime

| Feature | Gate |
|---------|------|
| `history(path=)` git log | Contract YAML + work tree |
| `filter_notes`/`read_note` `ref=` | Vault root is a git work tree; ref must resolve (`rev-parse`) |
| Auto commit+push | `sync.enabled` + Apo write debounce (watcher) |
| Auto pull | `sync.enabled` + idle schedule (`git pull --ff-only`) |
| MCP/RPC `git_sync` | Contract active; `action=status\|run\|pull\|rebase\|clear_block` |

On conflict / non-ff / push reject: **stop and surface** — status in `.apo/git-sync-status.json` (`state=blocked`) and tool payload. No force-push, no auto-resolve.

`action=rebase` is the explicit recovery when `pull` (ff-only) blocks because local commits exist the remote doesn't (another session's sync won the race): fetch, then replay local commits onto `origin/<default_branch>` so the follow-up push stays a fast-forward. It is never run implicitly — only on explicit tool call. On a rebase conflict, Apo aborts the rebase immediately (working tree never carries `<<<<<<<` markers into a note) and blocks same as any other failure; still no content auto-resolve.

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

- Auto-resolve / stash / reset — and rebase is opt-in via explicit `action=rebase`, never automatic on the idle pull path
- Laravel gateway `GitRemote` drivers, webhooks, or ECS git-sync sidecars
- Pull-before-commit on the write path (pull is idle/scheduled only)
- Choosing a host for you — fill `remote` when the vault has one
- Per-branch embedding indexes / dirty `tree=` worktree catalog
- Auto-inject `ref=` from agent cwd

## Mixing

Ship alongside [para.md](./para.md), [okf-bundle.md](./okf-bundle.md), or FamilyOS. One git contract per vault root. Multi-vault: each named vault has its own remote and live `system/config/git*`.
