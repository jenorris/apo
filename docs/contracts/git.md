# Contract template: Git (backup / remote)

**Status:** optional template · **Behaviors + machine telegraph** · pairs with any layout (PARA, FamilyOS, OKF, llm-wiki)

Use when the vault is (or should be) a **git checkout** with an off-machine remote. Markdown on disk remains the source of truth for agents; the remote is the durable off-machine copy. The Apo **index is rebuildable** — never treat `index.db` as backup.

Encode the live contract in the vault (`system/config/git.md` + `system/config/git-contract.schema.yaml`). This file is a **template** to copy — not live Apo config.

**Runtime today:** mostly telegraph. Apo loads the YAML only to gate `history(path=…)` file-level git log. MCP / engine still do **not** pull, push, or enforce never-commit. Gateway `GitRemote` sync stays out of scope.

## Stance

| Layer | Role |
|-------|------|
| **Git remote** | Off-machine SoT for the markdown tree (clone/restore) |
| **Vault files** | What humans and agents edit day to day |
| **Apo index** | Rebuildable search cache — not a backup medium |
| **This contract** | Declares remote, never-commit, LFS, restore drill for agents/humans |

## What to encode

| Field | Meaning |
|-------|---------|
| `remote` | Canonical clone URL |
| `host` | `github` \| `forgejo` \| `gitlab` \| `local` |
| `default_branch` | Usually `main` |
| `backup.expectation` / `cadence_hint` | When humans/agents should expect a push |
| `lfs` | Whether Git LFS is in play; `.sha256` sidecars in plain git when binaries exist |
| `never_commit` | Globs that must stay out of the remote |
| `restore` | Owner + one-line restore drill |

## Machine contract (encode in the vault)

Copy or adapt:

- Prose: `system/config/git.md` (behaviors + pointers)
- YAML: `system/config/git-contract.schema.yaml` — starter: [git-contract.schema.yaml](./git-contract.schema.yaml)

Fill `remote` / `host` for *this* vault. Keep `never_commit` aligned with the vault root `.gitignore`.

## Agent behaviors

1. **Never commit** paths matching `never_commit` (especially `*.db`, `.env`, `.apo/`, Passport keys, `*.sqlite`).
2. **Do not** force-push the vault’s default branch.
3. **Do not** treat Apo MCP writes as a git commit — files land on disk; git is a separate human/agent step.
4. Commit / push only when the human asks, or when a vault runbook explicitly requires it after a consequential batch.
5. When binaries are stored: use Git LFS if `lfs.enabled`; keep `.sha256` digest sidecars in **plain** git (not LFS pointers only).
6. After a bare-metal restore: clone → point `APO_VAULTS` / `APO_NOTES_ROOT` at the checkout → `just index --vault <id>` (or equivalent). Do not copy old `index.db` from backups unless debugging.
7. **History:** prefer MCP/RPC `history`. Browse mode (no `path`) = index mtime. With `path=` and this contract active (YAML + `.git`), Apo returns **file-level** `git log` commits. `recent_activity` is a frozen alias through **v0.1.x** — remove in **v0.2.0**.

## Runtime (partial — history only)

Apo loads `system/config/git-contract.schema.yaml` only to detect “contract active” for `history(path=…)`. Still **no** automated pull/push, and no write-time validation of this YAML.

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

- Automated `git pull` / `git push` from Apo MCP or `apo-engine`
- Laravel gateway `GitRemote` drivers, webhooks, or ECS git-sync sidecars
- Engine env vars (`APO_GIT_*`) or write-time validation of this YAML
- Choosing a host for you — fill `remote` when the vault has one

## Mixing

Ship alongside [para.md](./para.md), [okf-bundle.md](./okf-bundle.md), or FamilyOS. One git contract per vault root. Multi-vault: each named vault has its own remote and live `system/config/git*`.
