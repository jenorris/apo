# Changelog

All notable changes to Apo (`jenorris/apo`) are documented here. Semver tags start with **v0.1.0**.

## [Unreleased]

Worth-using polish for desk agents, Hermes/Night Shift, and fresh-machine share (cut as **v0.4.0** when ready). **Quit Cursor/Claude fully (Cmd+Q)** after upgrade so MCP reloads.

### Added

- **`vault-tools/`** — contract-gated batch mutators for vault corpora (OKF lint/fix/linkify/export pilot). Invoke with `--vault` / `VAULT_ROOT`; preflight requires `system/contracts/okf-contract*.yaml`. Thin vault Just binders call this toolkit; agents should not use vault roots as edit cwds. See [vault-tools/README.md](vault-tools/README.md). `just vault-tools …`.
- **`search_notes(vaults=[…])` fan-out** — hybrid search across named vaults (separate sqlite indexes), merge by score, stamp each hit with `vault`. Mutual exclusion with `vault=`. MCP + RPC `/v1/search`. See [docs/multi-vault.md](docs/multi-vault.md).
- **Usage `contribution`** — optional authoring dialect (`plain-md` \| `gfm` \| `obsidian-ofm`) + features/surfaces + orthogonal `render` (`none` \| `htmlize`) on [usage-contract.schema.yaml](docs/contracts/usage-contract.schema.yaml). `vault(project)` selectively loads usage-contract bodies and emits a per-vault **Contribution** one-liner into apo-desk (deep OFM/htmlize docs stay in pointers).
- **Region write preconditions** — `expected_frontmatter_hash` / `expected_body_hash` / `expected_content_hash` on `write_note` / `append_note` / `patch_note` (and per `items[]`). When `expected_mtime` is stale, FM-only or section/chunk writes still proceed if the untouched region matches. Same-process prior `read_note` / `expand_chunk` snapshots enable the FM/body split without extra args. Reads, expands, and search hits return the hashes.
- **`vault` tool** — lean-visible `list` / `contracts` / `describe` / `merge` / `project` for registry + contract discovery + desk overlay + host skill projection. Preferred live IR: `<vault>/system/contracts/`; legacy `system/config/*-contract.schema.yaml` still discovered. Engine OKF/git loaders prefer `system/contracts/` then legacy. Desk: `~/.apo/desk.yaml` ([docs/examples/desk.example.yaml](docs/examples/desk.example.yaml)). `project` writes Cursor `apo-desk.mdc`, Claude `apo-desk` skill, and **Hermes** `~/.apo/projected/hermes/apo-desk/SKILL.md` (`host=hermes|all`; `APO_PROJECT_HERMES`). MCP + RPC `GET|POST /v1/vault`. Contract payloads default to summaries; `full=true` includes YAML bodies. `just desk-project` / watcher auto-reproject.
- **Usage contract template** — [docs/contracts/usage-contract.schema.yaml](docs/contracts/usage-contract.schema.yaml) (host-neutral vault usage IR; engine discovery/project only — not interpreted for search/write). Hermes guide: [docs/hermes.md](docs/hermes.md).
- **`history` browse digests** — `since` / `until` (date-only = America/New_York day bounds), `preview=first|last`, optional `heading=` chunk scope, `exclude=` globs, optional frontmatter `fields=`, and `chunk_hash` on each note.
- **Body-field aliases** — `append_note` accepts `content=` as alias for `text=`; `write_note` accepts `text=` as alias for `content=` (conflict → `bad_request`; soft `tip` when alias used).
- **Agent habit UX** — `read_note` / `expand_chunk` record path touches so follow-up writes tip literal `expected_mtime=<n>`; unscoped search tips include top-level dirs; search hits include float `mtime` beside ISO `modified`; MCP `search_notes` accepts `exclude=`; `tool_stats` rolls up `by_error_shape`.
- **`just tool-list`** — pure-Python lean tool count (no Node/`npx`).

### Fixed

- **Tool metrics middleware order** — `ToolMetricsMiddleware` sits outside `AgentValidationMiddleware` so schema rejects are recorded as `validation_error` + `error_shape` (was inner → raw `ValidationError`, empty `by_error_shape`). `_pydantic_errors` walks the `__cause__` chain (ToolError → FastMCP → pydantic) so shapes survive the rewrite.

### Changed

