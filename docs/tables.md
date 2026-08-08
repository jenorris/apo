# Tables (Apo 0.6)

Apo indexes GFM pipe tables as first-class, retrievable content and lets agents edit them one row at a time without rewriting the note.

## Row indexing

Every markdown pipe table emits, in addition to its owning prose section chunk:

- one **`table_header`** chunk — schema recall (`… — Columns: Date, Mileage, Service`), and
- one **`table_row`** chunk per data row.

Each row is flattened with its heading breadcrumb and column labels so it embeds and matches natural-language queries:

```
Pacifica > Maintenance History — Date: 2026-06-07, Mileage: 114587, Service: Brake flush
```

Column labels are always kept — bare numerics embed weakly, so `Mileage: 114587` retrieves far better than `114587` alone. Tables inside fenced code blocks are ignored, and a table block is never split mid-table.

Search hits for rows carry `chunk_kind: "table_row"`, a `row_key`, and the full-chunk `content_hash` (usable as a write precondition).

### Identity

| Field | Meaning |
|-------|---------|
| `table_id` | Stable id for a table within a note (`path` + first line + ordinal). |
| `table_schema_hash` | Hash of column names + order — changes on rename/add/drop. |
| `row_key` | Natural key: the table-contract `key_column`, else the first non-empty cell, else `row-<n>`. |
| `content_hash` | Blake2b-64 of the flattened row text (heading-dependent) — the search/index staleness key. |
| `row_hash` | Blake2b-64 of the raw cell values (heading-independent) — from `read_note(format=row)`. |

## JSON in transit

The note on disk is always canonical markdown; structured formats are opt-in per call:

- **`read_note(chunk_hash=<section>, format=json)`** → `{"headers": [...], "rows": [[...]]}` for the table in that section.
- **`read_note(chunk_hash=<row>, format=row)`** → `{"columns": {col: val}, "row_key": …, "row_hash": …}`.
- **`write_note(sections=[{content_type: "csv"|"json"|"table_json", content: …}])`** → serialized to a GFM table that indexes as rows.

## Row-key ops (`patch_note`)

Row ops address a table by `table_id` (from a search hit) or `heading` (or omit both when the note has one table). They default to `strict=true` and **cannot be mixed** with prose/frontmatter ops in one call.

| Op | Purpose | Precondition |
|----|---------|--------------|
| `update_cell` | Set one cell in a row. | `expected_content_hash` **or** `expected_row_hash` (required) |
| `update_row` | Set several columns in a row. | same |
| `delete_row` | Remove a row by `row_key`. | same |
| `append_row` | Add a row (`row: {col: val}`; unknown columns rejected). | none |
| `replace_table` | Bulk `rows[]`/`csv` with `merge: replace\|append\|upsert`. | fuzzy header map |
| `alter_table_schema` | `rename_columns` / `add_column` / `drop_column`. | `confirm=true` |

Example — correct a mileage value:

```json
{
  "path": "areas/vehicle/pacifica.md",
  "ops": [{
    "op": "update_cell",
    "row_key": "2026-06-07",
    "column": "Mileage",
    "value": "114600",
    "expected_row_hash": "…from read_note(format=row)…"
  }]
}
```

A stale precondition returns `error: "stale_write"` with the fresh hash. Responses return the new `content_hash` / `row_hash` and `reembed: "pending"` — re-search (or wait for the watcher) before reading the row by `chunk_hash`.

## Column-op gate

`alter_table_schema` rewrites every row's flattened text, so it re-embeds the whole table. It is gated behind `confirm=true` to keep an accidental rename from silently re-embedding a large table. Prefer `update_cell` / `update_row` for value edits — those re-embed only the touched row (plus the owning prose section, which carries the raw table).

## Bulk ingest + fuzzy headers

`replace_table(merge=append|upsert)` and CSV imports map incoming headers to the existing schema by normalized fuzzy match. On a tie or low-confidence match the op **rejects** with `header_ambiguous` and per-column suggestions rather than guessing:

```json
{"error": "header_ambiguous",
 "suggestions": [{"incoming": "servce", "candidates": [{"column": "Service", "score": 0.83}]}]}
```

Pass an explicit column mapping or `allow_new_columns=true` to proceed. Duplicate `row_key`s within one import are last-wins.

## Re-embedding

The watcher is the sole index writer. A row/table write rewrites the file and enqueues the path; the watcher's `reembed_one` / `reembed_batch` re-index via `index_files`, which reuses unchanged rows by `content_hash` — so a one-cell edit re-embeds only the changed row. MCP never writes the index.

See also [toc-navigation.md](toc-navigation.md) and [contracts/table-contract.schema.yaml](contracts/table-contract.schema.yaml).
