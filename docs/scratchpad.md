# Scratchpad (JSON/YAML payload workshop)

Ephemeral buffer for **JSON/YAML catalog payloads** before a vault write. One MCP/RPC tool:

```text
scratchpad(action=create|read|patch|commit|discard, …)
```

Spill lives under `~/.apo/scratchpads/<session_id>/` (override with `APO_SCRATCHPADS_ROOT`). Default TTL is 24h.

## Why

Agents burn tokens regenerating whole JSON/YAML MCP payloads. Scratchpad keeps a **format-guaranteed buffer**, surgical `patch` ops (`set_field` / `delete_field`), optional schema check at **commit**, and promote without re-emitting the body.

## Loop

1. `scratchpad(action=create, format=json|yaml, content=…)` — vault-free
2. `scratchpad(action=patch, session_id=…, ops=[{op:set_field, field, value}, …])`
3. Optional: `scratchpad(action=read, session_id=…)` — returns truncated `buffer` when large
4. `scratchpad(action=commit, session_id=…, vault=…, destination_path=…, schema_path=?, schema_type=?)`
5. Or `scratchpad(action=discard, session_id=…)`

**Formats:** `json` (default) and `yaml` only. Markdown / `.mmd` → use `write_note` / `patch_note` directly.

**Promote:** `commit` only (no `write_note(scratchpad=)` / `patch_note(scratchpad=)`).

**Schema:** pass `schema_path` and/or `schema_type` on **commit** — loaded from the destination vault (`system/schemas/`, okf `type_profiles`). No session bind step.

**Concurrency:** last writer wins (overwrite via `write_note`). No checkout / 3-way merge.

After `PROMOTED`, `patch` fails — `create` a new session to iterate.

## Token-efficient responses

`create` / `patch` / `commit` return **envelope-only** by default (`session_id`, `state`, `format`, `diagnostics`). Use `read` when you need bytes.

Prefer `ops=[{op:set_field, field, value}]` with **native JSON values** over regenerating `content=`.

## Example

```text
scratchpad(action=create, format=json, content={"status":"draft","todos":[…]})
scratchpad(action=patch, session_id=<id>, ops=[{op:set_field, field=todos[0].status, value=completed}])
scratchpad(action=commit, session_id=<id>, vault=work, destination_path=inbox/plan.json, schema_type=Plan)
```

## Related

- [agent-throughput.md](./agent-throughput.md) — when to use scratchpad vs direct write
- [patch-note-ops.md](./patch-note-ops.md) — full ops dialect lives on `patch_note`; scratchpad exposes `set_field` / `delete_field` only
- [local-rpc.md](./local-rpc.md) — `POST /v1/scratchpad`
