# Changelog

All notable changes to Apo (`jenorris/apo`) are documented here. Semver tags start with **v0.1.0**.

## [Unreleased]

## [0.6.1] — 2026-08-08

Bugbot follow-ups on the 0.6.0 table/ToC surface.

### Fixed

- **`read_note` on `table_row` / `table_header`** now returns the index `content_hash` (flattened row text), not a preamble section hash — so `expected_content_hash` from a row read matches search hits and patch preconditions.
- **Search hits include `table_id`** alongside `row_key` / `chunk_kind`, so multi-table notes can be patched without re-reading the file.
- **Table-contract `key_column`** is loaded from `system/contracts/table-contract.schema.yaml` and used when emitting `row_key` (and when resolving row ops).
- **`heading=` table locator** raises `table_ambiguous` when a section contains more than one table (was silently mutating the first).
- **Patch responses** attach fresh `content_hash` / `row_hash` keyed by `(table_id, row_key)` even on multi-table notes.

## [0.6.0] — 2026-08-08

Table awareness + document navigation. Markdown pipe tables now index as retrievable rows, `read_note` gains a table-of-contents mode and sibling hops, and `write_note`/`patch_note` accept structured (CSV/JSON) content. **Quit Cursor/Claude fully (Cmd+Q)** after upgrade so MCP reloads. **Reindex once** (`apo_admin` → `reindex(mode=rebuild)`) so existing notes emit table-row chunks.

### Added

- **Table row indexing** — each GFM table emits a `table_header` chunk plus one `table_row` chunk per data row, flattened with its heading breadcrumb + column labels (`Pacifica > Maintenance History — Date: 2026-06-07, …`) so natural-language queries hit the specific row. Search hits carry `chunk_kind` and `row_key`.
- **`read_note(mode=toc)`** — lean heading outline (title, level, `chunk_hash`, byte size) with no section bodies; the ToC-first → section fetch → row patch flow.
- **`read_note(sibling=prev|next)`** — hop to the same-depth section; responses include a `nav` cursor (`position`, prev/next `chunk_hash`) instead of always-on sibling arrays.
- **`read_note(format=json|row)`** — structured table payloads: `format=json` returns `{headers, rows}` for a section table; `format=row` returns `{columns, row_key, row_hash}` for a single row chunk.
- **Row-keyed `patch_note` ops** — `update_cell`, `update_row`, `append_row`, `delete_row`, `replace_table` (merge `replace|append|upsert`), `alter_table_schema`. Address rows by `row_key` + `table_id`/`heading`. Row edits **require** `expected_content_hash` or `expected_row_hash` (hard reject on stale). `alter_table_schema` requires `confirm=true`.
- **`write_note(sections=[…], frontmatter={…})`** — structured note assembly (XOR with `content=`); `content_type` `csv`/`json`/`table_json` sections serialize to GFM tables that index as rows.
- **Fuzzy CSV/JSON header mapping with ambiguity reject** — `replace_table` upsert/append maps incoming columns to existing ones; a tie or low-confidence match errors with `header_ambiguous` + per-column suggestions unless `allow_new_columns=true`.
- **`offset` + `has_more`** on `search_notes`, `history`, `backlinks` — cursor pagination for large result sets.
- **`APO_QUERY_PREFIX`** — query-side instruction prefix for asymmetric embedders (e.g. bge-m3); applied to the query only, never to indexed passages, and the cache keys on the raw query.
- **`core.reembed_one` / `core.reembed_batch`** — named watcher-only re-embed entry points; single-cell edits re-embed only the changed row via `content_hash` reuse.

### Changed

- **Unified section boundaries** — a new `markdown_sections` module is the single source of truth for hierarchical heading spans, so an index hit, a read, and a patch resolve to the *same* byte range (previously the indexer treated a `##` as owning its nested `###` bodies while the patch engine treated headings as flat siblings).
- **`patch_note` table ops default `strict=true`** and never mix with prose/frontmatter ops in one call.
- **Search-hit `content_hash`** now returns the full-chunk hash from the index (was hashing the snippet), so it is usable as a write precondition.

