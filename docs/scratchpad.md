# Scratchpad (ephemeral workshop buffers)

Staging lifecycle for JSON / YAML / Markdown **before** a vault write. One MCP/RPC tool:

```text
scratchpad(action=create|checkout|read|patch|validate|bind_schema|commit|discard|status, …)
```

Spill lives under `~/.apo/scratchpads/<session_id>/` (override with `APO_SCRATCHPADS_ROOT`). Default TTL is 24h.

## Why

Agents burn tokens regenerating MCP JSON payloads and pasting plans between turns. Scratchpad keeps a **format-guaranteed buffer**, surgical `patch` ops, vaulted schema diagnostics, and promote without re-emitting the body.

## Modes

| Mode | Vault required? | Works | Does not |
|------|-----------------|-------|----------|
| **Workshop (vault-free)** | No | `create`, `patch`, `read`, format validate | `bind_schema`, `commit`, `write_note(scratchpad=)` without a vault target |
| **Schema-bound** | Yes (`vault=`) | Above + `bind_schema` / schema validate | — |
| **Promote** | Yes | `commit` or `write_note` / `append_note` with `scratchpad=` | — |

`create` does **not** require `vault=`. Pass `vault=` only when binding schemas or committing.

## Token-efficient responses

Mutate / validate / status responses are **envelope-only by default** (`session_id`, `state`, `format`, schema pins, `diagnostics`, `hashes`). Ask for bytes with `include`:

- `fragment` — path / heading / fields
- `handoff` — compact projection (`x-apo-handoff` or heuristics)
- `toc` — markdown headings
- `buffer` / `raw` — full text (truncated >8KiB with a tip)

When a schema is bound, `patch` runs validate in-process unless `validate=false`.

Prefer `ops=[{op:set_field, field, value}]` with **native JSON values** over regenerating `content=`.

## Schemas

Primary home: **`system/schemas/**/*.schema.json`** in the **pinned** vault.

| Flag | Default | Meaning |
|------|---------|---------|
| `vault=` | required on bind | Load schema / okf-contract only from this vault |
| `schema_vault` + content hash | persisted | Origin pin; hash drift → WARNING |
| `allow_foreign_schema` | false | Allow `schema_path` outside `system/schemas/` (still inside vault) |
| `allow_cross_vault_schema` | false | Allow commit when `schema_vault` ≠ destination vault |

Secondary: `schema_type=` → that vault’s okf-contract `type_profiles` (e.g. Plan with todos — **example**, not a privileged engine path). Dual bind (`schema_path` + `schema_type`) validates **AND**. `$ref`: same-doc + vault-relative only; no remote `http(s)`.

## Scenario A — payload workshop

1. `scratchpad(action=create, format=json, content=…)`
2. `patch` with `set_field` / `delete_field` until clean
3. `bind_schema(schema_path=system/schemas/….schema.json, vault=…)` or `schema_type=…`
4. `read(include=["fragment"], json_path=$.…)` → pass fragment into foreign MCP
5. Optional: `write_note(path, scratchpad=session_id, vault=…)` or `commit`

## Scenario B — inter-agent handoff

1. Agent A: create / patch / validate; hand off **`session_id`** (spill survives process restart)
2. Agent B: `status` + `read(view=handoff)` → patch → optional `commit`

## Commit / merge

`checkout` snapshots base text + section hashes. `commit`:

1. Re-validates bound schemas
2. Reads current **vault working-tree** bytes at `destination_path` (not a jj bookmark / secondary worktree)
3. Section-tree + frontmatter 3-way merge; preamble is its own unit. Frontmatter field merges keep YAML comments via `yaml_rt` (one-sided FM edits return that side’s fence text verbatim; mixed key wins apply `set_field` / `delete_field` on a base `CommentedMap`).
4. Non-overlapping edits auto-merge; same heading both changed → `MERGE_CONFLICT`
5. CAS re-read; write via `write_note`; session → `PROMOTED` (mutations denied; `read` follows vault)

**JSON / YAML catalog paths:** when buffer `format` matches the destination suffix (`.json`, `.yaml`/`.yml`), promote/commit writes **raw catalog bytes** — no OKF frontmatter wrapper. Markdown destinations still go through OKF as usual.

**Workbench note:** Apo writes registered vault roots (e.g. `~/Notes/Work`, `~/Workbench/compliance`). Scratchpad does **not** replace jj worktrees for compliance SoT — it helps when multiple writers hit the same registered path.

## Promote without re-emit

```text
write_note(path, scratchpad=<session_id>, vault=…)     # omit content=/sections=/frontmatter=
append_note(path, scratchpad=<session_id>, vault=…)    # omit text=
patch_note(path, scratchpad=<session_id>, ops=[…], vault=…)   # markdown: ops then merge-commit
scratchpad(action=commit, session_id=…, destination_path=…, vault=…)
```

Pass **`path + scratchpad=` only** on `write_note` — empty `content=` is ignored; non-empty body args are rejected. Prefer the **`bind_schema` / `patch` response envelope** over a parallel `status` in the same turn (meta is written atomically; parallel `status` may read pre-bind state).

`patch_note(scratchpad=)` applies `ops` to the spill buffer then merge-commits to `path` (markdown-only). Bound schemas always re-validate before promote.

## Related

- [agent-throughput.md](./agent-throughput.md) — when to use scratchpad vs direct write
- [patch-note-ops.md](./patch-note-ops.md) — shared `ops[]` dialect
- [local-rpc.md](./local-rpc.md) — `POST /v1/scratchpad`
