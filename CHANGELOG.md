# Changelog

All notable changes to Apo (`jenorris/apo`) are documented here. Semver tags start with **v0.1.0**.

## [0.31.0] — 2026-09-27

A follow-up review of Apo's intermediate search representation — not "is the
index too big" but "is the chunking/ranking strategy actually good" — found
five real problems downstream of the 0.30.0 rebuild. Fixed all of them, plus
the breadcrumb duplication and eval-harness gap the review also flagged.

### Fixed

- **Mermaid parser dropped most hand-authored diagrams** — a dead regex, a
  subgraph pattern that required quoted labels, no support for dotted/thick/
  chained edges, and a greedy sequence-diagram arrow regex that turned
  `API-->>MCP` into a phantom node `API-`. Measured on 10 real Work-vault
  fences: node coverage 34→64 ids, label-less nodes 6→3 (the remainder are
  legitimate subgraph-as-node references, not bugs). Two fences that
  previously failed to parse at all now fully parse. Non-flow diagram types
  (`xychart-beta`, `gitGraph`, `pie`, and 16 others) now short-circuit to
  file-level indexing instead of falling through to flowchart regexes and
  producing garbage nodes from unrelated syntax (e.g. a bar chart's data
  series numbers).
- **Mermaid node/edge chunks carried no relational context.** Edge chunks
  embedded raw node ids (`P0 --> P3`) instead of labels; node chunks carried
  no information about what they connect to or from, despite a diagram
  node's meaning being inherently relational. Both now resolve ids to labels
  and node chunks append 1-hop in/out context (`· from: Core API · to:
  Stripe — card, Authorize.net — ACH`); `read_note(format="node")` returns
  the same in/out lists so the read and embed paths agree.
  `include_edge_chunks` now defaults to `false` (edges are largely redundant
  once nodes carry relational context) — an explicit `true`/`false` in a
  contract still wins, so no already-configured vault's behavior changes.
- **Folder-scoped search injection picked the wrong representative chunk.**
  `table_row`/`table_header`/`mermaid_*` chunks are all stored with
  `heading_level=0`, so `ORDER BY heading_level ASC` systematically preferred
  a generic table/mermaid chunk over a note's actual prose content — measured
  on a live query, a `Metadata — Columns: Field, Value` table header
  outscored the note's real diagram content 2.24–2.90 to 1.30. Now prefers
  section chunks, falling back to table/mermaid only when a note has none.
- **`table_header` chunks were redundant noise** — 14% of all top-10 hits
  across 30 real queries were generic `Columns: Field, Value`-style chunks
  carrying zero note-specific information (the section chunk's
  `[table: N rows — cols]` marker already covers this). Demoted (not
  removed — `_build_toc`/note-outline reads still need the rows).
- **No result-set diversification** — every ranking boost applies per-path
  identically to every chunk of that path, so a table with many matching
  rows could crowd out everything else: 11 of 30 real queries had 4+ results
  from one source. Added a soft per-path cap (`APO_RESULT_DIVERSITY_CAP`,
  default 2) in the fusion step, backfilling remaining slots from the rest
  of the pool if the capped selection doesn't fill `k`. A synthetic
  crowded-table query went from 2 distinct paths in the top-5 to 4.
- Table/mermaid chunk breadcrumbs duplicated the note title when a note's H1
  repeats its frontmatter/filename title (36% of work-vault table rows did
  this) — `"Title > Title > Section"` instead of `"Title > Section"`, pure
  wasted prefix averaging 21% of chunk text.
- **The eval harness had no way to detect its own fixtures going stale** — a
  compliance-vault doc migration silently broke 18 of 19 `expect` paths in a
  mermaid eval a month ago (86%→0% hit@3), and nothing noticed because
  nothing runs the evals routinely and a stale `expect` scored identically
  to a real miss. Added a staleness gate (distinct `STALE` vs `MISS`,
  non-zero exit), result-composition metrics (`distinct_paths@k`,
  `max_same_path@k`, `chunk_kind` breakdown — the numbers that `hit@k`/`MRR`
  hide), and `just check-evals` to run every discovered fixture against a
  checked-in baseline. Re-labeled the repo-tracked mermaid fixtures against
  their real post-migration paths.

## [0.30.0] — 2026-09-27

A performance/simplicity/maintainability review turned up a live search-quality
problem (89.8% of the `work` vault's chunks were auto-generated Gmail tables)
and a pile of accreted config/code debt; this release fixes the former and
works through the latter, plus a contract-mechanism tightening pass covering
the same class of drift 0.29.0 chased across tool surfaces.

### Fixed

- **`index-work.db` was 90% auto-generated Gmail ingest tables** (468,766 of
  522,261 chunks) sitting in a `vec0` table that was 69% dead space (sqlite-vec
  never reclaims deleted slots), plus ~7GB of unvacuumed freed pages from the
  0.28.1 backlinks fix. Real hybrid-search latency on `work`: 50.4s cold /
  1.7s warm. Excluded the ingest folder and a buggy calendar-sync script's
  derivative output (`areas/schedule/days/*`, rewritten ~once/minute around
  the clock despite its own "skip if unchanged" logic never actually
  triggering) via `.indexignore`, rebuilt, and vacuumed:
  **16.8GB → 240MB, same query now 0.44s.**
- `index_health()`'s `vec_chunks`/`chunks_fts` orphan checks went through the
  virtual tables themselves — for `vec0` that means materializing every
  embedding blob just to check rowid membership (seconds on a large index).
  Both have their own shadow tables built for exactly this
  (`vec_chunks_rowids.rowid`, `chunks_fts_docsize.id`); `apo-engine doctor`
  drops from 25s to sub-second.
- Watcher logs were 988MB/174MB of un-timestamped, unrotated `print()` output
  — a burst of already-fixed cycle errors was impossible to date, and a
  steady-state re-embed (the schedule-sync bug above) was invisible in them.
  `watch.py` (and `git_sync`/`optima_merge`/`vault_project`'s own `print()`
  calls, which would have kept the old growth going even with the watcher's
  own logging fixed) now log through Python's `logging` with ISO timestamps
  and a `RotatingFileHandler` (50MB × 3). `core.py`'s existing
  `apo.index` logger inherits the same handler for free via the logging
  hierarchy.
- `git_sync._run_git`'s 120s timeout applied even to `push`/`pull`/`fetch` —
  the only realistic hang is a dead network on those specifically; tightened
  to 30s for network calls, left at 120s for local git operations.
- **`table-contract` was half-enforced.** `patch_table` row ops keyed by the
  contract `key_column`, but `replace_table(merge=upsert)` keyed by the first
  cell — on a table whose key column is not first (the template's own `SKU`
  example) an upsert appended a duplicate instead of replacing the row. Upsert
  now keys by `key_column`. The template's per-rule `merge`,
  `allow_new_columns` and `header_synonyms` were documented since 0.6.1 and
  never read; they are now defaults for `replace_table` (op fields still win;
  `ReplaceTableOp.merge` / `allow_new_columns` default to unset so a contract
  default can apply). `allow_new_columns: true` accepted a new header and then
  dropped its values because the table schema never grew — the column is now
  added. The mapping form `tables: {horizon.md: {key_column: start}}` (the
  shape the Optima vault ships) was silently ignored; both list and mapping
  forms are honored.
- **`vault(lint)` skipped OKF entirely.** Only `apo-engine okf validate`
  checked corpus conformance; MCP agents had no equivalent. Lint now runs the
  producer profile per note (`okf.missing_field`, plus `okf.missing_frontmatter`
  / `okf.reserved_frontmatter` for the structural clauses) with the same
  `suggested_op` shape as post-write flaws; the flagged-path set matches the
  CLI. `read_note(lint=true)` inherits it. `note_lint.lint_note(include_okf=)`
  opts out.
- **Lint sweep cache ignored most contract edits.** The 120 s `vault(lint)`
  cache fingerprinted only the read and archival contracts; editing the OKF or
  usage contract served stale findings. It now fingerprints every contract file.

### Added

- **Contract shape checks** (`vault_contracts.check_contract`): each
  engine-interpreted contract (`okf`, `search`, `table`, `git`, `mermaid`,
  `archival`, `telemetry`, `optima` `refresh`) declares the keys its loader
  reads and its enums. Unknown top-level keys, values outside an enum
  (`enforcement: strict`, `store.backend: sqlite`, `mode: maybe`) and wrong
  container shapes surface as `warnings[]` on `vault(contracts|describe|merge)`
  entries and as `contract.unknown_key` / `contract.invalid_value` /
  `contract.invalid_shape` flaws on unscoped `vault(lint)`; unparseable YAML is
  `contract.unreadable` (`error`). Advisory only — nothing blocks. Contracts the
  engine does not interpret (`usage`, `read`, `local-web`) are not checked.
  Against the live desk this flags exactly the Work vault's `provenance_optional`
  / `okf_02_soft_on` (0.2 provenance keys no loader reads).
- **Shared contract YAML cache** (`vault_contracts.load_yaml_cached`, keyed on
  path + mtime_ns + size): the eight per-contract loaders shared one copy-pasted
  parse-and-check body; `git` and `optima` had grown private caches for the
  watcher tick while the `table` loader re-parsed YAML per indexed file. One
  loader, one cache, one `clear_yaml_cache()`.
- **Watcher hook health**: `last_tick_at`/`last_tick_seconds`/`ok`/`error` per
  hook (git-sync, Optima-merge, desk-reprojection), surfaced through
  `apo-engine doctor` and MCP `index_health`/`memory_status` — a hang in any
  of the three (only exceptions were ever guarded against, not stalls) is now
  visible instead of silently stopping indexing. Does not add the
  thread-per-hook scheduler `docs/watcher-scheduler-separation.md` floated —
  reviewed and rejected as more machinery than a solo maintainer's watcher
  needs; the doc now carries a status note.
- `ranking.py`: the ~600 lines of hybrid-search boost heuristics
  (`_phrase_stem_boost`, `_backlink_search_boost`, `_neighbor_rank_boost`, etc.)
  moved out of `core.py` — a straight relocation, same logic and signatures,
  so `search()` reads as retrieval + storage, not retrieval + storage + eight
  boost multipliers.
- `search-contract.schema.yaml` gains `boost_vocab`: the architecture/system
  vocabulary that biases ranking toward infra-flavored results used to be a
  hardcoded regex in engine source — literal employer system names
  (`skypad`, `faber`, `cortana`, `starrez`, …) shipping in a general-purpose
  local tool, applied identically to every vault including personal ones with
  no relation to that vocabulary. Now empty by default (no boost, not a
  crash) and set per-vault; the `work` vault's contract now carries the exact
  terms that used to be hardcoded, so its ranking is unchanged.

### Changed

- `apo-engine search`/`stats` route through `ops.search`/`ops.stats` (were
  calling `core.*` directly) — CLI and MCP tools now return the same
  `{ok, results[], has_more, ...}` shape. `apo-engine` subcommands honor
  `$APO_VAULT` like `apo-local` already did. `apo-engine search` itself is
  removed (below) now that both fronted the same function.
- `apo-engine index`/`apo_admin`'s `reindex` are watcher-aware: `mode=rebuild`
  signals a live watcher (never a second concurrent `index.db` writer) or
  runs inline when none is running, and `wait=true` can block on completion.
  `apo_admin(reindex, mode=rebuild)` now requires `confirm=true`
  unconditionally, not just when `force=true` — a rebuild can run inline
  (a direct write from the MCP server process) when no watcher is live.
- Vault discovery: `--vault-path` is the preferred spelling for an explicit
  filesystem root in `apo-mcp`'s discovery argv (`--vault` kept as a
  back-compat alias) — it collided in name, not meaning, with
  `apo-engine`/`apo-local`'s own `--vault NAME` (a registered `vault_id`).
  `config.env`/`config.env.example` collapse into `.env`/`.env.example`
  (the two had already drifted — live `.env` carried three OTLP vars
  `config.env` lacked).

### Removed

- **`apo-engine serve` / `rpc.py`** (the JSON-HTTP RPC server, deprecated in
  0.28.3) is deleted outright rather than left deprecated-but-shipping — it
  had zero known consumers, and every future `ops.py` change had to be
  checked against its hand-rolled JSON encoding regardless. Coverage that
  was genuinely testing `ops.py` behavior moved to direct (non-HTTP) tests;
  `apo-engine serve`, `just rpc`, and `APO_RPC_*` config are gone. See
  `docs/local-rpc.md` for a pointer to the last commit before removal.
