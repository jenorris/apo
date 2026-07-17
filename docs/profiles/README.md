# Apo profiles

**Optional, opinionated presets** for vault layout + agent behaviors. The engine stays convention-agnostic; profiles are copy-paste policy.

| Profile | Status | Best for |
|---------|--------|----------|
| [para.md](./para.md) | **Ship** | Life OS / work PKB — projects, areas, inbox |
| [llm-wiki.md](./llm-wiki.md) | **Ship** | Research corpora — compile raw sources into a maintained wiki |

**Default path for existing vaults:** [../onboard-prompt.md](../onboard-prompt.md) (infer first; do not force a profile).

**Empty vault / “just give me a structure”:** pick a profile below, scaffold folders + stubs, then run the onboard prompt so Apo tool habits bind to *that* tree.

## Behaviors vs layout

Profiles ship **both**:

1. **Layout** — directories, naming, frontmatter floors
2. **Behaviors** — when and how the agent must write so the vault stays true

Engine-universal habits (check `ok`, prefer `append_note`/`patch_note`, `folder=` on search, `reindex_deferred` after batches) live in every profile’s Apo section. **Do not** put vault-specific private paths (personal OKF taxonomies, internal project threads, home-lab hosts) in these docs.

## Candidates (not shipped yet)

| Idea | Why interesting | Why wait |
|------|-----------------|----------|
| **Zettelkasten / evergreen** | Atomic notes + dense `[[wikilinks]]`; great search targets | Easy to fake badly; needs link hygiene behaviors |
| **Johnny.Decimal** | Strong unique IDs / sorting for humans | Weak agent defaults unless ID allocator is scripted |
| **Journal-first** | Daily notes as hub (Calendar / intermittent journaling) | Narrow; often a *layer under* PARA, not a whole vault |
| **GTD + PARA** | Next-actions / waiting / someday | Task systems diverge wildly; easy to over-prescribe |
| **Repo-adjacent docs** | `docs/` + root `AGENTS.md` for a code project | Different “vault”; may be a one-pager under `profiles/repo-docs.md` later |
| **Flat wiki** | Single `wiki/` of evergreen pages (no PARA) | Overlaps llm-wiki without raw/compile discipline |

Promote a candidate when a real onboard asks for it — then write a thin profile, don’t invent shelves.

## Mixing

PARA **life OS** + llm-wiki **topic silo** is a valid combo (keep compiled wikis under a separate collection/root). If mixed: document two roots or two folder prefixes and tell Apo which `APO_NOTES_ROOT` / `folder=` applies.
