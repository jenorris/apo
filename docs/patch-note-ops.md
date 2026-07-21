# patch_note ops (agent UX)

Discriminated union on `op`. Files are source of truth; this document is the
wire contract for MCP / RPC clients.

## Roles (not one overloaded “anchor”)

| Role | Meaning | Ops | Wire keys |
|------|---------|-----|-----------|
| **target** | Section identity / append location | `replace_section`, `append`, `prepend` | `heading` (canonical), `target` (alias) |
| **scope** | Search bound for find/replace | `replace_text`, `check_item` | `scope.heading` (canonical), top-level `heading` (alias) |

Conflicting alias pairs (`heading` vs `scope.heading`, or `target` vs `heading`)
raise validation / `invalid_op` errors.

## Ops

| op | Required | Optional |
|----|----------|----------|
| `set_field` | `field` | `value` |
| `delete_field` | `field` | — |
| `replace_text` | `find` | `replace`, `count`, `scope.heading` **or** `heading` |
| `check_item` | `item` | `checked` (default true), `count`, `scope.heading` **or** `heading` |
| `replace_section` | `heading` **or** `target` | `text` |
| `append` / `prepend` | `text` | `heading` **or** `target`, `position` |
| `append_eof` | `text` | — |

Prefer **`check_item`** for checkbox flips instead of scoped `replace_text`.

```json
{"op": "check_item", "item": "Send HECVAT", "heading": "## Next action"}
```

```json
{"op": "replace_text", "find": "old", "replace": "new", "heading": "## Summary"}
```

Normalization (`ops_to_dicts` / apply path) strips aliases so the engine sees one
canonical shape per op.