- Dead vault-discovery config: `APO_VAULT_PATH_LIST` (never set anywhere),
  `config.VAULTS_CONFIG` (defined, never read), and a hardcoded
  atlas/meta/jeremy/notes_global vault alias table (confirmed dead — all
  real vaults resolve via collection-id or the plain legacy fallback).
  `APO_VAULTS` (the JSON registration shim) is *not* removed — it's the only
  way `apo-engine okf ingest` registers a read-only vault, a real feature
  the original plan for this cleanup didn't account for.

### Docs

- `docs/contracts/README.md`: contract checks, and the glob-semantics split
  (`okf` / `archival` use full-path match where `*` stops at `/`; `search` /
  `table` / `mermaid` use `fnmatch` where it does not).
- `telemetry-contract` template no longer claims `deny` is stripped at ingest;
  the recorder is allow-list by construction and never captured those fields.

## [0.29.0] — 2026-09-27

An aggressive CLI/MCP-surface review turned up real gaps and drift between
`apo-engine`, `apo-local`, `apo-mcp`, the RPC server, and the Go client — this
release is the resulting cleanup batch.

### Added

- **`apo` unified console script** (`apo_engine.cli_apo:main`) — additive,
  dispatches by leading token: bare note verbs (`read`/`search`/`write`/
  `append`/`patch`/`patch-table`/`graph-neighbors`/`filter`/`backlinks`/
  `history`) go to the same backend as `apo-local`; `apo engine <cmd>` goes
  to the same backend as `apo-engine` (`index`/`search`/`search-eval`/
  `stats`/`watch`/`desk-project`/`okf`/`serve`/`optima-merge`); `apo mcp`
  runs the same MCP server as `apo-mcp`. Resolves the long-standing
  `search` name collision between the two backends by namespacing
  `apo-engine`'s version under `engine`. `apo-engine`/`apo-local`/`apo-mcp`
  are unchanged and keep working as their own console scripts — nothing
  that pins those exact names (launchd plists, shell aliases, MCP host
  config) needs to change.
- **`index_health` capability** (new `apo_admin` MCP capability, plus
  `apo-engine doctor [--vault NAME] [--json]`) — per-vault index size (db
  bytes, WAL bytes), row counts across `files`/`chunks`/`vec_chunks`/
  `chunks_fts`/`backlinks`, `backlinks_per_file_max`, orphan detection for
  `vec_chunks`/`chunks_fts` (the exact class of bug fixed in 0.28.3), embed
  quarantine count, and computed `flags[]`. Both recent production bugs
  (0.28.1's backlinks growth, 0.28.3's vec_chunks orphans) were found by
  hand-reading the SQLite files — there was previously no way to catch
  either through Apo itself.
- **`apo-engine index --vacuum`** — `VACUUM`s the bound vault's index db.
  Refuses if a watcher is live (no pause/resume coordination exists yet;
  stop the watcher first) rather than risk a lock conflict.
- **Watcher-aware `index`/`reindex`.** `ops.reindex(vault, mode, force, wait,
  timeout)` is now shared by `apo-engine index` and the MCP `apo_admin`
  `reindex` capability, so both mean the same thing: `mode=flush` wakes the
  watcher's deferred queue; `mode=rebuild` signals a live watcher (never a
  second concurrent `index.db` writer) or runs inline when no watcher is
  running (no writer-race risk), and `wait=true` can block up to `timeout`
  seconds for a signaled rebuild to finish. `apo-engine index` defaults to
  this watcher-aware path; `--inline` keeps the old direct-write behavior
  and is refused when a watcher is detected live unless `--force-inline`.
  **`apo_admin(reindex, mode=rebuild)` now requires `confirm=true`**
  regardless of `force` — a rebuild can run inline (a direct write) from the
  MCP server process itself when no watcher is live, so it's gated the same
  as any other destructive admin capability.
- `--version` on both `apo-engine` and `apo-local`.
- `--no-hybrid` on `apo-local search` (matches the `hybrid=` kwarg `ops.search`
  already accepted).
- `--no-diff` on `apo-local`'s mutating commands (`write`/`append`/`patch`/
  `patch-table`) — skips the extra before/after reads that exist purely to
  print a diff, for tight scripted loops that don't need it.
- `--items @file.json` on `apo-local patch` for batch patching from a file.
- `okf_dry_run` wired into the MCP `vault` action schema — it existed in
  `ops.vault_op` but was reachable from nowhere.
- `docs/watcher-scheduler-separation.md` — a design note (not implemented)
  proposing how to separate git-sync/Optima-merge from the watcher's
  sole-index-writer loop, since a hang in either currently stalls indexing
  with no timeout guard.

### Changed

- `apo-engine search`/`stats` now route through `ops.search`/`ops.stats`
  (previously called `core.*` directly) — both CLIs and the MCP tools now
  return the same `{ok, results[], has_more, ...}` envelope shape.
  `apo-engine` subcommands now honor `$APO_VAULT` like `apo-local` already
  did.
- `apply_discovery_argv`'s `--vault-path` is now the preferred spelling for
  an explicit filesystem root (bare `--vault` kept as a back-compat alias) —
  it collided in name, not meaning, with `apo-engine`/`apo-local`'s own
  `--vault NAME` (a registered `vault_id`).
- **Go network client (`client/`) renamed `apo` → `apo-remote`.** Frees the
  `apo` name for the new unified console script above. This binary had
  reserved `apo` since 0.27.0 but has essentially no adoption yet, so it
  gives the name up. Update any build scripts installing it to
  `-o /usr/local/bin/apo-remote` and any invocations from `apo ...` to
  `apo-remote ...` — see `client/README.md`.
- `apo-local`'s `patch`/`filter` JSON input now runs through the same
  pydantic validation MCP gets before hitting `ops.py`, instead of a raw
  `json.loads()` straight through. CLI tool calls are now recorded in
  `tool_metrics` (`surface="cli"`) alongside MCP calls.

### Fixed

- `apo_admin` was annotated `readOnlyHint: True` as a whole tool, but it
  covers `delete_note` and `reindex(force=true)` alongside genuinely
  read-only actions — a host that trusts that hint for auto-approval could
  have skipped confirmation on a delete. Annotated `_MUTATE` instead.
- `Dockerfile`'s entrypoint referenced `engine/mcp/server.py`, which moved
  in 0.28.2 — the image wouldn't build. Uses the installed `apo-mcp`
  console script directly now.

### Deprecated

- **`apo-engine serve` / `engine/src/apo_engine/rpc.py`** (the JSON-HTTP RPC
  server) — a third, hand-rolled wire protocol duplicating what the
  `apo-mcp` HTTP transport (:8878) and `apo-local` already provide. Its only
  known consumer had never once succeeded in production (12,030 failed
  requests, 0 successes — no launchd job ever ran `apo-engine serve`) and
  has been switched to call `apo-local write` directly instead. Not removed
  — still fully functional, now emits a deprecation warning on start. See
  `docs/local-rpc.md`.

## [0.28.3] — 2026-09-26

### Fixed

- `_insert_pending_chunks` computed each batch's starting row id from
  `MAX(id) FROM chunks` alone. `vec0` (sqlite-vec) doesn't always honor
  rollback on a failed batch insert: when a batch's `vec_chunks` insert
  throws partway through, `chunks` correctly rolls back but `vec_chunks`
  can keep the rows it already wrote. Since `chunks.id` never advances
  past that point, every retry recomputed the same starting id and
  walked straight back into the orphaned `vec_chunks` rows, throwing
  `UNIQUE constraint failed on vec_chunks primary key` every cycle,
  forever — discovered live as a watcher crash-loop re-embedding the
  same ~56k-chunk backlog every ~2s. Starting id is now the max across
  `chunks.id`, `vec_chunks.rowid`, and `chunks_fts.rowid`, so orphaned
  debris gets skipped instead of re-collided with.

## [0.28.2] — 2026-09-25

`apo-mcp` config wiring was an absolute project-local venv path
(`engine/.venv/bin/python engine/mcp/server.py`) with a hand-set
`PYTHONPATH` — brittle across checkouts and inconsistent with how every
other CLI on the desk (`graphify`, `rtk`) lands on `PATH`.

### Added

- `apo-mcp = "apo_engine.mcp.server:main"` console script, installed via
  `uv tool install --editable "engine[mcp]"` (now part of `just setup`)
  alongside `apo-engine`/`apo-local`.

### Changed

- `engine/mcp/server.py` moved into the package proper at
  `engine/src/apo_engine/mcp/server.py` so it's importable by the new
  console script; its `__main__` block became a callable `main()`.
  Discovery-argv parsing (`--vault`/`--default`/`--collection-root`) is now
  applied unconditionally at import time rather than gated on
  `__name__ == "__main__"`, since the console script imports rather than
  execs this module.
- `watch.sh`/`launchd-watch.sh`/`config.env.example` default
  `APO_ENGINE_BIN` to `$HOME/.local/bin/apo-engine` (the `uv tool install`
  shim) instead of the venv path.
- MCP host registration (`.mcp.json`, Cursor configs, quickstart docs) now
  just `"command": "apo-mcp"` — no `args`/`cwd`/`PYTHONPATH`.

**Upgrade:** run `just setup` (or `uv tool install --editable "engine[mcp]"`
directly) to install `apo-mcp`/`apo-engine` on `PATH`, update your MCP host
config to the new `apo-mcp` command, then quit/reopen Cursor or Claude Code
(Cmd+Q) fully. `just watch-install` to pick up the new watcher binary path.

## [0.28.1] — 2026-09-25

Unbounded `backlinks` growth on files with a missing `files` row — found via
a 15GB `index-work.db` where one 30KB note had accumulated ~1.95M backlinks
rows for 56 real links.

### Fixed

- `_index_vault_impl`'s per-file reindex loop only deleted a path's old
  `backlinks` rows when it already had a `files` row. A path that ends up
  with no `files` row at all — e.g. a chunk that keeps failing to embed
  across a `files` delete/restamp boundary — looked "added" on every
  subsequent full-vault scan, and the added-branch never deleted before
  inserting. `chunks`/`vec_chunks` self-dedupe via content-hashed IDs, but
  `backlinks` has no uniqueness constraint, so every such scan appended
  another full copy of the file's links, forever. The delete is now
  unconditional, matching the already-correct targeted-reindex path
  (`_index_files_impl`).
- `engine/pyproject.toml` / `apo_engine.__version__` corrected to `0.28.1` —
  the 0.28.0 release commit updated `CHANGELOG.md` only and left both at
  `0.27.0`.

## [0.28.0] — 2026-09-08

### Added

- **`Dockerfile`** — packages the HTTP-transport MCP server; vault data is
  bind-mounted, not baked in. See its header comment for a full `docker run`
  example, including optional `APO_MCP_AUTH=google`.
- **`client/`** — `apo`, a standalone Go MCP client (streamable-HTTP), no
  local vault access, no engine dependencies. Single static binary (~8MB).
  Convenience `tools`/`search`/`read` commands plus a generic `call <tool>
  --args '<json>'` that reaches any tool the server exposes, including ones
  added after the binary was built.
- **`client/Dockerfile`** — `apo-sandbox` image (`python:3.12-slim` + `jq` +
  the `apo` binary) for Hermes Agent's Docker code-execution backend, so
  `execute_code`/`terminal` sessions can run `apo <cmd> | jq ...` pipelines
  without any local vault access or engine dependencies inside the sandbox.
- **`apo append <path>`** — wraps `append_note` for piping a command's
  output straight into a note: stdin is the note text directly, no JSON
  envelope like `call` needs. `create` defaults to `false` (matches
  `append_note`), so a typo'd path errors instead of silently creating a
  note. Note: even with `--create`, a leading YAML block in the piped text
  is not parsed as frontmatter — it lands as literal body text under an
  auto-stamped minimal frontmatter.

### Fixed

- `client`: flags now accept `--name=value` alongside `--name value` in any
  position — previously only the latter worked, which an agent generating a
  command line has no particular reason to expect.

### Changed

- **Breaking:** the `apo` console script (`apo_engine.cli_ops`, shipped in
  0.27.0) is renamed **`apo-local`**. `apo` is reserved for the new network
  client above — the two are different tools (in-process + local vault vs.
  MCP-over-HTTP + no local vault) and the collision became real once the
  network client existed. Anyone who installed 0.27.0's `apo` in the ~hours
  since it shipped: `pip install -e '.[mcp,dev]'` again to regenerate console
  scripts, and re-alias any `~/.local/bin/apo` wrapper.

