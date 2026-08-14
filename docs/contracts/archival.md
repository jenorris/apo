# Archival contract (suggest)

**Status:** Shipped — **suggest** mode (engine loads contract; emits `flaws[]`).
**Template:** [archival-contract.schema.yaml](./archival-contract.schema.yaml)
**Related:** [search-contract.schema.yaml](./search-contract.schema.yaml) · [para.md](./para.md) · [../library-scribe.md](../library-scribe.md)

## Problem

Hot hybrid search degrades when finished threads and cold projects stay on the open shelves. PARA already has `archives/`; search-contract already drops `archives/*` from **unscoped** search. The archival contract encodes **when** a note is eligible so Apo can suggest archive moves without inventing taxonomy.

## Division of labor

| Piece | Role |
|-------|------|
| **archival-contract** | Eligibility + destination + mode (`suggest` shipped; `auto` deferred) |
| **search-contract** | Keeps `archives/*` out of hot (unscoped) search |
| **`flaws[]`** | `archive.eligible` / `archive.blocked_*` — agent applies |
| **Agent / human** | `set_field` on **src** → then `place` src→dst |
| **Watcher auto-place** | Future (`mode: auto`); currently ignored (treated as off) |

Embeddings stay the **card catalog**: after a place, the watcher reindexes — hot catalog forgets the work; cold catalog under `folder=archives` still finds it.

## Lifecycle (suggest)

```text
note goes idle (last_activity / status)
  → archival eligibility (contract)
  → mode=suggest: flaw archive.eligible + suggested_op place
  → agent: set_field on src → place → archives/…
  → search-contract excludes archives/* from unscoped search
  → cold recall: search/filter with folder=archives
```

Surfaces:

- **`vault(action=lint)`** — folder-scoped sweep (`folder=`, `limit=`, `offset=`)
- **Post-write** — on successful write/append/patch of a path: emit `archive.eligible` or `archive.blocked_todos` only (`blocked_status` is lint-only)

## Why not “cold flag in place”?

A stay-put `archived: true` index flag is possible later, but **move-to-`archives/`** matches PARA, git history, Obsidian folders, and today’s search-contract.

## Minimal GradGuard Work example

```yaml
archival_contract_version: "0.1"
mode: suggest
destination:
  root: archives
  strategy: mirror
eligibility:
  include_folders: [areas/threads, projects]
  exempt_folders: [system, archives]
  exempt_globs: ["**/index.md"]
  status_in: [done, resolved, closed, archived]
  idle:
    field: last_activity
    older_than_days: 90
actions:
  place: true
  set_fields:
    status: archived
    archived_at: "$now"
hot_search:
  relies_on: search-contract.default_exclude
  expected_exclude_globs: [archives/*]
safety:
  deny_if_open_todos: true
```

## Scribe codes

| Code | When | Write path? |
|------|------|-------------|
| `archive.eligible` | Idle + status in list + destination ok | Yes |
| `archive.blocked_todos` | Would be eligible but open todos | Yes |
| `archive.blocked_status` | Idle but status not in `status_in` | Lint only |

## Agent remediation

1. `patch_note` `set_field` on **src** (`status`, `archived_at` ISO now) from `evidence.set_fields`
2. `patch_note` `place` via `suggested_op` (src → mirror under `archives/`)

`suggested_op` is fully concrete for `archive.eligible` — apply without inventing taxonomy.

## Modes

| YAML `mode` | Runtime |
|-------------|---------|
| `suggest` | Emit findings |
| `off` / missing | Silent |
| `auto` | Treated as **off** + tip `archival mode auto not implemented; treated as off` |

v1 destination **`mirror` only**; `flat` / unknown → no eligible flaw (lint may warn).

## Future

| Phase | Work |
|-------|------|
| C | Watcher pass honors `mode: auto` with budgets + mtime safety |
| D | Desk projection one-liner |

## Non-goals

- Deleting notes
- Archiving evergreen `resources/` by mtime alone
- Changing unscoped search without search-contract
- LLM / watcher judgment in the detect path
