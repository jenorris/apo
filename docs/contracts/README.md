# Apo contracts

**Contracts** are policy the vault encodes so Apo adjusts its behavior. The engine stays convention-agnostic until a vault ships a contract it understands.

| Layer | Where | Role |
|-------|-------|------|
| **Runtime** | Apo engine | Interprets contracts; search + mutate |
| **Live contract** | Inside the vault (e.g. `system/config/okf-contract.schema.yaml`) | Adjusts Apo for *this* knowledge base |
| **Contract template** | This folder (`docs/contracts/`) | Copy-paste starters — not live config |

Do **not** confuse templates here with a setting in MCP config. Opt-in means: put the machine-readable (and/or agent-facing) contract **in the vault**, then point agents at it.

## Shipped templates

| Template | Status | Best for |
|----------|--------|----------|
| [para.md](./para.md) | **Ship** | Life OS / work PKB — projects, areas, inbox |
| [llm-wiki.md](./llm-wiki.md) | **Ship** | Research corpora — compile raw sources into a maintained wiki |
| [okf-bundle.md](./okf-bundle.md) | **Ship** | OKF Knowledge Bundle — `okf_type` primary; vault YAML stamp/soft/hard |

**Existing vault:** [../onboard-prompt.md](../onboard-prompt.md) — infer first; do not force a contract.

**Empty vault:** pick a template below, scaffold folders + stubs (and any `system/config/*-contract*.yaml`), then run the onboard prompt so Apo tool habits bind to *that* tree.

## Layout vs behaviors vs machine contracts

Templates may ship:

1. **Layout** — directories, naming, frontmatter floors  
2. **Behaviors** — when/how the agent must write (prose for Cursor/Claude rules)  
3. **Machine contract** — YAML Apo loads at write time (OKF stamp/validate today)

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
