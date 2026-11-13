# Glossary

- **Vault:** A filesystem root that contains knowledge files and a usage contract with a stable `vault_id`.
- **Contract:** A machine-readable YAML policy under `system/contracts/` that defines optional vault behavior.
- **Desk projection:** The generated host guidance that merges desk policy with selected vault contracts. It is not vault content.
- **Catalog:** The rebuildable SQLite view of indexed files, metadata, chunks, links, and search data.
- **History:** The index-backed mtime browse view, or file-level Git commits when a Git contract enables them.
- **Collection ID:** An internal stable identifier for one vault root. Apo uses it for index files, queues, and telemetry. It is not the public `vault_id` and is not configurable.
