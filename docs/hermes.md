# Hermes / Lyra + Apo

Apo is **durable PARA / PKB memory** (files on disk + hybrid index). Hermes /
Lyra already have **Mnemosyne** for episodic / working memory. Keep both:

| Layer | Role |
|-------|------|
| **Mnemosyne** | Hermes `memory.provider` — turn sync, sleep, prefetch |
| **Apo** | MCP (stdio or HTTP) or `apo-local` — search, filter, surgical writes over vault files |

Do **not** register Apo as Hermes’s sole MemoryProvider — that displaces
Mnemosyne and drops episodic lifecycle hooks Apo does not implement.

## Process isolation

When the Hermes process should not own the stdio MCP subprocess (Grid /
Desma), prefer **`apo-mcp`'s HTTP transport** (FastMCP, default `:8878`) or
**`apo-local`** for in-process/no-daemon calls. Cursor and Claude Code keep
stdio MCP.

The legacy hand-rolled RPC server (`apo-engine serve` /
[`local-rpc.md`](./local-rpc.md)) is **deprecated** — do not point new Hermes
integrations at it. It is kept only for a known-but-unverified external
consumer (`apo-enterprise`); its one confirmed real consumer has already
migrated to `apo-local`.

## Desk projection

```bash
just desk-project
# or: vault(request={action: "project"})
```

Returns shared `body` + `guidance`. Same merge IR as any host (`~/.apo/desk.yaml` +
per-vault `system/contracts/`). Deterministic — no LLM. Agent chooses placement.

Per-vault usage IR (optional): copy
[`contracts/usage-contract.schema.yaml`](./contracts/usage-contract.schema.yaml)
into each vault’s `system/contracts/`.

## Night Shift / cron

Each Hermes cron run is a **fresh agent** — prompts must be self-contained.
Use `--skill` / projected `apo-desk` plus Apo tools (`filter_notes`,
`history`, `append_note` / `patch_note` with `expected_mtime`). Reflect jobs
should mark `reflected: true` on closed dailies (see Meta memory-lifecycle).
