# ToC & navigation (Apo 0.6)

`read_note` gains document-level scoping so an agent can orient on a note's shape before pulling any section body, then walk sections without re-reading the whole file.

## Modes

| Call | Returns |
|------|---------|
| `read_note(path, mode=toc)` | Heading outline only — one entry per heading with `title`, `level`, `chunk_hash`, `bytes`. No section bodies. |
| `read_note(path)` (default) | Body with a frontmatter sidecar; large files return a preview + a tip to narrow. |
| `read_note(chunk_hash=…)` | One section (or table row), with `breadcrumb` and a `nav` cursor. |

The ToC-first flow keeps context small:

```
read_note(mode=toc) → pick a heading → read_note(chunk_hash=<that heading>)
```

Each ToC entry's `chunk_hash` is a live anchor for the follow-up section read, so no second lookup is needed.

## Sibling hops

Section reads return a lean `nav` cursor rather than always-on sibling arrays:

```json
"nav": {"position": 2, "count": 4,
        "prev": "<chunk_hash>", "next": "<chunk_hash>"}
```

- **`read_note(chunk_hash=…, sibling="next")`** / **`sibling="prev"`** hops to the adjacent section **at the same depth** (a `##` skips over nested `###` bodies to the next `##`).
- Ask for **`siblings=true`** only when you explicitly want the neighbor list; the default stays lean.

## Hash staleness

`chunk_hash` is a composite of the section's byte span + content hash. Editing a note changes the hashes of touched (and, for tables, structurally shifted) chunks:

- A search/ToC `chunk_hash` that no longer resolves returns a **path + heading fallback** plus a tip to re-search for a fresh hash.
- After a **table row/table write**, the response returns `reembed: "pending"` — the rewritten rows are on disk but not yet re-indexed. Re-search or wait for the watcher before reading a row by `chunk_hash`; meanwhile address rows by `row_key` / `table_id`.
- For hot notes, thread `expected_mtime` (and, when editing a section, `expected_content_hash`) from the prior read into the next write.

## Pagination

`search_notes`, `history`, and `backlinks` accept **`offset`** and return **`has_more`**:

```
search_notes(query, limit=10)              # page 1
search_notes(query, limit=10, offset=10)   # page 2; stop when has_more is false
```

Pages do not repeat prior hits, so an agent can walk a large result set deterministically.

## Tool mapping

| Goal | Call |
|------|------|
| Outline a note | `read_note(mode=toc)` |
| Read one section | `read_note(chunk_hash=…)` |
| Next/prev section | `read_note(chunk_hash=…, sibling=next\|prev)` |
| Table as JSON | `read_note(chunk_hash=<section>, format=json)` |
| One row as columns | `read_note(chunk_hash=<row>, format=row)` |
| Next page of hits | `search_notes(…, offset=…)` |

See [tables.md](tables.md) for structured table reads and row edits, and [agent-throughput.md](agent-throughput.md) for the end-to-end flow.
