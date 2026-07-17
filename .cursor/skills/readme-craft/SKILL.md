---
name: readme-craft
description: >-
  Write or rewrite excellent project README.md files without AI-slop voice.
  Evidence-first workflow with a 3-tier GitHub-native layout: hero (icon,
  promise, links, ≤4 factual badges), scan zone (why table, features table,
  Mermaid architecture, quick start, usage), supporting (config in details,
  docs map, boundaries). Use when creating or polishing a README, share
  package, or public-facing project docs; when the user asks for non-sloppy
  docs, README craft, Mermaid diagrams, or teammate/open-source onboarding prose.
---

# README craft

Write READMEs that a skilled engineer would trust — specific, scannable, honest.
Prose quality is editorial judgment; use the linter for objective breakage only.

## When to use

- New or rewritten root `README.md`
- Share/onboarding package polish (README + quickstart)
- User asks for non-AI-sloppy, human, or “excellent” project docs
- Second-pass upgrades (hero, Mermaid, features/why tables)

Do **not** use this for vault PARA directory contracts (those have a different template).

## Workflow

### 1. Inspect the repository

Before drafting, gather evidence from the tree:

- Root README (if any), `docs/`, install manifests (`justfile`, `package.json`, `pyproject.toml`, etc.)
- Entry points, CLIs, env examples, architecture already in-repo
- What actually works today vs aspirational roadmap

Do not invent features from marketing desire. If you cannot point to a file, command, or test, do not claim it.

### 2. Study 2–3 strong READMEs (when raising the bar)

For a first-class share README, skim comparable or high-signal projects (same domain or known excellent docs). Crib **layout moves**, not voice or fake social proof:

- Centered hero + quick links
- Features / comparison as **tables**
- Rendered **Mermaid** (not ASCII) for architecture and critical sequences
- Collapsible ToC and long config behind `<details>`
- Shortest install in the first screenful of useful content

Reject: emoji headers, badge walls, star-history theater, “Welcome to…”, empty intensifiers.

### 3. Define audience and promise

Write two lines privately (they become the opening, not a meta preamble):

1. **Who** — one primary reader (e.g. teammate cloning a private repo; stranger on GitHub)
2. **Promise** — one concrete sentence: what they can do after following the README

If teammate-first but public-ready later: optimize for teammates now; keep absolute-path placeholders, no private nicknames, no desk-only latency claims.

### 4. Inventory capabilities

List only **demonstrable** capabilities (command, tool name, or doc link). Rank by reader need. Drop or demote internals that belong in deeper docs.

### 5. Draft in 3 tiers

| Tier | Purpose | Typical content |
|------|---------|-----------------|
| **1 — Above the fold** | 3-second pitch | Icon, name, one-liner, ≤4 factual badges, links to Quickstart / docs |
| **2 — Scan** | Prove value in 2–3 screens | Why/comparison table, features table, Mermaid architecture (+ sequence if writes matter), quick start, one usage loop |
| **3 — Supporting** | Serve committed readers | Config in `<details>`, docs map, boundaries / maturity |

Default H2 order for a tool/engine README:

1. Why / comparison (when the model is non-obvious)
2. Features (table)
3. Architecture (Mermaid flowchart; add sequence for write→index or request path)
4. Quick start
5. How to use (agent or CLI loop)
6. API / tools summary (table; depth in docs)
7. Configuration (`<details>` if long)
8. Docs map
9. Boundaries

Optional: collapsible **Table of contents** when there are 5+ sections.

Move troubleshooting and typed-API detail into linked docs unless required in the first ten minutes.

### 6. Strip unsupported claims

Delete or rewrite any sentence that:

- Uses empty intensifiers without proof (`powerful`, `robust`, `seamless`, `delightful`, `world-class`)
- Names a private vault, employer path, or machine-specific benchmark as if universal
- Miscounts tools, versions, or commands vs the repo
- Repeats the same promise in three different phrasings
- Claims LICENSE / CONTRIBUTING / public polish that does not exist yet

### 7. Human-voice edit

Read at GitHub render width. Prefer short paragraphs, one job per section, imperative install steps, tables for scannable facts, Mermaid for topology.

Ban: emoji decoration, fake testimonials, badge walls, “Welcome to…”, “In this README we will…”, Smart Brevity axiom headers with emoji, ASCII diagrams when Mermaid would render.

### 8. Lint and verify

```bash
just readme-check
# or:
python3 .cursor/skills/readme-craft/scripts/lint_readme.py README.md docs/quickstart.md
```

Fix **errors** (broken local links/images, empty sections, placeholders). Treat **warnings** as editorial prompts.

Verify install commands against the real task runner. Run safe commands that do not mutate the user’s MCP config. Confirm Mermaid node labels stay short after a mental “GitHub render” check.

## Imagery and diagrams

**Icon**

- One modest, centered application icon in the hero (≈128 px display width; asset legible at ~64 px)
- Store under `docs/assets/` with a regeneration brief
- Use true alpha transparency outside the mark; verify the PNG does not contain a baked checkerboard
- No text in the glyph; no trademarked OS chrome

**Mermaid**

- Prefer `flowchart` / `sequenceDiagram` over ASCII; do not ship both for the same idea
- Short node labels; no `%%{init:...}%%` theme blocks (truncation bugs on GitHub)
- Use a sequence diagram when the write→index or request path is the product insight

See [reference.md](reference.md) for patterns, anti-patterns, and before/after examples.

## Acceptance checklist

- [ ] Hero states product + concrete promise; optional icon and ≤4 factual badges
- [ ] Why/features are tables when comparing or listing capabilities
- [ ] Architecture uses Mermaid (not ASCII) when a diagram helps
- [ ] Every major claim maps to a file, command, or linked doc
- [ ] Install path works for a cold clone (absolute-path placeholders only)
- [ ] Long config is collapsed or linked; root README is not a subsystem dump
- [ ] No private desk/employer leaks on the share path
- [ ] Linter errors clean; warnings reviewed
