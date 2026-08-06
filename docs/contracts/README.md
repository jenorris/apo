# Apo contracts

**Contracts** are policy the vault encodes so Apo adjusts its behavior. The engine stays convention-agnostic until a vault ships a contract it understands.

| Layer | Where | Role |
|-------|-------|------|
| **Runtime** | Apo engine | Interprets contracts; search + mutate |
| **Live contract** | Vault `system/contracts/` (preferred); legacy `system/config/*-contract.schema.yaml` | Adjusts Apo for *this* knowledge base |
| **Contract template** | This folder (`docs/contracts/`) | Copy-paste starters — not live config |

Do **not** confuse templates here with a setting in MCP config. Opt-in means: put the machine-readable (and/or agent-facing) contract **in the vault**, then point agents at it.

**Agent discovery:** MCP/RPC `vault(action=list|contracts|describe|merge|project)`. Prefer YAML under `system/contracts/`. Admin engine ops via `apo_admin(action=list|describe|invoke)` with `confirm=true` when destructive. `merge` / `contracts` / `describe` return summaries by default (`full=true` for YAML bodies). `merge` unions the registry with per-vault contracts and `~/.apo/desk.yaml`. `project` emits Cursor `apo-desk.mdc`, Claude `apo-desk` skill, and optionally Hermes (`host=hermes|all`) — see [../examples/desk.example.yaml](../examples/desk.example.yaml) and [../hermes.md](../hermes.md); re-run via `just desk-project` or the watcher after desk/contract changes.

## Shipped templates

| Template | Status | Best for |
|----------|--------|----------|
| [para.md](./para.md) | **Ship** | Life OS / work PKB — projects, areas, inbox |
| [llm-wiki.md](./llm-wiki.md) | **Ship** | Research corpora — compile raw sources into a maintained wiki |
| [okf-bundle.md](./okf-bundle.md) | **Ship** | OKF Knowledge Bundle — `okf_type` primary; vault YAML stamp/soft/hard |
| [yaml-notes.md](./yaml-notes.md) | **Ship** | Standalone `.yaml` / `.yml` catalog notes (filter + field patch) |
| [git.md](./git.md) | **Ship** | Vault backup / remote + `history(path=)` + optional `sync.enabled` commit/pull — [git-contract.schema.yaml](./git-contract.schema.yaml) |
| [search-contract.schema.yaml](./search-contract.schema.yaml) | **Ship** | Per-vault default exclude globs for unscoped search + history browse — [search-contract.schema.yaml](./search-contract.schema.yaml) |
| [usage-contract.schema.yaml](./usage-contract.schema.yaml) | **Ship** | Host-neutral vault usage IR for harness / `vault(project)` — **not** interpreted by the engine for search/write |
| [telemetry-contract.schema.yaml](./telemetry-contract.schema.yaml) | **Ship** | Vault-defined tool-use telemetry privacy + agent `session_stats` access — [telemetry.md](./telemetry.md) |

**Existing vault:** [../onboard-prompt.md](../onboard-prompt.md) — infer first; do not force a contract.

**Empty vault:** pick a template below, scaffold folders + stubs (and any `system/config/*-contract*.yaml`), then run the onboard prompt so Apo tool habits bind to *that* tree.

## Layout vs behaviors vs machine contracts

Templates may ship:

1. **Layout** — directories, naming, frontmatter floors
2. **Behaviors** — when/how the agent must write (prose for Cursor/Claude rules)
3. **Machine contract** — YAML Apo loads at write time (OKF stamp/validate today)

**Usage `contribution`:** optional authoring dialect (`plain-md` \| `gfm` \| `obsidian-ofm`) plus feature/surface overrides and an orthogonal `render` profile (`none` \| `htmlize`). Desk projection loads usage bodies only and emits a one-liner per vault into apo-desk; deep OFM/htmlize docs stay in `contribution.pointers`. Not a machine contract — engine does not validate body syntax.

**Usage `integrations`:** optional per-vault expected MCP host keys / CLI names (`mcp.required|expected|optional|never`, `cli` / `cli.expected`). Desk projection emits an **Expected integrations** section into apo-desk. Advisory for agents only — not a machine contract; Cursor still uses a global `mcp.json`.

Engine-universal habits (check `ok`, prefer `append_note`/`patch_note`, `folder=` on search) belong in every template’s Apo section. **Do not** put vault-specific private paths in these shared templates.

## Candidates (not shipped yet)

| Idea | Why interesting | Why wait |
|------|-----------------|----------|
| **Zettelkasten / evergreen** | Atomic notes + dense `[[wikilinks]]`; great search targets | Easy to fake badly; needs link hygiene behaviors |
| **Johnny.Decimal** | Strong unique IDs / sorting for humans | Weak agent defaults unless ID allocator is scripted |
| **Journal-first** | Daily notes as hub | Narrow; often a *layer under* PARA |
| **GTD + PARA** | Next-actions / waiting / someday | Task systems diverge wildly |
| **Repo-adjacent docs** | `docs/` + root `AGENTS.md` for a code project | Different “vault”; may be `contracts/repo-docs.md` later |
| **Flat wiki** | Single `wiki/` of evergreen pages (no PARA) | Overlaps llm-wiki without raw/compile discipline |
| **agentic-memory** | Episodic/working Facts (SPO) | Ship when Hermes/provider path is ready (may live on main separately) |

Promote a candidate when a real onboard asks for it — then write a thin **template**, don’t invent shelves.

## Mixing

PARA **life OS** + llm-wiki **topic silo** is valid (separate collection/root). PARA + **OKF Bundle** is the Meta vault shape — live machine contract under `system/config/`. If mixed: document two roots or folder prefixes and tell Apo which `APO_NOTES_ROOT` / `folder=` applies.
