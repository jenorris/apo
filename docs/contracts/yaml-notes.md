# YAML catalog notes

**Status:** shipped (engine) · optional vault habit

Standalone `.yaml` / `.yml` files are first-class Apo notes alongside Markdown.

## Model

| Format | Catalog | Body / search | Writes |
|--------|---------|---------------|--------|
| `.md` | YAML frontmatter fence | Heading chunks + hybrid search | `append_note`, heading patch, `set_field` (parse/dump; paths + lists) |
| `.yaml` / `.yml` | **Whole file** (must be a mapping) | Embeds `title` / `description` / `okf_type` / `status` / `resource` | `write_note`, `patch_note` `set_field` / `delete_field` (dotted paths, list indices, `[id=…]`) |

Machine contracts (`system/contracts/*-contract.schema.yaml`, legacy `system/config/`) are **ignored** by the indexer by default.

## Agent habits

1. Structure-only atoms (queues, inventories, thin OKF trackers) → `.yaml` under a clear folder (`records/`, `inbox/*-state.yaml`, …).
2. Prose, History, daily session log → stay Markdown + `append_note`.
3. Status work: `filter_notes(+fields, folder=…)` → `patch_note(set_field)` + `expected_mtime`.
4. Nested updates: `{"op":"set_field","field":"meta.owner","value":"jeremy"}` or list paths `todos.0.status` / `todos[id=x].status`.
5. Do not call `append_note` or heading ops on YAML — expect `unsupported_format`.
6. Successful field patches may return **`flaws[]`** (e.g. usage `frontmatter_floor`) — correct via `set_field`, do not ignore. Soft OKF dual-emits `warnings` + `flaws` during the compat window.

See [okf-bundle.md](./okf-bundle.md), [agent-throughput.md](../agent-throughput.md), and [library-scribe.md](../library-scribe.md).
