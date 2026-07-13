# Profile: LLM wiki (Karpathy-style)

**Status:** optional preset · **Layout + behaviors**

Compile raw sources into a persistent, LLM-maintained markdown wiki instead of re-RAGing snippets every query. Pattern from [Karpathy’s llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f); concrete agent skill often follows [llm-wiki skill](https://github.com/lewislulu/llm-wiki-skill) layouts (`raw/` + `wiki/` + schema file).

**Use when:** topic/research knowledge bases, literature synthesis, “compile once, query the wiki.”
**Don’t use when:** life OS, daily ops, project tracking — use [para.md](./para.md) (or keep llm-wiki as a **silo** beside PARA).

## Layout

```text
<wiki-root>/
├── CLAUDE.md              # or AGENTS.md — schema: scope, conventions, gaps
├── raw/                   # immutable sources (human-owned; agent reads only)
│   ├── articles/
│   ├── papers/
│   ├── notes/
│   └── refs/              # pointers to large binaries outside the tree
├── wiki/                  # LLM-owned compiled knowledge
│   ├── index.md           # master catalog
│   ├── concepts/
│   ├── entities/
│   └── summaries/
├── log/                   # per-day or append-only operation log
├── audit/                 # human feedback inbox → resolved/
└── outputs/               # optional query artifacts / exports
    └── queries/
```

Exact subfolders may vary; the **contracts** matter more than names:

| Layer | Owner | Rule |
|-------|-------|------|
| `raw/` | Human | Never rewritten by the agent |
| `wiki/` | Agent | Create/update/cross-link freely under schema |
| Schema (`CLAUDE.md` / `AGENTS.md`) | Co-evolved | Read at session start |
| `audit/` | Human → agent | Structured corrections, then resolve |

## Behaviors (ship these)

### 1. Five operations

Prefer explicit modes (slash commands or stated intent):

| Op | Does |
|----|------|
| **ingest** | Read new `raw/` item → update summaries/entities/concepts → index + log |
| **query** | Answer from `wiki/` first; cite pages; optionally file durable answers back |
| **lint** | Orphans, dead links, contradictions, coverage gaps |
| **compile** / restructure | Split oversized pages; normalize links |
| **audit** | Apply items from `audit/`; archive to `audit/resolved/` |

### 2. Divide and conquer

Concept pages stay readable (~400–1200 words). Split into `wiki/concepts/<topic>/` + `index.md` when growing past that.

### 3. Schema is law

Every session: read schema + `wiki/index.md` before mutating. Propose schema changes; don’t silently rewrite the contract.

### 4. Consequential-turn writes (wiki-shaped)

Same *trigger* as PARA (decisions, corrections, durable facts) — different *targets*:

| Event | Write |
|-------|-------|
| Ingest finished | `wiki/` pages + `log/` + `wiki/index.md` |
| Query produced durable knowledge | promote into `wiki/` (not only chat) |
| Human filed audit feedback | apply → move to `resolved/` + log |
| Lint found issues | fix or open an audit item — don’t only report orally |

Skip writes for explorative Q&A that should stay ephemeral (unless the human asks to keep it).

### 5. Raw immutability

Never `write_note` / `patch_note` under `raw/` except creating a human-requested stub pointer. Corrections to *understanding* go in `wiki/` or `audit/`.

## Apo wiring

- Point `APO_NOTES_ROOT` at `<wiki-root>` (or a parent vault that contains this tree).
- Scope tools: `folder=wiki/`, `folder=raw/` as appropriate.
- Prefer `search_notes` over re-reading entire `raw/` for queries once compiled.
- `ingest_uri` (if enabled) may land under `raw/` or `resources/wiki/` depending on host config — keep **immutable** semantics.
- Surgical updates to wiki pages: `patch_note` / `append_note`; log lines: `append_note` on the day log.

Install engine via [../quickstart.md](../quickstart.md). Use [../onboard-prompt.md](../onboard-prompt.md) to emit host rules that encode these ops — or point Cursor/Claude at this file + your schema.

## Non-goals

- Replacing PARA for personal ops
- Publishing / ACL / multi-tenant gateway
- Requiring a specific Obsidian plugin set
