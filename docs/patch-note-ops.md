# patch_note ops (agent UX)

Discriminated union on `op`. Files are source of truth; this document is the
wire contract for MCP / RPC clients.

**Alias freeze:** keep existing aliases (`target`≡`heading`, top-level
`heading`≡`scope.heading`, `limit`≡`top_k`, `filters`≡`where`,
`content`≡`text` on `append_note`, `text`≡`content` on `write_note`). Do **not**
add new aliases without an agent-success regression and a docs bump.

## Write routing (append paths)

| Need | Tool |
|------|------|
| Session log / History / post-search add | **`append_note`** (preferred) |
| Frontmatter + section mutate in one call | **`patch_note`** (`set_field`, `replace_*`, …) |
| Append text *while* batching other ops | `patch_note` `append` / `prepend` / `append_eof` |
| Create / full overwrite | `write_note` — **no** `append` param (use `append_note`); optional `scratchpad=` session id as content source |
| Ephemeral workshop / validate / merge-promote | **`scratchpad`** — see [scratchpad.md](./scratchpad.md); same `ops[]` dialect; promote also via `write_note` / `append_note` / `patch_note(scratchpad=)` |
| Dual-write (domain + daily) | **Parallel** `append_note` / `patch_note` in one turn |
| Multi-path patch-only (N≥2) | **`patch_note`** with `items: [{path, ops, expected_mtime?}]` (max 20; XOR with path+ops) |
| Move / host promote / cross-vault copy | **`patch_note(ops=[{op:place, src, dst, …}])`** — place-only, no `path`; move if src in vault; copy host `.md` into the vault; copy across vaults when `allow_cross_vault=true` (always copy, never move — rejected otherwise) |
| Status / frontmatter sweeps | `filter_notes(where=…, fields=["status","okf_type","last_checked","title"])` |
| Browse by mtime / file git log | `history` (`since`/`until`, `preview=first\|last`, `heading=`, `exclude=`, `fields=`, `chunk_hash`; `path=` + git contract → commits) — status sweeps still use `filter_notes` |

Thread `mtime` from read/write into **`expected_mtime`** on the next mutate for
the same path (`place_note` guards **src** for in-vault moves, **dst** for host copies).
When file mtime advanced from an *unrelated* edit, scoped writes may still proceed if
**`expected_frontmatter_hash` / `expected_body_hash` / `expected_content_hash`** match
(or a same-process prior `read_note` / `expand_chunk` left a region snapshot). Reads
return those hashes; search hits include `content_hash` for the chunk text.

## Roles (not one overloaded “anchor”)

| Role | Meaning | Ops | Wire keys |
|------|---------|-----|-----------|
| **target** | Section identity / append location | `replace_section`, `append`, `prepend` | `heading` (canonical), `target` (alias), **`chunk_hash`** (search hit) |
| **scope** | Search bound for find/replace | `replace_text` | `scope.heading` (canonical), top-level `heading` (alias), **`chunk_hash`** / `scope.chunk_hash` |

Conflicting alias pairs (`heading` vs `scope.heading`, or `target` vs `heading`)
raise validation / `invalid_op` errors. Prefer canonical keys in new calls.

`chunk_hash` is resolved to a heading before apply. If the hash is missing from
the index but the op still has `heading` (and the note `path` is known), Apo
retries by heading and returns a soft `tip` to re-search.

## Ops (canonical shapes)

| op | Required | Optional |
|----|----------|----------|
| `set_field` | `field` | `value` |
| `delete_field` | `field` | — |
| `replace_text` | `find` | `replace`, `count`, `scope.heading` / `heading`, `chunk_hash` / `scope.chunk_hash` |
| `replace_section` | `heading` \| `target` \| `chunk_hash` | `text` |
| `append` / `prepend` | `text` | `heading` \| `target` \| `chunk_hash`, `position` |
| `append_eof` | `text` | — |

```json
{"op": "set_field", "field": "status", "value": "active"}
```

```json
{"op": "set_field", "field": "todos[id=skypad-resolver].status", "value": "completed"}
```

```json
{"op": "set_field", "field": "todos", "value": [{"id": "a", "content": "…", "status": "pending"}]}
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
double headers.

**Bare (no heading / target / chunk_hash):**

| Op | Where text lands |
|----|------------------|
| `prepend` | Document body start — immediately after YAML frontmatter (or line 0 if none) |
| `append` / `append_eof` | EOF |

Headed `prepend` still inserts at the start of that section’s body.

Normalization (`ops_to_dicts` / apply path) strips aliases so the engine sees one
canonical shape per op.

## Frontmatter field paths (`set_field` / `delete_field`)

| Segment | Meaning | Example |
|---------|---------|---------|
| map key | Nested object key | `meta.owner` |
| list index | Element when parent is a list | `todos.0.status` |
| id selector | First list dict with matching field | `todos[id=skypad-resolver].status` |

Markdown notes: frontmatter is **parsed → mutated → `yaml.safe_dump`'d** back into
the `---` fence (body unchanged). Multi-line keys like `todos:` update as a
subtree — no orphaned continuation lines. Fence whitespace/comments may be
reformatted. Pass structured `value` as a native list/dict (not a stringified
repr). String scalars stay strings (ISO timestamps are not coerced to dates).

YAML catalog notes use the same path grammar (maps, indices, `[id=…]`).

## `filter_notes` nested match (related)

- `$elemMatch` — at least one list dict satisfies all inner predicates (AND).
- Dotted sugar — `{"todos.status": "pending"}` matches any element's field.
  Multi-field correlation on the **same** element requires `$elemMatch`.