## [0.5.0] — 2026-08-07

MCP surface consolidation — **10 top-level tools**, **5 admin capabilities**. **Quit Cursor/Claude fully (Cmd+Q)** after upgrade so MCP reloads.

### Added

- **`read_note(chunk_hash=, force=, fields=)`** — absorbs `expand_section` / `expand_chunk`; one search→read anchor end-to-end.
- **`search_notes(folders=[])`** — multi-folder fan-out merge (XOR with `folder=`).
- **`vault(action=stats, days=)`** — habit KPI rollups (`folder_scoped_pct`, chunk-read ratio, validation tips) from embedded metrics.
- **`patch_note` place op** — `{op:place, src, dst, overwrite?, fields?}` replaces top-level `place_note`.
- **`reindex(mode=flush|rebuild)`** — merges `reindex_deferred`; legacy admin handler `reindex_deferred` one release.

### Changed

- **Top-level MCP count: 10** — removed `telemetry`, `expand_section`, `expand_chunk`, `place_note`.
- **Admin capabilities: 5** — removed `telemetry` rollup; operator observability → OTel + Jaeger (not Apo MCP).
- **MCP schemas strip alias params** — no `text`/`body`/`content`/`top_k`/`filters` on MCP (RPC keeps aliases one release).
- **`POST /v1/expand`** — delegates to `read_note(chunk_hash=)`; **`POST /v1/place`** → patch place op dispatch.
- **`POST /v1/telemetry`** / **`POST /v1/session_stats`** — deprecated; habits via **`POST /v1/vault` `action=stats`**.
- **Retired `just tool-stats` CLI** and **`LocalDeskMetricsBackend`** (`store.backend=local` maps to embedded).

### Removed

- Top-level MCP **`telemetry`** — use `vault(action=stats)` for habits; session traces via OTel hooks + Jaeger.
- MCP **`expand_section`**, **`expand_chunk`**, **`place_note`** — see Added migrations above.

### Migration cheatsheet

| Before | After |
|--------|-------|
| `expand_section(chunk_hash)` | `read_note(chunk_hash=…)` |
| `expand_chunk(…)` | `read_note(chunk_hash=…)` |
| `place_note(src, dst)` | `patch_note(ops=[{op:place, src, dst}])` |
| `search_notes` × N folders | `search_notes(folders=[…])` |
| `telemetry(action=efficiency)` | `vault(action=stats)` |
| `apo_admin` → `reindex_deferred` | `apo_admin` → `reindex(mode=flush)` |
| `just tool-stats` | `vault(action=stats)` or Jaeger UI |

## [0.4.0] — 2026-08-06

Desk agents + telemetry semver bump. **Quit Cursor/Claude fully (Cmd+Q)** after upgrade so MCP reloads. **Force reindex** all vaults after upgrade (`apo_admin` → `reindex` with `force=true`).

### Changed

- **`vault(action=project)` return-only** — removed `write`, CLI `--dry-run`, and `host`. Returns shared `body` + short `guidance` (non-prescriptive placement hint); agent chooses surface and frontmatter. Watcher no longer writes skill/rule files (logs when desk/contracts drift).

### Added

