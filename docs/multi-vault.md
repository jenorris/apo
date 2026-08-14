# Multi-vault (path discovery)

Apo serves **multiple vault roots**, each with its own sqlite index and deferred-queue collection, in one MCP process / one `apo-engine watch`.

Separate indexes are intentional: isolation (no cross-vault ranking bleed), independent rebuild/lock blast radius, and subset search via `vault=` / `vaults=[]` — not one shared database with a vault filter column.

## How vaults are discovered

| Mechanism | Role |
|-----------|------|
| `APO_COLLECTION_ROOT` / `--collection-root` | **Primary autoconfigure** — parent directory of vaults. Each immediate child with a usage-contract `vault_id` is registered. Non-vault siblings (e.g. `Wiki/`) are skipped. |
| `--vault PATH` (MCP) / `APO_VAULT_PATHS` | Explicit roots (colon-separated). Workbench escape hatch for non-sibling trees (e.g. `compliance`). |
| `APO_DEFAULT_VAULT` / `--default` | Default `vault=` when empty (required when more than one vault and no unique `memory.default_vault` claim). |
| `APO_VAULTS` (compat) | JSON file/object — **roots only**; object keys and `collection` are ignored. Names come from usage `vault_id`. Optional `index` still honored during cutover. Emits a deprecation warning. |
| `APO_NOTES_ROOT` | Legacy single-root when nothing above is set. |

A path is a vault iff it has a readable usage contract with non-empty `vault_id` and is not marked `system/contracts/.apo-disabled`. Duplicate `vault_id` across roots → hard fail.

**Naming:** `APO_COLLECTION_ROOT` means “parent directory of vaults,” **not** a queue/telemetry id. The internal deferred/telemetry partition id is derived (`compute_collection_id`) from the vault’s git root commit (or a cached random id).

### Default vault resolution

1. `APO_DEFAULT_VAULT` / `--default` if set (must match a loaded `vault_id`)
2. Compat `default` field from `APO_VAULTS` when it matches a loaded `vault_id`
3. Exactly one vault → that vault
4. Exactly one loaded usage-contract with `memory.default_vault` uniquely naming a loaded vault
5. Otherwise **fail at process start** (no silent sorted-first pick)

### Indexes

Default path: `~/.apo/index-{collection_id}.db`. If that file is missing but a legacy `~/.apo/index-{vault_id}.db` exists, the legacy path is used (cutover). Soft-removing a vault keeps its index and deferred queue on disk.

## Desk recipes

**Personal (all Notes children that are vaults):**

```bash
export APO_COLLECTION_ROOT="$HOME/Notes"
export APO_DEFAULT_VAULT=atlas   # or rely on a unique memory.default_vault claim
```

**Workbench (subset — never collection-root on all of Notes):**

```bash
export APO_VAULT_PATHS="$HOME/Notes/Work:$HOME/Notes/Contracts:$HOME/Notes/Optima:$HOME/Workbench/compliance"
export APO_DEFAULT_VAULT=work
```

MCP argv equivalent:

```bash
python engine/mcp/server.py \
  --vault ~/Notes/Work \
  --vault ~/Notes/Contracts \
  --vault ~/Notes/Optima \
  --vault ~/Workbench/compliance \
  --default work
```

**Hard gate:** MCP and `apo-engine watch` / launchd must use the **same** discovery env. Mismatch is a support footgun.

```bash
just index --vault work
just watch-fg   # one thread per vault; soft-remove + hot-add without full restart
```

`search_notes` accepts `vaults=["work", "contracts"]` to fan out across those indexes, merge hits by score, and return top `limit` (each hit stamped with `vault`). Do not pass `vault=` and `vaults=` together.

## Qualified paths (`vault_id:rel`)

Tool path / folder args accept an optional **`vault_id:relative/path`** prefix (same grammar as desk citation pointers). Examples:

| Input | Meaning |
|-------|---------|
| `areas/threads/x.md` | Vault from `vault=` or process default |
| `work:areas/threads/x.md` | Vault `work`, path `areas/threads/x.md` |

**Writability gate:** the prefix (and `vault=`) must name a vault in **this MCP/RPC/watch process registry** — the live bindings from `APO_COLLECTION_ROOT` / `APO_VAULT_PATHS` / `--vault` for that process — not every folder under `~/Notes` and not `desk.yaml`. Unknown prefix → `bad_vault` (no fall-through to the default vault). Prefix disagreeing with `vault=` → `bad_request`.

Workbench example: registry = work, contracts, optima, compliance → `atlas:areas/…` is denied; `work:areas/threads/…` is allowed.

Successful reads/writes/search hits also return **`qualified_path`** (`vault:rel`) for copy-paste, while keeping relative `path` / `source` + `vault`.

v1 `patch_note` batches (`items[]`): all prefixed paths must resolve to the **same** vault_id.

Each vault may ship contracts under `system/contracts/` (see [contracts/](./contracts/)).
