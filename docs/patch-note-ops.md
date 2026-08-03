# patch_note ops (agent UX)

Discriminated union on `op`. Files are source of truth; this document is the
wire contract for MCP / RPC clients.

**Alias freeze:** keep existing aliases (`target`≡`heading`, top-level
`heading`≡`scope.heading`, `limit`≡`top_k`, `filters`≡`where`). Do **not** add
new aliases without an agent-success regression and a docs bump.

## Write routing (append paths)

| Need | Tool |
|------|------|
| Session log / History / post-search add | **`append_note`** (preferred) |
| Frontmatter + section mutate in one call | **`patch_note`** (`set_field`, `replace_*`, …) |
| Append text *while* batching other ops | `patch_note` `append` / `prepend` / `append_eof` |
| Create / full overwrite | `write_note` — **no** `append` param (use `append_note`) |
| Promote host `.md` into vault | **`send_note(src, dst, fields=?)`** — absolute host path; leaves src |
| Dual-write (domain + daily) | **Parallel** `append_note` / `patch_note` in one turn |
| Multi-path patch-only (N≥2) | **`patch_note`** with `items: [{path, ops, expected_mtime?}]` (max 20; XOR with path+ops) |
| Move / host promote | **`place_note`** — move if src in vault; else copy host `.md` |
| Status / frontmatter sweeps | `filter_notes(where=…, fields=["status","okf_type","last_checked","title"])` |
| Browse by mtime / file git log | `history` (`path=` + git contract → commits) — status sweeps still use `filter_notes` |

Thread `mtime` from read/write into **`expected_mtime`** on the next mutate for
the same path (including `move_note` on **src**, `send_note` on **dst**).

## Roles (not one overloaded “anchor”)

| Role | Meaning | Ops | Wire keys |
|------|---------|-----|-----------|
| **target** | Section identity / append location | `replace_section`, `append`, `prepend` | `heading` (canonical), `target` (alias) |
| **scope** | Search bound for find/replace | `replace_text` | `scope.heading` (canonical), top-level `heading` (alias) |

Conflicting alias pairs (`heading` vs `scope.heading`, or `target` vs `heading`)
raise validation / `invalid_op` errors. Prefer canonical keys in new calls.

## Ops (canonical shapes)

| op | Required | Optional |
|----|----------|----------|
| `set_field` | `field` | `value` |
| `delete_field` | `field` | — |
| `replace_text` | `find` | `replace`, `count`, `scope.heading` (**or** alias `heading`) |
| `replace_section` | `heading` (**or** alias `target`) | `text` |
| `append` / `prepend` | `text` | `heading` (**or** alias `target`), `position` |
| `append_eof` | `text` | — |

```json
{"op": "set_field", "field": "status", "value": "active"}
```

```json
{"op": "delete_field", "field": "draft"}
```

```json
{"op": "replace_text", "find": "old", "replace": "new", "scope": {"heading": "## Summary"}}
```

```json
{"op": "replace_section", "heading": "## Summary", "text": "Replacement body\n"}
```

```json
{"op": "append", "heading": "## History", "text": "- 2026-07-24 — note\n"}
```

```json
{"op": "prepend", "heading": "## Session log", "text": "**2026-07-24 08:00 ET** — …\n\n"}
```

```json
{"op": "append_eof", "text": "\n---\nfooter\n"}
```

**`text` is body only.** Pass the section via `heading` / `target` (or
`chunk_hash` on `append_note`). A leading markdown heading that repeats the
anchor title is stripped on `append` / `prepend` / `replace_section` so
clients that copy `read_note(..., heading=…)` into `text` do not write
double headers. EOF append (`append_eof` / no heading) does not strip.

Normalization (`ops_to_dicts` / apply path) strips aliases so the engine sees one
canonical shape per op.