- **`apo_admin` meta-tool** — `list` / `describe` / `invoke` for engine ops (`memory_status`, `reindex*`, `reload_config`, `delete_note`, `tool_stats`, `git_sync`). Destructive invoke requires `confirm=true`. Replaces top-level admin tools and **`APO_MCP_LEAN`** (removed). Top-level MCP count: **15** (adds `expand_section`; `expand_chunk` deprecated alias). Tests: `engine/tests/test_apo_admin.py`.
- **Section-first markdown index** — one embed per heading section (no sub-chunk splits); search hits expose `file_bytes` / `section_bytes`; soft tips for large notes, sections, and preambles.
- **`expand_section(chunk_hash, force=false)`** — canonical read-more path with preview mode above `APO_SECTION_PREVIEW_BYTES` (8 KB default). `expand_chunk` remains a deprecated alias.
- **`vault(action=..., vaults=[…])` subset filter** — `list` / `contracts` / `describe` / `merge` / `project` all scope to a named subset of the registry (mutual exclusion with `vault=`; unknown names → `bad_vault`). `describe`'s empty-`vault=` default resolves against the filtered set, not the registry's true default. For a workspace whose desk projection should only ever mention some of the registered vaults (e.g. a persona workspace scoped to its own vault + one shared one). MCP `vault` tool + RPC `POST /v1/vault`.
- **Agent-habit wire compat** — `append_note`/`write_note` accept legacy `body=` alias; MCP instructions no longer say `body=text` (misread as kwarg). `patch_note` ops accept `set_field.path`→`field`, `replace_text.old_text`/`new_text`→`find`/`replace`. Validation hints + `agent-throughput.md` updated.
- **Telemetry `apo_version`** — each tool-call row stamps engine semver; `tool_stats` / `session_stats` expose `engine_version` + `by_version` rollups for cross-version burn-down.

- **MCP wire session context** — `SessionContextMiddleware` reads `_meta.apo/conversation_id` or `_apo.conversation_id` on each tools/call; strips `_apo` before validation; binds per-request contextvar for metrics (multi-session + remote-safe). RPC bodies accept the same fields. — [docs/contracts/search-contract.schema.yaml](docs/contracts/search-contract.schema.yaml): per-vault `default_exclude` globs for unscoped search and history browse. Loader: `apo_engine.search_contract`. Fan-out responses may include `default_exclude_by_vault`.
- **`vault-tools/`** — contract-gated batch mutators for vault corpora (OKF lint/fix/linkify/export pilot). Invoke with `--vault` / `VAULT_ROOT`; preflight requires `system/contracts/okf-contract*.yaml`. Thin vault Just binders call this toolkit; agents should not use vault roots as edit cwds. See [vault-tools/README.md](vault-tools/README.md). `just vault-tools …`.
- **Telemetry contract** — [docs/contracts/telemetry-contract.schema.yaml](docs/contracts/telemetry-contract.schema.yaml) + [telemetry.md](docs/contracts/telemetry.md). Vault-defined privacy for tool-use metrics (`paths: vault_relative` for optimization nodes). Engine honors `enabled: false`.
- **Lean MCP `session_stats` / `active_session`** — session-scoped rollups from `~/.apo/metrics.duckdb`; `by_path` when contract `expose_paths: true`. RPC `POST /v1/session_stats`. Admin `tool_stats` unchanged.
- **Contract-aware ingest** — `record_call` stamps `conversation_id` (``APO_CONVERSATION_ID`` or `active-session.json`), vault-relative `note_path`, heading, chunk_hash per telemetry contract.
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

- **Tool metrics storage** — MCP tool-use analytics now live in `~/.apo/metrics.duckdb` (DuckDB). Legacy `~/.apo/tool-metrics-*.jsonl` files are imported once on first open, then deleted. `just tool-stats` / admin `tool_stats` rollups unchanged.
- **Git sync commit subjects** — empty/`auto` messages expand path-aware templates (`{path_count}`, `{top_folders}` / `{paths_summary}` plus time tokens). Agent `git_sync` `message` still wins as subject. Commits always include a capped `Paths:` body trailer.
- **`APO_YAML_MAX_CHARS` / `APO_YAML_OVERLAP`** — YAML catalog chunking only; markdown ignores legacy `APO_MAX_CHARS` splits.
- Search hits omit `start_line` / `end_line` from the agent-facing payload (still stored internally).
- Tool counts: lean **13** / full **20** (adds `session_stats`, `active_session`).
- **`APO_SEARCH_EXCLUDE`** — deprecated desk-wide fallback; per-vault search-contract preferred.
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
