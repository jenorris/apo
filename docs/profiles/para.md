# Profile: PARA

**Status:** optional preset · **Layout + behaviors**

Tiago Forte–style PARA for a markdown PKB with Apo as the agent memory backend. Use when the vault is empty or the user explicitly wants this shape. Prefer [../onboard-prompt.md](../onboard-prompt.md) when a vault already has its own rules.

## Layout

```text
<vault>/
├── AGENT.md                 # routing + pointer to agent hosts (draft below)
├── inbox/                   # capture; triage outward
│   ├── daily/               # optional day files
│   ├── zettels/             # optional atomic captures
│   └── tasks/               # optional
├── projects/                # finite efforts with an end state
├── areas/                   # ongoing responsibilities
├── resources/               # reference by topic
│   └── wiki/                # optional externally ingested pages
├── archives/                # inactive / completed
└── system/                  # templates, agent config (optional)
```

Create empty dirs with `.gitkeep` or first notes as you like. Do **not** require OKF, citations, or thread systems unless you add another profile later.

### Frontmatter floor (suggested)

```yaml
---
title: Human title
tags: []
status: active   # or draft / done / archived — pick a small enum and stick to it
---
```

Agents may add fields the human already uses; don’t invent a large schema on day one.

### `AGENT.md` stub

```markdown
# Agent guidance — PARA vault

Markdown files here are source of truth. Use Apo MCP for search and surgical writes.

| Folder | Put here |
|--------|----------|
| `inbox/` | New / untriaged captures |
| `projects/` | Work with a done definition |
| `areas/` | Ongoing (no end date) |
| `resources/` | Reference material |
| `archives/` | Finished or inactive |
| `system/` | Templates and agent config |

Search before creating notes. Prefer `append_note` / `patch_note` over full-file rewrites.
Profile behaviors: see apo `docs/profiles/para.md` (or paste Behaviors section into your Cursor/Claude rules).
```

## Behaviors (ship these)

### 1. Consequential-turn writes (recommended default)

On every turn where something **consequential** happened, update the vault **before** the final user-facing reply. Do not batch across turns or wait for “session end.”

**Consequential** — write required:

- Decision, preference, or commitment
- Status change (done, blocked, deferred, in progress)
- New or completed action item
- Durable fact (people, dates, links, blockers, outcomes)
- Correction of prior understanding

**Not consequential** — skip write:

- Read-only recall
- Clarifying questions
- Trivial acknowledgments
- Drafts awaiting explicit approval

**Where to write (PARA map):**

| Scope | Target |
|-------|--------|
| Active project | `projects/<slug>/` (project home note if you use one) |
| Ongoing responsibility | `areas/<domain>/` |
| Unclear / new idea | `inbox/` (zettels or bare capture), then triage |
| External reference | `resources/` (or `resources/wiki/` if ingesting) |

Optional but high-leverage: a daily note under `inbox/daily/YYYY-MM-DD.md` with a short **Session log** bullet (`YYYY-MM-DD HH:MM` + one line). Only enable if the human wants an audit trail.

### 2. Search before create

`search_notes` (and `folder=` when the PARA bucket is known) before `write_note`. Prefer appending to an existing note over near-duplicates.

### 3. Surgical writes

- New note → `write_note`
- Log / History / additive → `append_note` (`heading`, `position`)
- Frontmatter or targeted replace → `patch_note`
- Rename / archive → `move_note` (never read→write→delete)
- After a batch of index-deferred writes → `reindex_deferred`

Always check tool `ok`.

### 4. Inbox hygiene

Inbox is temporary. Flag or triage items older than ~7 days. Don’t treat `inbox/` as long-term storage.

### 5. Archives are moves

Completed projects and dead areas → `archives/` via `move_note`, preserving history.

## Apo wiring

| Need | Tool |
|------|------|
| Semantic recall | `search_notes` |
| Status / field sweeps | `filter_notes` |
| Known path | `read_note` |
| Health | `memory_status` first on failures |

Set `APO_NOTES_ROOT` to this vault root; index; register MCP per [../quickstart.md](../quickstart.md). Then paste [../onboard-prompt.md](../onboard-prompt.md) so host rules (Cursor / Claude) match the folders you actually created.

## Non-goals for this profile

- Multi-vault / ACL / gateway
- Academic citation processors (unless you adopt footnotes yourself)
- Compiling research corpora (use [llm-wiki.md](./llm-wiki.md))
