# Hermes / Lyra + Apo

Apo is **durable PARA / PKB memory** (files on disk + hybrid index). Hermes /
Lyra already have **Mnemosyne** for episodic / working memory. Keep both:

| Layer | Role |
|-------|------|
| **Mnemosyne** | Hermes `memory.provider` — turn sync, sleep, prefetch |
| **Apo** | MCP or local RPC — search, filter, surgical writes over vault files |

Do **not** register Apo as Hermes’s sole MemoryProvider — that displaces
Mnemosyne and drops episodic lifecycle hooks Apo does not implement.

## Process isolation

Prefer **RPC** (`apo-engine serve` / [`local-rpc.md`](./local-rpc.md)) when the
Hermes process should not own the stdio MCP subprocess (Grid / Desma). Cursor
and Claude Code keep stdio MCP.

## Desk projection

```bash
just desk-project --host hermes
# or: apo-engine desk-project --host all
# or: vault(action=project, host=hermes, write=true)
```

Writes `~/.apo/projected/hermes/apo-desk/SKILL.md` (override with
`APO_PROJECT_HERMES`). Same merge IR as Cursor/Claude (`~/.apo/desk.yaml` +
per-vault `system/contracts/`). Deterministic — no LLM.

Per-vault usage IR (optional): copy
[`contracts/usage-contract.schema.yaml`](./contracts/usage-contract.schema.yaml)
into each vault’s `system/contracts/`.

## Night Shift / cron

Each Hermes cron run is a **fresh agent** — prompts must be self-contained.
Use `--skill` / projected `apo-desk` plus Apo tools (`filter_notes`,
`history`, `append_note` / `patch_note` with `expected_mtime`). Reflect jobs
should mark `reflected: true` on closed dailies (see Meta memory-lifecycle).
