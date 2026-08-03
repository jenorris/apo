# Changelog

All notable changes to Apo (`jenorris/apo`) are documented here. Semver tags start with **v0.1.0**.

## [0.3.0rc4] — 2026-08-03

### Changed

- **`place_note`** replaces MCP `move_note` + `send_note`: move when `src` is in the vault; copy host `.md` otherwise (`mode=move|copy`). RPC: `POST /v1/place`; `/v1/move` and `/v1/send` remain aliases.
- **`patch_note`** accepts multi-path `items[]` XOR single `path`+`ops` (removed separate MCP `patch_notes`; RPC `/v1/patch_notes` still works).
- **`git_sync`** demoted to admin (`APO_MCP_LEAN=0`). Auto sync still runs in the watcher.
- Lean **10** / full **17** tools.

## [0.3.0rc3] — 2026-08-03

### Added

- **`patch_notes`** — same-vault multi-path patch batch (`items: [{path, ops, expected_mtime?}]`, max 20). Continues on per-item failure (`partial` / `results[]`). MCP + `POST /v1/patch_notes`. Dual-write (domain + session log) still parallel `append_note` + `patch_note`. Lean **13** / full **19**.

## [0.3.0rc2] — 2026-08-03

### Removed

- **`recent_activity`** MCP tool and **`POST /v1/recent`** RPC alias — use `history` / `POST /v1/history` only. Lean was **12** / full **18** after this cut (before `patch_notes`).

## [0.3.0rc1] — 2026-08-03

### Added

- **Git contract sync** — opt-in `sync.enabled` in `git-contract.schema.yaml`: watcher debounce commit+push after Apo writes; idle scheduled `git pull --ff-only`; MCP/RPC `git_sync` (`status` \| `run` \| `pull` \| `clear_block`). Conflicts / non-ff / push reject → `blocked` + `.apo/git-sync-status.json`. Never force-push; enforce `never_commit`. Spec: Meta `projects/apo-git-sync/mvp`.
- Lean tool count **13** / full **19** (`git_sync` + then-still-present `recent_activity`).
- **Agent habit tips** — successful `search` without `folder=` returns soft `tip` to scope; second in-process write to the same path without `expected_mtime` tips to thread mtime. See `docs/agent-throughput.md`.

### Notes

- Release candidate for **v0.3.0**.

## [0.2.0] — 2026-07-29

### Changed

- **MCP façade** — lean tools delegate to `apo_engine.ops` (parity with local RPC); watcher-not-running tip on successful writes is shared. `engine/mcp/server.py` is a thin FastMCP layer (admin tools + resources stay local).
- **Published tool counts** — lean **12** / full **18** (docs + `just inspect` expectations).
- **`filter_notes`** — omitted `where`/`filters` defaults to `{}` (list notes without forcing an empty object).
- **`expand_chunk`** — returns `mtime` when the source file exists (chain into `expected_mtime`).
- **Onboard / lean diagnostics** — stop requiring lean-hidden `memory_status`; prefer smoke tools + write `warning` / `just watch-status`.

### Deprecated

- **`recent_activity`** / `POST /v1/recent` — still frozen aliases of `history`. Removal deferred from this release to **v0.3.0** (0.2.0 is the MCP façade cut). Prefer `history`.

## [0.1.2] — 2026-07-28

### Fixed

- **Duplicate section headers on append** — `append` / `prepend` / `replace_section` strip a leading markdown heading that repeats the section anchor (common MCP client misuse: `heading="## Session log"` plus the same line in `text`). EOF append is unchanged.

## [0.1.1] — 2026-07-26

### Fixed

- **Git contract active check** — detect work trees via `git rev-parse` so Meta-style subdirectory vaults (parent `.git`) and dedicated vault checkouts both activate `history(path=)`.

## [0.1.0] — 2026-07-26

First tagged release. Tool/schema surface is now versioned toward **v1**.

### Added

- **Git contract template** — `docs/contracts/git.md` + `git-contract.schema.yaml` (telegraph backup/remote expectations; vault live copies under `system/config/`).
- **`history` MCP / RPC tool** — browse by index mtime (same as former `recent_activity`); with `path=` and an active git contract (YAML + `.git`), returns **file-level** `git log` commits. RPC: `POST /v1/history`.

### Deprecated

- **`recent_activity`** / `POST /v1/recent` — frozen aliases of `history` through the **v0.1.x** line. Removal moved to **v0.3.0** (see 0.2.0 notes). Prefer `history`.

### Notes

- Git contract does not automate pull/push; engine loads YAML only to gate `history(path=…)`.
- Chunk/blame history is out of scope until a later release.
