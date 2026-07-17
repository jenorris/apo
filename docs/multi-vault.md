# Multi-vault (true multi-index)

Apo can serve **multiple vault roots**, each with its own sqlite index and deferred-queue collection, in one MCP process / one `apo-engine watch`.

## Quick setup

`~/.apo/vaults.json`:

```json
{
  "default": "meta",
  "vaults": {
    "meta": {
      "root": "/Users/YOU/Notes/Meta",
      "index": "/Users/YOU/.apo/index-meta.db",
      "collection": "meta"
    },
    "work": {
      "root": "/Users/YOU/Notes/Work",
      "index": "/Users/YOU/.apo/index-work.db",
      "collection": "work"
    }
  }
}
```

```bash
export APO_VAULTS="$HOME/.apo/vaults.json"
just index --vault meta
just index --vault work
just watch-fg   # one thread per vault
```

MCP tools take `vault=` (name). Empty → `default` from the JSON.

Each vault may ship its own contract at `system/config/okf-contract.schema.yaml` (see [contracts/](./contracts/)).

## Nested roots

Parent vault should `.indexignore` the child folder so files are not double-indexed. Child is a separate `root` / `index` / `collection` entry.

## Legacy

Without `APO_VAULTS`, behavior is unchanged: one vault from `APO_NOTES_ROOT` / `APO_INDEX` / `APO_COLLECTION`, named `default`.

## Agent habit

```python
search_notes("open IRLs", folder="projects/pci-2026", vault="meta")
filter_notes({"okf_type": "Thread"}, folder="areas/threads", vault="meta")
write_note("inbox/capture.md", "…", vault="work")
```
