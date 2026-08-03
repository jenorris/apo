# YAML catalog notes

**Status:** shipped (engine) · optional vault habit

Standalone `.yaml` / `.yml` files are first-class Apo notes alongside Markdown.

## Model

| Format | Catalog | Body / search | Writes |
|--------|---------|---------------|--------|
| `.md` | YAML frontmatter fence | Heading chunks + hybrid search | `append_note`, heading patch, `set_field` |
| `.yaml` / `.yml` | **Whole file** (must be a mapping) | Embeds `title` / `description` / `okf_type` / `status` / `resource` | `write_note`, `patch_note` `set_field` / `delete_field` (dotted paths) |

Machine contracts (`system/config/*-contract.schema.yaml`, `okf-profile.schema.yaml`) are **ignored** by the indexer by default.

## Agent habits

1. Structure-only atoms (queues, inventories, thin OKF trackers) → `.yaml` under a clear folder (`records/`, `inbox/*-state.yaml`, …).
2. Prose, History, daily session log → stay Markdown + `append_note`.
3. Status work: `filter_notes(+fields, folder=…)` → `patch_note(set_field)` + `expected_mtime`.
4. Nested updates: `{"op":"set_field","field":"meta.owner","value":"jeremy"}`.
5. Do not call `append_note` or heading ops on YAML — expect `unsupported_format`.

See [okf-bundle.md](./okf-bundle.md) and [agent-throughput.md](../agent-throughput.md).