- **Git sync commit subjects** — empty/`auto` messages expand path-aware templates (`{path_count}`, `{top_folders}` / `{paths_summary}` plus time tokens). Agent `git_sync` `message` still wins as subject. Commits always include a capped `Paths:` body trailer.
- Tool counts: lean **11** / full **18** (adds `vault`).
- `APO_SEARCH_EXCLUDE` documented as recommended desk default (`inbox/daily/* archives/*`); `config.env.example` enables it.
- `launchd-watch.sh` default embed backend aligned with engine (`ollama`).
- Share docs: real clone URL, Python 3.11+, Linux `watch-start`, Claude env example, lean health/`0 tools` troubleshooting, `/v1/vault` in local-rpc.

## [0.3.1] — 2026-08-03

### Fixed

- **MCP / schema copy** — remove desk dual-write (domain + session log) from product wire instructions and `patch_note(items=)` descriptions. Parallel mutators stay same-`vault=`; cross-role writes remain separate MCP calls. Desk dual-write stays in Cursor `mcp-apo.mdc` + Meta vault policy.

## [0.3.0] — 2026-08-03

Stable release — everything from rc1–rc6 plus the readiness-assessment hardening below.

### Added

- **YAML catalog notes** (from rc6) — `.yaml` / `.yml` are first-class indexed notes. Whole-file mapping → `files.frontmatter` for `filter_notes`; `read_note` / `write_note` / `patch_note(set_field|delete_field)` with dotted nested paths; OKF stamp/validate format-aware. Hybrid search embeds `title` / `description` / `okf_type` / `status` / `resource`. `append_note` and heading ops stay Markdown-only (`unsupported_format`). Machine contracts under `system/config/*-contract.schema.yaml` are ignored by default.
- **Search eval harness** — `apo-engine search-eval` / `just search-eval`: labeled YAML query sets (outside the repo) scored as hit@k / MRR@k through the real `ops.search` path. Example: `docs/examples/search-eval.example.yaml`; results + methodology: `docs/search-quality.md`.
- **Optional cross-encoder reranker** — `APO_RERANK=1` + `pip install -e '.[rerank]'` (fastembed ONNX, local). Rescores the fused pool (`APO_RERANK_POOL`, default 24) before the cut to `k`; responses set `reranked: true`; any failure falls back to fused order with a `warning`. Eval-measured as a marginal lift — see `docs/search-quality.md` before enabling.
- **`APO_SEARCH_EXCLUDE`** — default exclude globs for *unscoped* searches (e.g. `inbox/daily/* archives/*`; measured +8pts hit@5). Never applied to `folder=`-scoped or caller-`exclude=` searches; responses carry `default_exclude` when active.
- **CI + dev tooling** — GitHub Actions (Linux + macOS, py3.11/3.12), `just test`, `dev` extra (pytest). `engine/README.md` fixes `uv` editable installs (pyproject no longer reads `../README.md`).

### Changed

- **Embed-backend-down search degrades to BM25** with an actionable `warning` (MCP/RPC field + CLI stderr) instead of silently returning `[]`.
- **Nonexistent `folder=` warns** on `search_notes` / `filter_notes` (`results are empty by construction` + real top-level dirs) instead of a silent empty result.
- **Hermetic tests** — `APO_DEFERRED_DIR` overrides the `~/.apo` runtime dir; `tests/conftest.py` isolates queues, tool metrics, and the watcher PID probe per test. The suite never touches `~/.apo` or a real vault.
- **Tool metrics** — validation failures now record a privacy-safe `error_shape` (pydantic `type:loc` only, never values) for hint burn-downs.
- **Canonical index location** — docs/examples now recommend `~/.apo/index.db` over `engine/index.db` (multi-vault already defaulted there).
- **Docs truth pass** — README tool counts corrected to lean **10** / full **17** (contract-tested); `move_note`/`send_note` ghosts replaced by `place_note` in `docs/patch-note-ops.md`, contracts, and validation hints; `justfile` no longer hardcodes the Homebrew Ollama path.

## [0.3.0rc5] — 2026-08-03

### Changed

- **`append_note`**: `path` optional when `chunk_hash` is set (path derived from the index; optional path remains a guard).
- **`patch_note` ops**: `append` / `prepend` / `replace_section` / `replace_text` accept `chunk_hash` (or `scope.chunk_hash`) as target/scope; resolved to the innermost heading covering the chunk span.
- **Stale-hash fallback**: on `anchor_not_found`, if `path` + `heading` from the search hit are still present, retry by heading and return a soft `tip` to re-search.

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
