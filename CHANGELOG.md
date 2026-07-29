# Changelog

All notable changes to Apo (`jenorris/apo`) are documented here. Semver tags start with **v0.1.0**.

## [Unreleased]

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