## [0.27.0] — 2026-09-06

Vault-facing CLI and optional Google-auth'd MCP endpoint — both additive, no
existing client (Claude Code, Cursor, each Hermes gateway) is affected.

### Added

- **`apo` console script** — a vault-facing CLI (`apo read`/`search`/`write`/
  `append`/`patch`/…) mirroring the MCP note/vault tools, alongside the
  existing admin `apo-engine` entry. Shares the same backend
  (`apo_engine.ops`, same `index.db`) so behavior matches the MCP server
  exactly.
- **`apo_engine.mcp_auth`** — Google OIDC multi-persona auth for a FastMCP
  instance, opt-in via `APO_MCP_AUTH=google`. A plain `RemoteAuthProvider`
  resource-server verifier (not an `OAuthProxy`), so more than one person's
  separate Google OAuth client registration can authenticate against one
  endpoint. Default (`APO_MCP_AUTH` unset) is byte-identical to no `auth=`
  kwarg — every existing stdio/loopback client is unaffected. See
  `docs/systemd/apo-desma-mcp.service.example` for a dedicated public
  instance (not installed by this release).

### Fixed

- Standalone MCP instances (not the shared desk default) could hit an
  `AttributeError` on boot in `_maybe_warm_query_embed()` — it called
  `apo_vaults.bind(v)` with a raw `Vault` where `vaults.bind()` now requires
  a `VaultBinding`. Fixed to use the existing `_bound(v)` helper.

### Changed

