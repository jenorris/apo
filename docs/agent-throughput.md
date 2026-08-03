# Agent throughput (Apo)

Habits that cut MCP round-trips more than further embed latency work. Desk agents should treat these as defaults.

## Decision tree (before any Apo call)

1. **Catalog / status / `okf_type`** → `filter_notes` + `fields=` (+ `folder=`) — works for MD frontmatter **and** `.yaml` / `.yml` catalog notes
2. **Known path** → `read_note` / `append_note` / `patch_note` / `write_note` (skip search)
3. **Meaning recall** → `search_notes` with **`folder=`** when PARA bucket is known
4. **Need more than a snippet** → `expand_chunk(chunk_hash)` (not full-file `read_note`)
5. **Append/edit from a hit** → `append_note(chunk_hash=…)` (path optional) or `patch_note` op with `chunk_hash` — do **not** `read_note` only to obtain an anchor
6. **Dual-write** → parallel tools in one turn, same `vault=`
7. **Multi-path patch** → `patch_note(items=…)` (not session log)
8. **Structure-only atom** → prefer `write_note` / `patch_note(set_field)` on a `.yaml` path (no `append_note` / headings)

## Hard defaults

- Require **`folder=`** whenever the PARA bucket is known; unscoped search is a smell.
- Require **`expected_mtime`** on any second write to the same path in a turn.
- Cap full-file `read_note` (no `heading` / `max_chars`) unless doing a full rewrite.
- Dual-write must be **parallel** MCP calls; end-of-turn gate fails if only one side landed.
- `patch_note` ops need discriminator **`op`** and keys `field` / `find` / `replace` — never invented `key` / `old` / `new`.

## Fast path (cheat card)

```
filter(+fields) → search(+folder) → expand_chunk → append(chunk_hash)  # path optional (MD)
                                              ↘ patch(set_field|…, chunk_hash|heading) + expected_mtime
YAML atoms: filter(+fields) → patch(set_field dotted path) + expected_mtime
```

## Soft engine tips

Successful responses may include a non-fatal **`tip`** field:

- `search_notes` without `folder=` → tip to pass `folder=`
- Second in-process write to the same path without `expected_mtime` → tip to thread mtime
- Stale `chunk_hash` with path+heading fallback → tip to re-search for a fresh hash

Do not treat `tip` as failure; `warning` remains for watcher / path issues.

## Weekly metrics gate

From the engine checkout:

```bash
just tool-stats
```

Targets (desk, rolling 7d, primary collection):

| Metric | Target |
|--------|--------|
| `folder_set` / `search_notes` calls | ≥ 80% |
| `filter_notes` calls vs `search_notes` | rising share for status/catalog work |
| `expected_mtime_set` / write calls | rising; dual-writes should thread mtime |
| `patch_note` ValidationError rate | near zero |
| `expand_chunk` | > 0 when search→section is common |

Lean desk hides MCP `tool_stats` — use the CLI. Disable metrics: `APO_TOOL_METRICS=0`.

## End-of-turn checklist

Before the final reply on an Apo turn:

1. `folder=` on search/filter when known?
2. `fields=` on status sweeps?
3. Parallel dual-write same `vault=`?
4. `mtime` → `expected_mtime` on follow-up writes?