- `dev` extra now declares `joserfc` and `starlette` explicitly (previously
  available only as `fastmcp`'s transitive deps) — both are exercised
  directly by the new auth test suite.

**Upgrade:** Quit Cursor/Claude fully (Cmd+Q) if you run a Claude Code/Cursor
session against this engine, so MCP tool schemas reload.

## [0.26.10] — 2026-09-02

Cold hybrid query-embed latency across MCP restarts.

### Added

- **`QUERY_EMBED_DISK_TTL`** — disk meta cache default 24h (in-memory stays 600s).
- **`QUERY_EMBED_KEEP_ALIVE`** — per-request Ollama `keep_alive` on query embed (default `5m`).
- **`warm_query_embed()`** — hydrate disk LRU + preload Ollama embed model.
- MCP server calls warm on startup (`APO_QUERY_EMBED_WARM_ON_START`, default on).

### Fixed

- Disk query-embed entries store normalized query text for startup hydration.
- Cold MCP hybrid search no longer misses disk cache after 10-minute in-memory TTL.

## [0.26.9] — 2026-09-01

Unscoped exclude search latency — cap hit hydration and push prefix excludes into FTS.

### Fixed

- **Unscoped + default exclude** — no longer hydrates the entire fused pool (~500 hits);
  capped at ``EXCLUDE_HIT_SCAN_MAX`` (default 96) and iterates ``ids`` not full ``ranked``.
- **Architecture queries** — promotion pool capped at ``ARCH_PROMOTE_POOL`` (48); cut to ``k``
  after reorder (was returning 48 hits to ops).
- **FTS exclude pushdown** — prefix globs (``archives/*``, etc.) applied in FTS SQL via
  ``JOIN chunks`` so excluded paths never enter the fused pool.

### Added

- Config: ``APO_EXCLUDE_HIT_SCAN_MAX``, ``APO_ARCH_PROMOTE_POOL``.

## [0.26.8] — 2026-09-01

Corpus hygiene automation, expanded thread eval, and search boost batch prefetch.

### Added

- **`engine/scripts/hygiene_batch.py`** — batch frontmatter floor, cross-vault `atlas:` link
  prefix, and dialect wikilink stubs (`areas/` + `projects/`).
- **`hygiene_apply.py`** — mechanical fix helpers used by hygiene batch.
- **`resolve_foreign_wikilink()`** — unique sibling-vault match with `atlas` preference for
  `system/*` paths; auto remediation in lint.
- **~31-query thread eval** fixture (expanded from 22; +6 threads, +2 platform plans).
- **Batch boost prefetch** — single SQL round-trip for frontmatter + backlink counts per
  search pool (P3 latency).

### Fixed

- **Wikilink path escape** — `_wikilink_candidates` rejects targets resolving outside vault root
  (e.g. `[[../../Workbench/...]]` no longer false-matches).

### Changed

Search and index performance (carried from 0.26.7 unreleased section):

### Fixed

- **`hybrid=False`** — routes to keyword-only search (no query embed, no folder
  vector scan); fixes ~25× regression on folder-scoped lex queries.
- **Lex fallback** — only runs when `hybrid=True` (hybrid fusion path).

### Added

- **Query embed disk cache** — persists in index `meta` across MCP restarts
  (`APO_QUERY_EMBED_DISK_CACHE`, default on; TTL 600s).
- **`UNSCOPED_VEC_K`** — caps global vec0 KNN on large vaults (default 48).
- **Large-folder vector approx** — global vec0 oversample + path filter when
  folder chunk count exceeds `SCOPED_VECTOR_FULL_SCAN_MAX`.
- **Vault lint sweep cache** — paginated `vault(lint)` reuses merged payload 120s.
- **Wiki index cache** — shared across lint sweeps and broken-link detection.
- **`APO_EMBED_COMMIT_BATCH`** — configurable index embed batch size.
- Tests: `test_search_perf.py`, updated pool/hybrid tests.

### Changed

- Index embed commits stream batch-by-batch (lower peak memory on large rebuilds).
- Stem index built once per `index_vault` / `index_files` batch (not per file).
- Phrase-stem folder scan prefilter skips obvious non-matches.
- CLI `--no-hybrid` help: keyword-only (was incorrectly labeled vector-only).

## [0.26.6] — 2026-08-31

Phrase-stem **candidate injection** when hybrid search misses filename matches; lex
fallback when folder exclude empties the hit pool.

### Added

- **`_inject_phrase_stem_hits`** — pulls stem phrase matches into the ranked pool.
- Lex fallback when exclude filters all fused candidates in folder-scoped search.

### Changed

- Thread eval queries: disambiguate nightwatch + soc2 labels (avoid generic ``thread`` token).

## [0.26.5] — 2026-08-31

Closes retrieval pilot P0/P1 backlog: phrase/title rank boosts, 22-query eval,
CI gate, OKF stamper skip for contract schemas, corpus lint batch script.
**QMD sidecar retired** — Apo-only thread recall (no adjacent QMD).

### Added

- **`_phrase_stem_boost` / `_title_frontmatter_boost`** — hyphenated phrase match
  on filename stem + gated title/permalink overlap from index frontmatter.
- **`is_contract_schema_path()`** — OKF stamp skips ``system/contracts/*.schema.yaml``.
- **`engine/scripts/lint_batch.py`** — paginated ``vault(action=lint, fix=true)`` sweep.
- **22-query thread eval** fixture (`docs/examples/search-eval-threads.example.yaml`).
- **CI gate:** ``test_thread_eval_fixture_hit_rate`` in ``test_retrieval_gaps.py``.
- **`engine/tests/test_okf_contract_schema.py`**.

### Changed

- Thread eval target **100% hit@5** on 22 queries (was 91.7% / 11/12 at 0.26.3).
- ``docs/search-quality.md`` — CI gate + lint batch docs.

## [0.26.4] — 2026-08-31

Thread ``exclude=`` through ``search_expanded`` lex/vec sub-queries — same widen-and-filter
semantics as :func:`search`, not a post-fusion cut only.

### Changed

- **`search_lex_only` / `search_vector_only`** — accept ``exclude``; use
  :func:`_hybrid_candidate_pools` for fetch sizing and filter excluded paths before
  returning.
- **`search_expanded()`** — passes ``exclude`` to every sub-query; widens ``pool_n`` when
  exclude is active; scans full fused rank list when exclude would under-fill ``k``.

### Added

- **`test_search_expanded_threads_exclude_glob`** — expand path honors explicit exclude
  globs on sub-queries.

## [0.26.3] — 2026-08-31

Completes the retrieval pilot P0/P1 backlog plus graph-aware rank and index-time
wikilink resolution. Thread eval fixture (12 queries) now **hit@5 91.7%**, **MRR@5
0.917** (was 75% / 0.75 in 0.26.2).

### Added

- **`docs/examples/search-eval-threads.example.yaml`** — 12 labeled thread queries for
  `apo-engine search-eval`; nightwatch expect fixed to
  `plat-790-nightwatch-request-context.md`.
- **1-hop neighbor promotion** — notes linked to top slug/ticket hits get a modest rank
  boost in `search()` and `search_expanded()` via `_neighbor_paths_for` /
  `_neighbor_rank_boost`.
- **Basename wikilink resolution at index time** — `[[slug]]` / `[[ticket-id]]` without
  path resolve against a vault stem index during batch index and `index_files()` wikilink
  insert; edges land in `backlinks` / outlinks for graph traversal and neighbor boost.
- **`engine/tests/test_retrieval_gaps.py`** — basename resolution + expanded-path boost
  coverage.

### Changed

- **`search_expanded()`** — accepts `exclude` and `explain`; applies the same
  `_path_retrieval_boost` / neighbor promotion as core search via
  `_apply_path_boosts_to_hits`.
- **`docs/search-quality.md`** — documents thread eval fixture and 0.26.3 metrics.
- **`docs/contracts/search-contract.schema.yaml`** — documents optional `folder_exclude`
  (example for pilot thread demotion).

**Upgrade:** Cmd+Q MCP hosts after pull. Re-index work vault if basename wikilinks were
previously unresolved (`just index --vault work`). Optional:
`just watch-stop && just watch-start`.

## [0.26.2] — 2026-08-31

Closes the QMD/GBrain retrieval pilot P0/P1 gaps: explainable search hits,
wiki-link graph traversal, and ticket/slug recall boosts measured on the
`areas/threads` eval slice (hit@5 75%, MRR@5 0.75 vs 0.583 pre-boost).

### Added

- **`search_notes(explain=true)`** — per-hit fusion breakdown (`fts_rank`, `vec_rank`,
  `fused`, catalog/slug/backlink boosts) plus **`path_context`** tree (usage-contract
  layout labels per path segment) on every result; `folder_context` unchanged.
- **`graph_neighbors(path, depth=1..3, direction=in|out|both)`** MCP tool + RPC
  (`POST /v1/graph_neighbors`) — index-backed wiki-link traversal (inbound
  `backlinks` + outbound `list_outlinks`); no vault walk.
- **Memory-verb map** in desk projection (compact index + full body) — intent → Apo
  MCP tool routing table (`search_notes`, `filter_notes`, `graph_neighbors`, …).
- **`engine/tests/test_retrieval_gaps.py`** — slug boost, explain/path_context,
  graph_neighbors coverage.

### Changed

- **`core.search()` post-fusion boosts** — ticket/slug filename match
  (`itops-713`, `dv-2295`, `plat-787`, …), eval-artifact `table_row` demotion
  (e.g. `apo-qmd-retrieval-pilot.md` history tables no longer outrank ticket threads),
  modest backlink-count boost; re-sort after all multipliers.
- **MCP tool count 12 → 13** (`graph_neighbors`); schema char budget ceiling raised
  accordingly.

**Upgrade:** Cmd+Q every MCP host so `graph_neighbors` and `search_notes(explain=)`
reload. Re-run `just desk-project-cursor` / `just desk-project-claude` for the
memory-verb map. Optional: `just watch-stop && just watch-start` after pull.

## [0.26.1] — 2026-08-29

### Fixed

- 0.26.0 changed the MCP call shape for `vault`/`scratchpad` but missed the runtime
  strings an agent actually reads to learn it: `vault_project.py`'s rendered
  desk-projection body (baked into `AGENTS.md` / apo-desk skills / Cursor rules every
  session), `mcp_instructions.py`'s `MCP_INSTRUCTIONS` handshake (sent to every
  connecting client), the `desk-project` CLI help text, and the two global skill/rule
  generator scripts (`write-claude-skill.sh`, `write-cursor-rule.sh`) — all still said
  `vault(action=project, vaults=[...])`. Every one of these updated; this workspace's
  own `AGENTS.md`/`.claude/rules/apo-desk.md` and the global apo-desk skill/rule files
  regenerated so no session is left operating off the stale call shape.

## [0.26.0] — 2026-08-29

Follow-up "deslopify" pass: hidden inefficiencies, naming/interface consistency, and
cognitive load across the MCP tool surface and supporting config code.

### Changed

- **BREAKING (MCP only): `vault` and `scratchpad` take one typed `request`, not flat
  kwargs.** `vault(action=X, ...)` → `vault(request={action: X, ...})`; same for
  `scratchpad`. Backed by a discriminated union on `action`
  (`apo_engine.mcp_action_schemas`, mirroring `patch_note`'s op-union pattern) with one
  request model per action carrying only the fields that action actually reads — a
  field that belongs to a different action (e.g. `to=` on `lint`) is now a schema-level
  rejection instead of a silently-ignored no-op. `apo_admin` is unchanged (its own,
  less severe shape) and out of scope. RPC/HTTP (`POST /v1/vault`, `POST /v1/scratchpad`)
  is **unchanged** — it calls the same internal functions with flat kwargs directly,
  untouched by this MCP-schema-only change; `docs/scratchpad.md` now documents both
  shapes since they've diverged. **Upgrade:** quit and restart every MCP host (Claude
  Code, Cursor, each Hermes gateway) so tool schemas reload; any saved call examples for
  `vault`/`scratchpad` need the new `request={...}` wrapper.
- `config.env_bool()` — one settled truthy/falsy word set for every boolean env var.
  `APO_WATCH_EVENTS`, `APO_RERANK`, `APO_QUERY_EXPAND`, and `APO_TOOL_METRICS` had each
  hand-rolled their own word list and drifted: `APO_WATCH_EVENTS` didn't accept `"off"`
  and didn't strip whitespace, so a form that works for `APO_RERANK` silently no-opped
  for the watcher. Also documents `APO_WATCH_EVENTS` in the README env var table (it was
  missing entirely).
- `mcp/server.py`'s `Vault` now wraps `apo_vaults.VaultBinding` whole (composition, not
  a hand-copied field list) — it was missing `read_only` entirely since that field was
  added to `VaultBinding` after `Vault` was written, a silent-drop risk for any future
  field. `_memory_status_sync`, `_reindex_sync`/`_reindex_deferred_sync`, and
  `note_resource` all go through this registry.
- `.serena/` and `.claude/` (local tool caches) added to `engine/.gitignore`.
- Committed `engine/deploy/10-memory-guard.conf`, the apo-engine systemd memory-guard
  drop-in from the 2026-08-15 OOM triage — it had sat untracked since.
- Cross-referenced the CLI's `desk-project` and the MCP `vault` tool's `project` action
  (same operation, previously unrelated-looking names on the two surfaces) in
  `project_guidance()`'s return text and the CLI's `--help`.

### Removed

- `vaults.compute_vault_id` — a dead "historical name" alias for
  `compute_collection_id` with no production callers left, kept alive only by test call
  sites.

## [0.25.4] — 2026-08-28

Follow-up from a full-repo review (architecture, correctness, tests, config/security,
recent-churn, untracked-file hygiene) run against 0.25.3.

### Fixed

- **`git_sync`: read_only vaults now also refuse unattended sync automation**, not just
  MCP writes. `sync_enabled()` only checked the git contract's own `sync.enabled` — an
  ingested foreign OKF bundle (`read_only: true`, promised to reject every write) still
  got a `VaultSyncController` built by the watcher, so a bundle whose own
  `git-contract.schema.yaml` turned on `sync` + `on_block_command` could reach
  `_notify_blocked()`'s `subprocess.run(..., shell=True)` from a routine idle tick — no
  MCP write call involved, arbitrary shell execution instead of the promised write
  rejection. `sync_enabled()` now also refuses when the resolved root matches a
  registered read_only binding.
- **`scratchpad.read_buffer_payload`: fixed a multibyte truncation boundary mismatch.**
  The oversize check counted encoded UTF-8 bytes but truncated by character count, so a
  buffer with multi-byte content (any non-ASCII text) could land arbitrarily far from
  the intended 8KiB cap — e.g. an all-2-byte-char buffer truncated to exactly double the
  budget. Now slices on the same encoded-byte budget the check uses.
- Removed an unused `_diag` import left over from 0.25.3's scratchpad create fix.
- Corrected `search()`'s docstring: `_catalog_retrieval_boost()` can push a boosted
  hit's score above 1.0 (not just just-under, as previously documented) — no behavior
  change, score is a ranking signal within one result set, not compared across queries.

### Added

- `tests/test_vault_project.py` — `vault_project.py` (1268 lines, generates the
  desk-projection content agents load every session) had no dedicated test file, only
  incidental coverage. Covers `is_contracts_rel`, the pointer helpers
  (`_pointer_vault_id`/`_abs_pointer`), `scope_desk_overlay`, and
  `_contracts_signature`/`_desk_mtime` against a real filesystem instead of mocks.

### Changed

- `.serena/` and `.claude/` (local tool caches — Serena's LSP cache, a Claude hook
  config hardcoding an absolute local path) added to `engine/.gitignore`; neither
  belongs in the shipped engine.
- Committed `engine/deploy/10-memory-guard.conf`, the apo-engine systemd memory-guard
  drop-in from the 2026-08-15 OOM triage — it had sat untracked since, one `git clean`
  from silently losing real production hardening.

## [0.25.3] — 2026-08-28

### Fixed

- **`scratchpad(create)` no longer persists a poisoned buffer when `format`
  is omitted.** Create defaulted to JSON; a prose/markdown seed normalized to
  a `JSON_PARSE` diagnostic but was still saved as an `ACTIVE` session that
  `patch()`/`commit()` would both later reject ("staging unavailable"). Now,
  with `format` omitted: try JSON, auto-promote to a YAML session if the seed
  cleanly parses as a YAML mapping, otherwise refuse with an actionable
  `create_failed` (hint: scratchpad is JSON/YAML-only; markdown notes go
  through `write_note`). An explicit `format` keeps the documented strict
  contract (a broken payload is preserved as-is with diagnostics).

## [0.25.2] — 2026-08-27

0.25.1 scoped qmd's fusion refinements to `search_expanded()` only, deliberately not
touching `search()`/`rerank.rerank_hits()` — no eval file existed at the time to
validate a change against the house rule in `docs/search-quality.md` ("no lift, no
merge"). Investigated properly this time: built a 24-query labeled eval set
(`docs/search-quality.md`'s methodology) against a real vault and measured both
refinements against `search()` directly, with real numbers instead of priors.

### Changed

- **`search()` reranking: full-override → position-aware blend.** Measured
  (`APO_RERANK=1`, 24 queries, k=5): current shipped behavior (full override)
  scored **hit@5 95.83%, MRR@5 0.795** — a real regression vs. not reranking at all
  (hit@5 100%, MRR@5 0.861), reproducing `docs/search-quality.md`'s existing
  "reranker is a marginal, opt-in refinement" finding, just more starkly on this
  set. The position-aware blend already shipped in `search_expanded()` (0.25.1),
  applied to `search()` too: **hit@5 100%, MRR@5 0.826** — recovers the miss the
  full-override reranker introduced and closes most (not all) of the gap to
  not-reranking-at-all. `rerank.rerank_hits()` (still used by callers that want
  the old full-override reorder+cut in one call) is unchanged; `search()` now
  calls `rerank.rerank_scores()` directly instead, same as `search_expanded()`.
  `Hit.score` for a reranked top hit can now land just under `1.0` rather than
  always exactly at it — documented on `search()`'s docstring.
- **Top-rank bonus stays exclusive to `search_expanded()`** — tried porting it to
  `search()`'s plain 2-list (FTS + vector) fusion and measured a regression
  (MRR@5 0.861 → 0.854, no hit@5 change): "#1 in either list" is a much weaker
  agreement signal for a single query than "#1 in several independently-generated
  sub-query variants," which is what the bonus is actually validated for.
  `search_expanded()`'s docstring corrected to stop implying otherwise.

Eval file: `~/.apo/search-eval-grid.yaml` (not checked in — vault-path-specific,
per `docs/search-quality.md` convention). All 841 tests pass (1 updated for the new
blended-score expectation).

## [0.25.1] — 2026-08-27
## [0.25.1] — 2026-08-27

Follow-up to 0.25.0's query expansion: switch to qmd's own fine-tuned expansion
model, and import the fusion refinements from qmd's architecture doc that 0.25.0
didn't yet match (top-rank bonus, position-aware rerank blending).

### Changed

- **`APO_QUERY_EXPAND_MODEL` default: `qwen3.5:4b` → `apo-query-expand`** — qmd's
  own fine-tuned expansion model (`tobil/qmd-query-expansion-1.7B`, Qwen3-1.7B SFT'd
  on `lex:`/`vec:`/`hyde:` output), not a general chat model prompted for JSON. One-
  time setup: `just setup-query-expand-model` (docs/models/qmd-query-expansion.md).
  `core.expand_query()`'s prompt/parsing rewritten for this model's native plain-text
  output (multiple `lex:`/`vec:` lines are common, unlike a single JSON object).
  `APO_QUERY_EXPAND_TIMEOUT` default `15s` → `20s` — a per-request `keep_alive` did
  not reliably keep the model warm across calls in testing (another Ollama consumer
  can evict it inside the window on a shared GPU).
- **`search_expanded()` fusion, two refinements on top of plain RRF** (scoped to
  this function only — `search()` and `rerank.rerank_hits()`, used by every other
  caller, are untouched):
  - **Top-rank bonus**: a chunk ranking #1 in *any* sub-query's own list gets +0.05
    of the pool's top score; #2-3 get +0.02 — protects an exact match for the
    original query from dilution when expanded variants disagree with it.
  - **Position-aware rerank blend**: reranking no longer fully overrides the fused
    order — blended with it by retrieval rank tier (top 1-3: 75% retrieval / 25%
    reranker; 4-10: 60/40; 11+: 40/60). New `rerank.rerank_scores()` (raw,
    unreordered scores) extracted from `rerank_hits()` as a behavior-preserving
    refactor to make this possible without touching the shared function.

4 new tests (`test_search_expanded_fusion.py`), 5 existing query-expansion tests
updated for the new model's output format, all 841 pass.

## [0.25.0] — 2026-08-27
## [0.25.0] — 2026-08-27

Two more ideas adapted from [tobi/qmd](https://github.com/tobi/qmd) (see 0.24.0).

### Added

- **Typed query expansion** — `search_notes(expand=true)` RRF-fuses `lex`/`vec`
  sub-queries instead of one hybrid pass; with `APO_QUERY_EXPAND=1`, an Ollama chat
  call (`APO_QUERY_EXPAND_MODEL`, default `qwen3.5:4b`) adds LLM-typed `lex`/`vec`/
  `hyde` sub-queries on top, each routed to exactly one backend (new
  `core.search_lex_only` / `core.search_vector_only`) — never both, unlike qmd's own
  README-documented behavior for the *original* query (searched on both). Disabled
  → behaves like plain hybrid search on the raw query, no LLM call. New
  `core.expand_query()` / `core.search_expanded()`; never raises — any backend
  failure (model down, bad JSON) falls back to the un-expanded base pair.
  - **Found in development, not qmd's problem**: the default expansion model
    (`qwen3.5:4b`) is a hybrid-thinking model — `format: "json"` without `think:
    false` burns the whole output budget on `<think>` and returns an empty
    `response`. Also sends a per-request Ollama `keep_alive` (`APO_QUERY_EXPAND_KEEP_ALIVE`,
    default `5m`) independent of the process-wide `OLLAMA_KEEP_ALIVE` — a shared-GPU
    host may deliberately want that at `0` for other consumers (measured: a cold
    4B-model load alone was ~7.4s here), so expansion keeps its own warm window
    without changing that policy.
- **HTTP MCP transport** (`APO_MCP_TRANSPORT=http`, `just mcp-http`) — a shared,
  long-lived server multiple clients can point at instead of each spawning its own
  stdio subprocess (qmd's `qmd mcp --http` pattern). DNS-rebinding protection is
  FastMCP's own `HostOriginGuardMiddleware`, explicitly enabled
  (`host_origin_protection="auto"`) — mirrors qmd's own fix for the identical bug
  class (its CHANGELOG #881: loopback binding alone doesn't stop a browser page from
  re-pointing its hostname at 127.0.0.1). `APO_MCP_ALLOWED_HOSTS` /
  `APO_MCP_ALLOWED_ORIGINS` (comma-separated) add a non-loopback client.
  - **Found in development, not qmd's problem**: FastMCP ships this guard **off** by
    default (`host_origin_protection=False`, its own docstring says "for
    compatibility") — verified with a live hostile-`Origin` request getting `200`
    before the explicit `"auto"` was added, `403`/`421` after.
    `test_mcp_http_transport.py::test_unprotected_app_would_have_allowed_hostile_origin`
    guards against a future FastMCP silently flipping that default back.

16 new tests, all 836 pass.

## [0.24.0] — 2026-08-27

Adapted from a review of [tobi/qmd](https://github.com/tobi/qmd) — its README calls
per-result descriptive context its key feature ("don't sleep on it"). Apo already
carried this data (usage-contract `layout`, previously only rendered into the static
desk-projection "Folder layout" section) — this surfaces the same per-vault dict
inline on each search hit instead.

### Added

- **`folder_context` on search hits** — `search_notes` / `query` results now carry a
  `folder_context` field (top-level-folder → one-line description, from the hit's
  vault's usage-contract `layout`) when the vault has one and the hit's folder is
  described. Absent when neither is true — no schema growth for vaults that don't use
  `layout`. `vaults.read_usage_layout()` is cached per vault root, invalidated on the
  contract file's own mtime (same pattern as the 0.23.1 `load_bindings()` fix).

## [0.23.1] — 2026-08-27

Three perf fixes, no behavior/schema changes. `pyproject.toml` / `__init__.py`
also corrected — 0.23.0 shipped without bumping them (still read 0.22.0).

### Fixed

- **`vaults.load_bindings()` was uncached** — full registry re-discovery (directory
  walk, one usage-contract YAML parse per vault, index-file resolution) ran fresh on
  *every* MCP tool call, since nearly every `ops.py` request handler calls it directly.
  Now cached (`config.BINDINGS_CACHE_TTL`, default 15s, env `APO_BINDINGS_CACHE_TTL`),
  invalidated immediately within that window on any discovery-fingerprint change (env
  vars / `registry_mtime()`). `apo_admin(reload_config)` still forces a fresh read.
  10-vault registry: 23.8ms → 0.013ms median per call.
- **Folder-scoped vector search did per-row Python L2** — `core._scoped_vector_hits()`
  computed squared L2 distance one chunk at a time in a pure-Python loop. New
  `_l2_sq_batch()` vectorizes it with numpy in 4096-row batches (soft dependency —
  numpy is transitive via the `rerank` extra, not a base dep; pure-Python fallback
  when absent). Same ranking, verified bit-identical top-k on real data.
  12.7k-chunk folder: 824ms → 181ms mean per query.
- **`vault(action=lint)` re-walked a foreign vault's entire tree per cross-vault link**
  — `note_lint.detect_broken_links()` built its foreign-vault wiki index fresh on every
  call instead of sharing one across the sweep. `lint_folder()` now threads a single
  `foreign_idx_cache` through every note, same pattern it already used for the local
  `wiki_index`. No vault currently has cross-vault wikilinks in practice — this was a
  landmine, not an active drag. Synthetic repro (30 notes → 3000-note foreign vault):
  2013ms → 67ms.

## [0.23.0] — 2026-08-26

### Changed (breaking)

- **`patch_note` MCP slim** — note/section/place ops only (8 variants). Removed `items[]` batch param from MCP (RPC `POST /v1/patch_notes` unchanged). Table row ops moved to new **`patch_table`** tool (6 variants).
- **`MCP_INSTRUCTIONS` handshake** — trimmed to ~770 chars (was ~1.9k); documents `patch_table`, `vault(action=stats)`, and per-vault `project`.
- **Claude `apo-desk` skill** — `write-claude-skill.sh` now projects **index** mode (~1KB) instead of full desk (~29KB).

### Added

- **`patch_table`** MCP tool — GFM table row/cell mutators (`update_cell`, `update_row`, `append_row`, `delete_row`, `replace_table`, `alter_table_schema`).
- **`test_patch_table_schema.py`** / **`test_mcp_schema_size.py`** — regression ceilings on tool payload size.
- **Desk index** — `vault(action=stats, days=7)` habit-check line in compact projection.

### MCP instruction size (patch_note split)

| Metric | 0.22.1 | 0.23.0 |
|--------|--------|--------|
| All tools (`list_tools`) | 36,842 chars | 29,524 chars (−20%) |
| `patch_note` tool | 17,884 (48.5%) | 5,252 (17.8%) |
| `patch_table` tool | — | 5,314 (18.0%) |
| Handshake `MCP_INSTRUCTIONS` | 1,911 chars | 773 chars |

**Upgrade:** Quit Cursor/Claude fully (Cmd+Q) so MCP tool schemas reload. Table edits: use `patch_table` instead of `patch_note` with table ops.

## [0.22.1] — 2026-08-26

### Added

- **`just desk-project-cursor`** — places compact index (`--mode index`, ~1.8KB) into `~/.cursor/rules/apo-desk.mdc` via `scripts/write-cursor-rule.sh`.
- **`just check-filter-notes-wire`** — lint script guards `filter_notes({...})` doc examples missing `where=`.

### Changed

- **Tier-2 desk projection** — removed `## Examples`, `## Contract inventory`, and `## Key directives (recap)` from `render_desk_body()` (index mode unchanged).
- **MCP Field descriptions** — deduped `expected_mtime` and region-hash suffix text in `server.py`.
- **Docs** — `vault(action=project)` described as return-only; host places via `desk-project-cursor` / `desk-project-claude`.

## [0.22.0] — 2026-08-26

### Changed (breaking)

- **Lean scratchpad** — five actions only: `create | read | patch | commit | discard`. Removed `checkout`, `duplicate`, `bind_schema`, `validate`, `status`; removed `write_note` / `append_note` / `patch_note` (`scratchpad=`) promote paths; removed 3-way merge (`scratchpad_merge.py`). Formats: **json** and **yaml** only (default json). Schema via `schema_path` / `schema_type` on **commit** only. MCP `scratchpad.ops` is a 2-variant union (`set_field`, `delete_field`) — not the full `PatchOp` surface.

**Upgrade:** Quit Cursor/Claude fully (Cmd+Q) so MCP tool schemas reload.

### Added

- **`test_scratchpad_schema.py`** — CI ceilings on scratchpad MCP tool size (≤3000 chars name+desc+params), op variants (2), property count (≤10), handshake blurb (≤220), and absence of `scratchpad=` on sibling write tools.

### MCP instruction size (scratchpad cut)

| Metric | Before | After |
|--------|--------|-------|
| All tools (`list_tools` name+desc+params) | 45,658 chars | 36,842 chars (−19%) |
| `scratchpad` tool | 10,016 (21.9%) | 1,924 (5.2%) |
| Handshake scratchpad blurb | 603 chars | 202 chars |
| `scratchpad=` on write/append/patch | 682 chars | 0 |

## [0.21.3] — 2026-08-25

Apo mutators now reject non-note paths (`.py`, scripts, dotfiles) before OKF
stamp — fixing incidents where `patch_note` prepended frontmatter to Python files.
Desk projection surfaces `note_types`, read-only vaults, and a `mutator_note_types_only`
write habit.

### Added

- **`_require_note_path()` mutator gate** — `write_note`, `patch_note`, `patch_notes`
  (per item), `append_note`, `delete_note`, `move_note` (src + dst), and `_copy_into_vault`
  dst reject paths outside `NOTE_SUFFIXES` with `unsupported_format`. Scratchpad raw
  catalog promote (json/yaml/mmd) unchanged.
- **`contribution.note_types`** on usage-contract template — projected as a normalized
  subset of the engine floor; vaults may document a narrower intent.
- **`normalize_note_types()`** — projection intersects declared suffixes with the floor;
  warns when clamping unknown entries.
- **`mutator_note_types_only` write habit** — projected into apo-desk Apo throughput.
- **Desk vault table Read-only column** — merge IR includes `read_only` from registry bindings.
- **`MCP_INSTRUCTIONS` suffix line** — mutators accept `.md` / `.yaml` / `.yml` / `.mmd` only.

### Fixed

- **`process_concept` no-op on non-note paths** — OKF soft stamp no longer fences `.py`
  and other host files when a mutator bug would have allowed a write.

## [0.21.2] — 2026-08-25

### Fixed

- **Watcher self-deadlock hardening** — poisoned cached writer connections now
  `rollback()` + `writer_close()` on sqlite errors; watch loop uses exponential
  lock/busy backoff and skips respawning vault threads that fail to join.
- **WAL bounds** — `journal_size_limit` on writer connect, passive checkpoint on
  every index finalize, truncate checkpoint when `-wal` exceeds
  `APO_WAL_LIMIT_BYTES` after a successful watch cycle.
- **Embed-drop quarantine** — chronic Ollama embed failures stamp
  `embed_quarantined` after `APO_EMBED_FAIL_QUARANTINE` drops of the same hash
  (first drop still unstamped per existing contract); retry backoff via
  `APO_EMBED_FAIL_BACKOFF`.
- **MCP read path** — `reader_connect()` uses `mode=ro`, closes stale handles
  on ping failure, `APO_DB_READ_TIMEOUT` (default 3s); ops map sqlite lock/busy
  to `{ok: false, error: "index_busy"}`.
- **Git-sync** — idle `pull_ff_only` skipped when the index is in lock-backoff
  or WAL is over limit.

## [0.21.1] — 2026-08-25

### Fixed

- **`scratchpad` MCP tool `content` parameter typed `Any`** — pydantic emits an
  open (`{}`, no `type`) JSON Schema for `Any`, unlike every sibling
  write-tool's `str | None` content field. Callers passing plain text got no
  `type: string` branch to match against, so tool-calling layers could
  misinterpret literal text as raw JSON and fail to parse it. Narrowed to
  `str | dict[str, Any] | list[Any] | None`, matching what the field actually
  accepts (string for markdown/yaml, object/array for `format=json`).

## [0.21.0] — 2026-08-24

New **read-contract** — a machine YAML alongside okf/usage-contract that tells a
consumer agent what to read for a given question, in what order, how much to
trust it, and what a status value means when reading (not writing) a note.
Additive: discovery/merge/project are already generic over contract filenames
(no engine changes needed there); vaults without a read-contract are
unaffected.

### Added

- **`read-contract.schema.yaml`** template (`docs/contracts/`) — `type_authority`
  (fixed vocabulary: `authoritative | source_of_truth | operational | reference |
  speculative | diary | record`), `purposes` (entry `okf_type` + `where=`-dialect
  filter + cross-type `read_order`), `traversals` (directed join edges), and
  `lifecycle_read` (consumer-side status semantics — single-owner split from okf
  `type_profiles.<Type>.note_status`, which keeps the status *vocabulary*).
- **Live IR on atlas** (`system/contracts/read-contract.schema.yaml`) — five
  purpose clusters (`compliance`, `project_status`, `financial`,
  `personal_knowledge`, `system_config`) and `type_authority` for all 21
  okf_types in atlas's `path_rules`.
- **`vault_project.format_read_routing_lines()`** — projects a new **Read
  routing** desk section beside Type routing (OKF), gated on `_contract_data(row,
  "read-contract")` so vaults without one never render it.
- **`note_lint.detect_read_contract_type_mismatches()`** — vault-level lint
  cross-check wired into `vault(action=lint)`: `read_contract.unknown_okf_type`
  (warn — a read-contract reference absent from okf path_rules) and
  `read_contract.missing_type_authority` (info — a path_rules okf_type with no
  read-contract entry yet).

## [0.19.0] — 2026-08-24

Tool-call telemetry can now be exported as OpenTelemetry spans, so per-session
analysis is possible for the first time. Apo also now produces conformant
OKF bundles and reads both v0.1 and v0.2 — see `docs/contracts/okf-bundle.md`
for the field-by-field compatibility table.

### Added

- **OTLP metrics backend (`store.backend: otlp | both`).** One span per `tools/call`, exported to a local OTel Collector. Contract gains `store.endpoint` (env override `APO_OTLP_ENDPOINT`, default `http://localhost:4318/v1/traces`). Optional install extra: `pip install 'apo-engine[otlp]'` — absent OpenTelemetry the backend logs once and no-ops rather than breaking tool calls. Existing `TelemetryPolicy` filtering is applied before dispatch, so `note_path` / `heading` / `chunk_hash` obey the vault contract exactly as they do for DuckDB.
  - Why spans rather than Prometheus metrics: this data is per-event forensics ("what did session X do, in what order, failing how"). Prometheus stores aggregates and cannot answer that, and a session id as a metric label is unbounded cardinality. Aggregates are derived downstream by the collector's `spanmetrics` connector instead — instrument once, get both shapes.
- **`both` fan-out backend** for the DuckDB→OTLP cutover: spans start flowing before the read path moves, so there is never a window with no telemetry surface. A failing sink cannot take down another, or the tool call.
- **Process-scoped session id fallback.** `conversation_id` was NULL on 100% of recorded calls, because clients are expected to supply one via MCP `_meta` or an `_apo` arg block and Claude Code sends neither (the only shipped injector is a Cursor hook). Under stdio, Apo is spawned as one subprocess per client session — so the process *is* the session, making a process-scoped id correct rather than merely convenient. Explicit `_meta`/`_apo` ids still win; `APO_SESSION_ID` overrides. Does **not** hold for a long-lived HTTP/SSE server shared by several clients.
- **Cross-vault link lint, skill-reference check, two-tier desk projection.** `note_lint.detect_broken_links` resolves a `vault_id:rel`-prefixed wikilink against that vault's own index (`link.unknown_vault` flaw for an unregistered id); `note_lint.detect_skill_references` flags a prose skill mention not in a caller-supplied `known_skills` list (`link.unknown_skill`), wired into `vault(action=lint)` and `scratchpad(action=validate)`; `vault(action=project, mode=index)` renders a compact always-loaded pointer surface (vault table + a directive to call `project` again per vault) for baking into static files without inflating them as per-vault detail grows.
- **`apo-engine okf validate | fix | init | export | ingest`** — one OKF implementation. `vault-tools/tools/okf/` previously carried a second copy with its own frontmatter parser and its own type map; those scripts are now shims over the engine. Only `--regenerate-indexes` is still implemented there (§6 listings, no type logic to drift).
- **Two validation profiles.** `--profile okf` is SPEC §11 conformance exactly and deliberately does *not* require `description` / `timestamp`, since §11 forbids a consumer rejecting a bundle for missing optional fields. `--profile apo` (default) keeps the stricter house producer profile.
- **`spec_type_policy: fill | mirror | off`** (env `APO_OKF_SPEC_TYPE`). Default `fill` writes `type` only when absent, so vaults using `type` as a legacy taxonomy keep their own values and stay conformant on them.
- **OKF v0.2 read support.** `okf.py` became a package with `v0_1` / `v0_2` readers behind `okf.read_concept()`, which runs both so the §13.1 fallbacks work: `generated.at` else `timestamp`, `sources` else a `# Citations` body list. Also the §11 bare-`verified`-mapping rule, the §7 actor convention, `status` / `stale_after` lifecycle, and the shared `usage_window` framing sources beneath it.
- **`generated_policy: off | forward`** (env `APO_OKF_GENERATED`) — forward-only `generated: {by, at}` emission. Existing notes are never backfilled: the engine does not know who generated content it did not write, and `generated.by` is what the trust family keys on.
- **Read-only vaults.** `"read_only": true` in an `APO_VAULTS` entry makes a vault searchable but rejects every write op with `read_only_vault`. This is what backs `okf ingest`, which registers an external bundle as its own read-only vault rather than copying foreign notes into your vault root.
- **A documented read-after-write visibility bound.** MCP never writes `index.db`, so a write is durable immediately but searchable only after the watcher runs — previously with no bound written down anywhere. `ops.index_visibility()` computes it from live config and `memory_status` reports it. It is a *scheduling* bound; `embed()` is extra, and callers needing certainty must poll rather than sleep.

### Fixed

- **Unknown `store.backend` values were coerced to `embedded` in silence.** That is how the shipped contract's `backend: duckdb` — never a valid value — went unnoticed. Unrecognised values now warn; `duckdb` and `local` are accepted aliases for `embedded`.
- **Spans queued at shutdown were lost.** MCP clients terminate stdio servers with a signal, and `atexit` does not run on one, so the final (frequently the only) spans of a session never left the process. Flush is now also driven from a SIGTERM/SIGINT handler that chains to the previous one; the batch delay dropped 5s → 1s.
- **No Apo-stamped vault was a conformant OKF bundle.** SPEC §11 requires a non-empty `type` on every concept frontmatter block, but the contract set `type_field: okf_type` and demoted `type` to `legacy_type_field`. `okf_export` never emitted `type` either, so exports were non-conformant too, and `okf_lint` accepted `okf_type or type` — a check *weaker* than the spec, passing bundles the spec rejects. Apo now emits `type` alongside `okf_type`; §11 explicitly forbids consumers rejecting a bundle for unknown additional keys, so carrying both is spec-legal and needs no vault migration.
- **The bundle-root `index.md` was asked for a concept `type`.** §11.1/§11.2 scope to *non-reserved* files; reserved filenames are governed by §11.3 and the root index legitimately carries only `okf_version`.
- **`okf validate` reported a vault with no contract as clean.** Without a contract every check is a no-op, so a newcomer running `validate --profile okf` before `okf init` got `0 violations` and exit 0 — a false clean bill of health on something that is not an OKF bundle at all. `--profile okf` now fails with a `contract` violation pointing at `okf init`; `--profile apo` warns and passes, since OKF-off is a legitimate state for a vault that never opted in.

## [0.18.1] — 2026-08-24

### Changed

- **Vault-wide mermaid retrieval boost** — on unscoped searches whose query matches architecture vocabulary (CDE, cardholder, Stripe, ECS, data flow, …), apply stronger post-fusion multipliers for `diagram.mmd` / `mermaid_*` chunks and demote catalog `pages/`; widen the fused candidate pool then re-sort. Catalog-scoped boosts unchanged. Policy-relay vault-wide hit@3: 50% → 60% (gate ≥60%).

## [0.18.0] — 2026-08-21

### Added

- **Cross-vault note copy** — `patch_note(ops=[{op:place, src, dst, allow_cross_vault:true}])` /
  `place_note(allow_cross_vault=True)` copies a note from one registered vault into another
  (both `src`/`dst` need an explicit `vault_id:` prefix; `vault=` must stay empty or it conflicts
  with the prefix). Always a copy — src is left untouched, cross-vault move stays rejected.
  OKF-validated against the *destination* vault's contract; usual `overwrite=`/`expected_mtime=`
  guards on the destination; optional `fields=` frontmatter merge on the way in. Same-vault
  `place` is unchanged and still rejects a src/dst vault mismatch unless this flag is set.
- **`vault(action=clone, vault=<from>, to=<to>)`** — scaffolds a vault from another already-registered
  vault by copying every file under `system/` (contracts, config, schemas — never vault content)
  into the destination. Existing destination files are always skipped, never overwritten.
  `dry_run=true` previews the copy/skip list without writing. Does not create or register vaults.
  Formalizes the ad-hoc "clone the contracts by hand" bootstrap previously used for e.g. `peni`
  from `lyra`.
- **`scratchpad(action=duplicate, session_id=<source>)`** — forks a scratchpad buffer (any state,
  including a `commit`-ed `PROMOTED` session — a validated, known-good template is a legitimate
  source) into a brand-new independent session, for adjusting a series of variants off one
  template without mutating it or round-tripping the vault between each. Unlike `checkout`, the
  clone has no pinned merge-base, so committing each variant to its own new `destination_path`
  behaves like a plain create rather than raising a spurious `MERGE_CONFLICT` on "trunk doesn't
  exist yet." The prior `PROMOTED`-mutation-denied error message now points here instead of
  `checkout|create`.

## [0.17.3] — 2026-08-21

### Fixed

- **`filter_notes` wire example missing `where=` — third occurrence** — 0.17.2 fixed the `filter_okf_type` habit line and doc examples but missed the sibling `filter_memory_type` habit line (rendered for vaults whose `write_habits` list the `filter_memory_type` id, e.g. Lyra's), which had the identical `filter_notes({"memory_type": "…"}, folder=…)` shape. Now reads `filter_notes(where={"memory_type": "…"}, folder=…)`.

## [0.17.2] — 2026-08-21

### Fixed

- **`filter_notes` wire example was missing `where=`** — the desk-projection habit line and every doc example (`README.md`, `docs/agent-throughput.md`, `docs/contracts/okf-bundle.md`, `docs/contracts/mermaid-notes.md`) showed `filter_notes({"okf_type": "…"}, folder=…)`, which reads as a flat top-level kwarg. The tool's actual schema only accepts a `where=` predicate (`additionalProperties: false`), so an agent following the doc literally gets a hard schema-validation error. Traced from a live incident where three identical malformed calls tripped a client-side "MCP server unreachable" circuit breaker, misreported as an Apo outage. All examples now read `filter_notes(where={...}, folder=…)`.

## [0.17.1] — 2026-08-21

### Fixed

- **Unscoped+exclude search tail latency** — widen the FTS candidate pool (`APO_EXCLUDE_CANDIDATE_FLOOR`, default 500) without forcing global vec0 KNN to the same `k`. Dense neighbors now cap at `APO_EXCLUDE_VEC_K` (default 64), fixing multi-10k-chunk indexes where search p90 was dominated by k=500 vec0 scans.
- **`__version__` sync** — align `apo_engine.__version__` with `pyproject.toml` (0.17.0 release bumped package metadata only).

### Changed

- **`WATCH_RECONCILE_INTERVAL` default** — 300 → 900 seconds in `config.env.example`.

## [0.17.0] — 2026-08-20

### Added

- **`okf_dry_run` read-only validation** — `okf.okf_dry_run(vault_root, vault_name, contract_yaml)` simulates a proposed `okf-contract.schema.yaml` against the current corpus (reclassifications, new violations, shadowed path rules) without writing anything. Wired into `vault_op(action=okf_dry_run, vault=, contract=)`.

### Changed

- **`APO_VAULTS` env cleanup** — docs, README, contract templates, and source docstrings now describe `APO_COLLECTION_ROOT` / `APO_VAULT_PATHS` / `APO_DEFAULT_VAULT` as the primary multi-vault discovery mechanism; `APO_VAULTS` is consistently relegated to a legacy roots-only compat shim (unchanged behavior, still works). Prose-only — no functional shim code changed.

## [0.16.1] — 2026-08-19

### Added

- **Mermaid catalog search tuning** — `search-contract` `folder_exclude` for scoped catalog queries; catalog slug/title prefix + entity tokens on index chunks; post-fusion retrieval boosts for `diagram.mmd` / `mermaid_*` chunks (demote catalog `pages/` table rows). `search_eval` accepts `mermaid_header` for `expect_chunk_kind: mermaid_file`.

## [0.16.0] — 2026-08-19

### Added

- **Mermaid diagram indexing** — `.mmd` files and fenced ` ```mermaid ` blocks in markdown index as `mermaid_file`, `mermaid_header`, `mermaid_node`, and `mermaid_edge` chunks (flattened embed text, catalog join for `diagrams/mermaid-catalog/`). `read_note(chunk_hash=, format=node)` on `mermaid_node` hits. Vault `mermaid-contract` tunables (`chunk_strategy`, `validation` hard/soft/off). `scratchpad(format=mmd)` workshop buffers. Search-eval fixtures for compliance catalog and work fenced diagrams. See [docs/contracts/mermaid-notes.md](docs/contracts/mermaid-notes.md).

### Fixed

- **Scratchpad promote** — JSON/YAML/MMD catalog paths write raw bytes (no OKF frontmatter wrapper); `write_note(scratchpad=)` ignores empty `content=`; `append_eof` merge path; bind_schema envelope atomicity docs.

## [0.15.2] — 2026-08-19

### Added

- **`patch_note(scratchpad=)`** — apply `ops` to spill buffer then 3-way merge-commit to `path` (markdown-only). MCP + `POST /v1/patch`. Shared `commit_session()` helper for scratchpad commit paths.

### Fixed

- **`patch_note` with `path` + place op** — early `bad_request` with tip to use place-only `patch_note(ops=[{op:place,…}], vault=…)` without `path`.

## [0.15.1] — 2026-08-18

### Fixed

- **Scratchpad frontmatter merge strips YAML comments** — mixed 3-way FM merges (both sides changed different keys) now clone the base `CommentedMap` and apply `yaml_rt` set/delete per field instead of rebuilding from plain dicts. One-sided verbatim fast path unchanged.

## [0.15.0] — 2026-08-18

### Added

- **`scratchpad` tool** — ephemeral workshop buffers under `~/.apo/scratchpads/`: `create` / `checkout` / `read` / `patch` / `validate` / `bind_schema` / `commit` / `discard` / `status`. Vault-free create/patch/read; `vault=` required for schema bind and commit. Primary schemas under `system/schemas/**/*.schema.json`; secondary `schema_type` via okf `type_profiles`. Cross-vault pins: `allow_foreign_schema` / `allow_cross_vault_schema` (default false). Section-tree + frontmatter 3-way merge on commit; `PROMOTED` forwarding pointer. Promote also via `write_note` / `append_note` (`scratchpad=`). MCP + `GET|POST /v1/scratchpad`. Dependency: `jsonschema`. See [docs/scratchpad.md](docs/scratchpad.md).

### Fixed

- **`yaml_rt` block-comment ownership** — stand-alone `#` comments immediately above a key now annotate that key (human model), not the predecessor. Deletes/edits no longer lop the wrong comments; nested map / sequence fixtures covered.

## [0.14.2] — 2026-08-18

### Fixed

- **RPC clients had no way to receive Apo's toolset-routing instructions** — the stdio MCP server hands this text to clients automatically as part of its `FastMCP(..., instructions=...)` handshake, but any client talking to the local RPC server instead (no stdio transport — e.g. the Hermes/Lyra memory-provider plugin) never received it. Extracted the shared text to `apo_engine.mcp_instructions.MCP_INSTRUCTIONS` (single source for both transports) and added `GET /v1/instructions` to serve it over RPC.

## [0.14.1] — 2026-08-18

### Fixed

- **`vault(action=project)` discarded every contract except usage-contract before rendering** — `vault_op`'s `project` action merged with `bodies=False` (strips parsed YAML from all contracts) and then re-attached full data for `usage-contract` only, via a helper whose own docstring said "other contracts stay summary-only." okf-contract, git-contract, telemetry-contract, search-contract, and local-web-contract were parsed and then thrown away before `render_desk_body()` ever saw them — only a bare `` `id` ← `path` `` line survived in "Contract inventory." `project` now merges with `bodies=True` directly; the now-dead re-attach helper is removed.
- **`write_habits` ids outside the built-in `okf_type` dialect rendered a dead-end stub** — any vault whose `write_habits` id wasn't in the hardcoded `_WRITE_HABIT_LINES` dict (e.g. a vault tagging notes by `memory_type` instead of `okf_type`) silently got `` - `{id}` — see usage-contract / apo-write-api. `` instead of real guidance. `write_habits` entries can now be `{id, text}` objects carrying vault-authored guidance directly, bypassing the dict; backfilled real entries for `task_router_threads` and `filter_memory_type`.

### Added

- **Projected desk body now surfaces usage-contract fields that were populated but never rendered**: `purpose`/`in_scope`/`out_scope` (§ Vault purpose & scope), `layout` (§ Folder layout), `frontmatter_floor` (§ Frontmatter floor), and `consult_vault_first`/`task_routing` (§ Vault directives) — the latter two promoted from an atlas-only convention into the canonical `docs/contracts/usage-contract.schema.yaml` template.
- **§ Type routing (OKF)** — a folder → `okf_type` → required-fields table rendered from each vault's okf-contract `path_rules`, truncated to fit `usage-contract.token_budget` (first real consumer of that previously-unenforced field).
- **§ Git safety** and **§ Telemetry privacy** — never-commit globs / sync block hook / restore drill from git-contract, and privacy allow/deny / retention from telemetry-contract, when present.
- **`local-web-contract` formalized**: `docs/contracts/local-web-contract.schema.yaml` template + `apo_engine.local_web_contract` loader (mirrors `search_contract.py`), giving it template/loader parity with the other lightweight contracts instead of being legacy-filename-only. Projected as § Local web browser when present.
- Projected body now names when `~/.apo/desk.yaml` is absent — `vault_roles`/`dual_write`/`workspace` were silently inert defaults with no signal in the rendered markdown (only in the JSON API's `desk_meta.source`).

**Upgrade:** no schema or config changes required — all additions are read from fields vaults may already have populated. Re-run `vault(action=project)` / `just desk-project` to see the fuller output.

## [0.14.0] — 2026-08-18

### Added

- **Optima Stage B merge** — `optima_contract` + `optima_merge` (`VaultMergeController`) tick from `apo-engine watch` after git-sync; gated by optima-contract `refresh.watch` and `OPTIMA_SYNC`. CLI: `apo-engine optima-merge`. Degrades to valid `current.yaml` when domain sources are missing. No `gws`/GCal in engine core. Idle ticks skip rewrite when only `synced_at`/`timestamp` would change; relative source paths are contained under vault roots.

**Upgrade:** Restart `apo-watch` (or `apo-engine watch`) so Optima vaults with `refresh.watch.enabled` pick up the merge tick. Desk `just optima merge` prefers the engine when installed.

## [0.13.3] — 2026-08-17

### Fixed

- **RPC dropped the connection on unquoted YAML dates** — an unquoted `timestamp: 2026-08-12` in note frontmatter loads as a `datetime.date`, which `json.dumps` cannot encode. The `TypeError` raised out of `_dispatch` and killed the handler thread *before any response was written*, so the client saw a reset socket rather than an error body. `_json_bytes` now serializes with a `default=` coercion (`datetime` / `date` / `time` → ISO 8601, other unencodable values → `str`), matching the `default=str` convention `core.py` already uses when storing frontmatter in the index. Indexed notes return frontmatter from the index, which was already string-coerced, so the failure only showed on paths that parse YAML live: reading a note the watcher has not indexed yet (an editor writes `date: 2026-08-17` and an agent reads it inside the debounce + embed window), and the 0.13.0 `ref=` catalog, which projects frontmatter straight from git blobs.

**Upgrade:** `systemctl --user restart apo-rpc` — no schema or config changes.

## [0.13.2] — 2026-08-17

### Added

- **`apo_admin list_refs`** — list reachable git heads/tags at the vault registry root for `ref=` discovery (`kind=heads|tags|all`). RPC: `POST /v1/list_refs`. Unknown `ref=` now names reachable heads and points at `list_refs` (jj colocated export habit). See [docs/contracts/git.md](docs/contracts/git.md). Fixes [#29](https://github.com/jenorris/apo/issues/29).
- **`search_notes(…, ref=)`** — FTS-only body recall at a reachable git tip (no embeddings / `chunk_hash`). Same streamed blob caps and LRU as the `ref=` catalog. Follow up with `read_note(path, ref=)`. See [docs/contracts/git.md](docs/contracts/git.md). Fixes [#31](https://github.com/jenorris/apo/issues/31).

### Fixed

- **`_TOOL_PARAM_HINTS` unique keys** — build the tool map via `_unique(**kwargs)` so a second `read_note=` is a SyntaxError (the 0.13.0 last-wins overwrite). Tests assert required params and AST-walk the source for duplicate keys. Fixes [#30](https://github.com/jenorris/apo/issues/30).

## [0.13.1] — 2026-08-17

### Fixed

- **`read_note` validation hints** — a duplicate `"read_note"` key in `_TOOL_PARAM_HINTS` overwrote the `snippet_chars` → `max_chars` hint (and the rest of the original map). CI failed on both hint tests.

## [0.13.0] — 2026-08-17

### Added

- **`ref=` catalog (read-only)** — `filter_notes(…, ref=)` and `read_note(path, ref=)` project frontmatter/YAML from a reachable git tip at the vault root (no per-branch embeddings). Cache: `ref_trees` / `ref_files` keyed by `tree_oid` (LRU 8). Writes reject `ref=`. See [docs/contracts/git.md](docs/contracts/git.md).

### Fixed

- **`ref=` catalog streaming caps** — `git cat-file --batch`, `ls-tree -z`, and `git log -z` are streamed (no unbounded `capture_output`); per-blob / listing / total size limits apply before retaining bodies; stdin write is threaded to avoid pipe deadlock; watchdog kills hung git; non-zero `cat-file` exit rejects partial catalogs. `read_blob` checks `cat-file -s` before `-p`.

**Upgrade:** Quit Cursor/Claude fully (Cmd+Q) so MCP reloads `ref=` on `filter_notes` / `read_note`.

## [0.12.2] — 2026-08-17

### Fixed

- **Idle watcher burned CPU permanently** — `apo-engine watch` sat at ~35% of a core with every vault quiet. Each vault watcher thread wakes about once per second, and two per-cycle calls did full work every time: `vault_project.maybe_reproject()` reloaded the vault registry (a usage-contract YAML parse per vault) and stat'd every contract file *before* its debounce gate, and the git-sync tick re-parsed the git contract and forked `git rev-parse --is-inside-work-tree` on every call. Cost scaled with vault count — ~24 ms + ~10 ms per vault per second, so a 10-vault desk had no idle state at all. Now: the drift scan is gated before it runs (`APO_DESK_POLL_GAP_S`, default 15 s; `force=True` still scans immediately), `load_git_contract()` caches on `(path, mtime_ns, size)`, and `is_git_work_tree()` TTL-caches its subprocess probe (`APO_GIT_WORKTREE_TTL_S`, default 300 s). Measured on a 10-vault desk: **34.7% → 0.9%** of one core idle. Indexing latency, git-sync behaviour and contract-change detection are unchanged (detection may lag by the poll gap; projection is advisory/return-only).

**Upgrade:** `systemctl --user restart apo-watch` (or restart your watcher) — no schema or config changes.

## [0.12.1] — 2026-08-17

### Changed

- **Comment-preserving YAML writes** — every YAML write site (catalog `.yaml` notes, Markdown frontmatter fences, YAML patch ops) round-trips through the new `yaml_rt` module (ruamel.yaml) instead of `safe_dump`. Comments, blank lines, quoting, flow style and block indentation survive a `set_field` / `delete_field` on another key (yq-v4 semantics). Deleting a key also drops the comments it owns (its inline comment and the block following it). Invalid YAML 1.1 timestamps (`2017-00-00`) still load as strings, PyYAML remains the fallback for documents ruamel refuses, and freshly built mappings emit exactly as before.

**Upgrade:** `pip install -e engine` (new `ruamel.yaml` dependency), then restart `apo-rpc` / `apo-watch`.

## [0.12.0] — 2026-08-15

### Added

- **Library scribe `flaws[]`** — soft OKF dual-emits structured `flaws` + prose `warnings`; write-path `format.trailing_ws` auto-fix (`status: fixed`); `vault(action=lint)` merges archival + note_lint detectors (links, usage floors, dialect, layout); opt-in `read_note(lint=true)`; `vault(stats)` KPIs `flaws.emitted` / `flaws.auto_fixed`. See [docs/library-scribe.md](docs/library-scribe.md).
- **Archival contract (suggest mode)** — vault `archival-contract` eligibility → structured `flaws[]` on writes and `vault(action=lint)` (folder/limit/offset). Agent applies `set_field` on src then `place`; `mode: auto` reserved (treated as off). See [docs/contracts/archival.md](docs/contracts/archival.md).
- Usage-contract write habits `address_flaws_on_write` / `lint_before_conclude` projected into apo-desk.

**Upgrade:** Quit Cursor/Claude fully (Cmd+Q) so MCP reloads tool schemas and `flaws[]` response fields.

## [0.11.3] — 2026-08-14

### Fixed

- **Bare `prepend` document start** — `patch_note` `op: prepend` without `heading` / `target` / `chunk_hash` inserts immediately after YAML frontmatter (or at line 0 if none), not at EOF. Headed prepend and bare `append` / `append_eof` unchanged. See [docs/patch-note-ops.md](docs/patch-note-ops.md).

**Upgrade:** Quit Cursor/Claude fully (Cmd+Q) so MCP reloads the engine.

## [0.11.2] — 2026-08-14

### Added

- **Vault-prefixed tool paths** — `path` / `folder` / `src` / `dst` / `items[].path` accept `vault_id:rel` (same grammar as desk citations). Successful responses add `qualified_path`. Writes are hard-gated to this MCP/RPC process registry (unknown prefix → `bad_vault`; prefix vs `vault=` conflict → `bad_request`). v1 patch batches must target a single vault.

**Upgrade:** Cmd+Q to reload MCP after pull. See [docs/multi-vault.md](docs/multi-vault.md).

## [0.11.1] — 2026-08-14

### Fixed

- **Empty collection-id index on rename** — if `index-{collection_id}.db` exists but has zero `files` rows, prefer a populated legacy alias (`index-meta.db` / `jeremy` / …) instead of stranding the vault on an empty db.

## [0.11.0] — 2026-08-14

Atlas PKB cutover — usage `vault_id` / docs / legacy index aliases track `atlas` (`~/Notes/Atlas`, `jenorris/atlas`). Compat still accepts legacy `meta`/`jeremy` index filenames.

**Upgrade:** Set `APO_DEFAULT_VAULT=atlas` (personal) after renaming the Meta folder; Workbench stays on explicit `APO_VAULT_PATHS`. Quit Cursor/Claude fully (Cmd+Q) so MCP reloads.

### Changed

- **Legacy index aliases** — `atlas` / `jeremy` / `meta` / `notes_global` resolve to the same pre-rename sqlite file when present.
- **Docs & examples** — multi-vault recipes and OKF/vault-tools paths use `~/Notes/Atlas` and `APO_DEFAULT_VAULT=atlas`.

## [0.10.0] — 2026-08-14

Path-list / collection-root vault discovery — no more `vaults.json` name map required. Tool-facing names come from each vault’s usage-contract `vault_id`.

**Upgrade:** Point MCP/watch at `APO_COLLECTION_ROOT` (parent of vaults) and/or `APO_VAULT_PATHS` / `--vault`, plus `APO_DEFAULT_VAULT` when more than one vault. `APO_VAULTS` still loads as a roots-only compat shim. Quit Cursor/Claude fully (Cmd+Q) after upgrade so MCP reloads. MCP and watch must share the same discovery env.

### Added

- **Path-list vault registry** — `APO_COLLECTION_ROOT` (parent directory of vaults), `APO_VAULT_PATHS` / MCP `--vault PATH`, and `APO_DEFAULT_VAULT` / `--default` replace `APO_VAULTS`/`vaults.json` as the preferred multi-vault config. Tool-facing names come from usage-contract `vault_id`. Watcher soft-removes vaults that leave the registry (index/deferred kept). See [docs/multi-vault.md](docs/multi-vault.md).

### Changed

- **`APO_VAULTS` compat shim** — JSON object keys and `collection` are ignored; roots (+ optional `index`) still load. Shim is skipped when `APO_COLLECTION_ROOT` or `APO_VAULT_PATHS` is set. Deprecation warning on stderr.
- **Default index path** — `~/.apo/index-{collection_id}.db`, with legacy `index-{vault_id}.db` (and `meta`↔`jeremy` alias) fallback when present.
- **Default vault resolution** — explicit → sole vault → unique `memory.default_vault` claim → fail if ambiguous (no sorted-first).

## [0.9.0] — 2026-08-13

Optional OTLP forwarding so Apo MCP tool calls land in the same local Jaeger stack as Cursor / just spans — additive to DuckDB habit KPIs.

**Upgrade:** `pip install -e ".[mcp,otel]"` (or add the `otel` extra to your install). Quit Cursor/Claude fully (Cmd+Q) after upgrade so MCP reloads. No schema change to existing tools.

### Added

- **OTLP span forwarding (optional)** — `record_call` now mirrors each privacy-redacted tool-use event as one OpenTelemetry span to an OTLP collector (Jaeger via otlp-mcp), **additive** to the DuckDB metrics store (which stays the queryable source for `vault(action=stats)`). Service `apo-mcp`, span `apo.tool`; the `trace_id` is derived from `conversation_id` the same way the Workbench Cursor hooks do, so Apo spans join the same Jaeger trace and nest under that conversation's session span. Span width reflects Apo's real in-process `duration_ms`, with `tool.name` / `vault_id` / `req_bytes` / `resp_bytes` / flags / `error_shape` attributes. Enablement: auto-on when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, explicit `APO_OTEL_EXPORT=1|0`, or a vault telemetry-contract `otel.export` flag (precedence: env → contract → endpoint auto). Optional dependency — `pip install apo-engine[otel]`; no-op when the SDK is absent or the collector is down (best-effort, never raises, never blocks the tool or the DuckDB write).

## [0.8.1] — 2026-08-12

Search snippet quality — reranking always scored `full_texts` (untruncated), so `search_notes`'s `snippet_chars` preview was the only thing affected here; no ranking change.

### Changed

- **`search_notes` snippet construction** no longer takes a raw `text[:snippet_chars]` slice. For `section`-kind hits: embedded GFM tables collapse to a one-line `[table: N rows — col, col, …]` marker (that table's rows are already independently indexed as `table_row`/`table_header` chunks — raw pipe/dash markup in a prose snippet was pure redundancy); decorative markdown (bold/italic/heading marks/list bullets/blockquote markers/link syntax) is stripped from prose; cuts land on a whitespace boundary, never mid-word. `table_row`/`table_header` hits are never truncated — already-flattened `label: value` text, short and dense by construction.
- **Query-anchored excerpts** — `section`-kind hits that matched via the FTS5 keyword side of hybrid search now get a `snippet(chunks_fts, …)`-windowed excerpt around the actual matching terms instead of a chunk-prefix slice, so a match deep inside a large section is no longer invisible to the caller. Vector-only hits (no FTS match) fall back to the prefix pipeline above. No reindex needed — `chunks_fts` already stores the raw text `snippet()` reads.
- **`mcp_backend`'s `table_row` content cap** (240 chars) now cuts on a word boundary via the same helper, instead of a raw slice.
- **Storage-time text split (Phase 4)** — `chunks.text` (canonical: `read_note`, `content_hash`/`expected_content_hash` preconditions) stays byte-for-byte raw; a new `_index_text_for_embedding` derives a cleaned variant (same table-collapse/decoration-strip pipeline as the snippet work above) fed to the embedder and to `chunks_fts`, for `section`-kind chunks only. Wired into all three index-write paths: `_embed_and_store_pending` (full rebuild), `index_files` (incremental/watcher re-embed — also `reembed_one`/`reembed_batch`), and `ensure_fts`'s pre-FTS backfill (previously bypassed the split entirely via a raw `INSERT…SELECT id, text FROM chunks`; now routes the same transform through a registered SQLite scalar function so bulk backfill still avoids materializing chunk text in Python). Requires re-embedding to take effect on already-indexed vaults — `apo_admin(reindex, mode=rebuild, force=true)`.

Design note + phase plan: jeremy vault `projects/apo-pkb/search-snippet-optimization.md`.

## [0.8.0] — 2026-08-12

Vault data plane as the principal product story; desk/watcher hardening for multi-vault registries.

**Quit Cursor/Claude fully (Cmd+Q)** after upgrade so MCP reloads schemas and tool descriptions.

### Added

- **Watcher registry hot-add** — multi-vault supervisor re-reads `APO_VAULTS` on `wake-registry` (or registry file mtime) and spawns threads for newly registered vaults without a full bounce. `apo_admin(reload_config)` touches the wake file after refreshing the MCP vault map. Removals / root-or-index path changes still require a watcher restart.
- **Desk projection scoping** — `merge` / `project` trim `role_notes` and `pointers` to vaults present in the active registry (`APO_VAULTS` / `vaults=`).

### Changed

- **Positioning** — README hero and stack rank lead with the vault data plane (typed `.md`/`.yaml`, contracts, `filter_notes` / surgical writes); hybrid search is retrieval substrate. OKF remains the flagship optional contract, not the product name.
- **Session-audit domain vaults** — default derivation skips `grc` (and `audit`); GRC SoT remains git/PR. Explicit `dual_write.domain_vaults` still wins when set.
- **Single-vault `APO_VAULTS` file** — still runs the multi-vault supervisor (so a one-vault registry can hot-add a second vault later). Legacy no-`APO_VAULTS` single-root mode unchanged.

## [0.7.1] — 2026-08-11

Plan-shaped frontmatter: query and surgically update list-of-dict fields (e.g. Cursor `todos:`).

**Quit Cursor/Claude fully (Cmd+Q)** after upgrade so MCP reloads schemas. **No force reindex** — matching uses existing indexed frontmatter JSON.

### Added

- **`filter_notes` `$elemMatch`** — match notes where at least one dict in a list field satisfies all inner predicates (AND). Example: `{"todos": {"$elemMatch": {"status": "pending"}}}` or correlated `{"id": "x", "status": "completed"}`.
- **Dotted / selector `where` keys** — `{"todos.status": "pending"}` expands across list elements (single-field sugar). Multi-field correlation still requires `$elemMatch`.
- **`set_field` / `delete_field` path grammar** (Markdown + YAML) — map keys, list indices (`todos.0.status`), and id selectors (`todos[id=skypad-resolver].status`). Markdown frontmatter is parse → mutate → dump (no orphaned multi-line YAML under `todos:`). Structured `value` (list/dict) passes through on Markdown.

### Changed

- Markdown `set_field` no longer does scalar line-replace; FM fence may be reformatted by `yaml.safe_dump` (comments/whitespace inside the fence are not preserved).

## [0.7.0] — 2026-08-10

Catalog sort for frontmatter sweeps (archive-ready coldest-N, stale `last_checked`, memory-reflect).

**Quit Cursor/Claude fully (Cmd+Q)** after upgrade so MCP reloads schemas. **No force reindex** — sort uses existing indexed frontmatter.

### Added

- **`filter_notes` `sort=` / `order=`** — default remains `mtime` / `desc`. Pass a safe frontmatter key (e.g. `last_activity`) with `order=asc` for oldest-first catalog pages. Missing sort values sort last for both directions.
- **`filter_notes` `has_more`** — pagination parity with `history` / `search_notes` (`offset + len(notes) < total`).

## [0.6.4] — 2026-08-08

### Fixed

- **`watch.sh start`'s directory guard rejected valid multi-vault-only setups.** It checked `-d "$APO_NOTES_ROOT"` unconditionally, but `APO_VAULTS` (the multi-vault registry) supersedes `APO_NOTES_ROOT` per `.env`'s own comment — a host with only `APO_VAULTS` set (no `APO_NOTES_ROOT` at all, the normal multi-vault desk config) always failed with `Vault does not exist: unset` even though the registry was fine. The guard now checks `APO_VAULTS` (as a file) when set, falling back to the `APO_NOTES_ROOT` directory check only for legacy single-vault setups.

## [0.6.3] — 2026-08-08

Recovers `git_sync` from diverged branches without hand-editing the vault repo.

### Fixed

- **`git_sync` could get permanently stuck on a diverged remote.** `action=pull` is (and stays) fast-forward-only by design, so the moment another concurrent session's sync won the push race, every subsequent `pull`/`run` just re-blocked with the same "fetch first" rejection — `clear_block` reset the status flag but the underlying divergence was still there to hit again on the next attempt.

### Added

- **`git_sync action=rebase`** — explicit recovery for exactly that case: fetches, then replays local commits onto `origin/<default_branch>` so the follow-up push stays a fast-forward (no `--force` ever). On a rebase conflict it aborts back to the pre-rebase state immediately (working tree never carries `<<<<<<<` markers into a note the vault would otherwise index) and blocks, same "no auto-resolve of content" contract as the rest of `git_sync`. Reached via `apo_admin(action=invoke, name=git_sync, parameters={action: "rebase"}, confirm=true)`; `confirm=true` is required, same as `run`/`pull`.

## [0.6.2] — 2026-08-08

Telemetry collection alignment + durable watcher start.

### Fixed

- **`ToolMetricsMiddleware`** records the vault registry `collection` (not process-wide `default`), so `vault(action=stats)` sees the same bucket as writes.
- **`remap_default_collections_by_vault_id`** one-shot remediates historical `default` rows that already had a correct `vault_id`.
- **`watch.sh start`** double-forks into a new session (survives Cursor/agent shell teardown) and preserves caller `APO_VAULTS` / `APO_NOTES_ROOT` overrides across `.env` source.
- **`apo_engine.__version__`** synced to package semver (was stuck at `0.4.0` while pyproject was `0.6.x`).

### Changed

- MCP `_metrics_vault_for_args` returns `(vault_id, vault_root, collection)` for multi-vault desks.

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
- **`vault` tool** — lean-visible `list` / `contracts` / `describe` / `merge` / `project` for registry + contract discovery + desk overlay. Preferred live IR: `<vault>/system/contracts/`; legacy `system/config/*-contract.schema.yaml` still discovered. Engine OKF/git loaders prefer `system/contracts/` then legacy. Desk: `~/.apo/desk.yaml` ([docs/examples/desk.example.yaml](docs/examples/desk.example.yaml)). `project` is return-only — host places via `just desk-project-cursor` (Cursor index), `just desk-project-claude` (Claude full), or Hermes (`host=hermes|all`; `APO_PROJECT_HERMES`). MCP + RPC `GET|POST /v1/vault`. Contract payloads default to summaries; `full=true` includes YAML bodies. Watcher logs when desk/contracts change — re-run desk-project to render.
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
