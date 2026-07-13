# Apo — vault onboard prompt

**Audience:** paste into Cursor or Claude Code after the engine is installed and MCP host key `apo` points at **this** vault (`APO_NOTES_ROOT`).

**Goal:** infer the vault’s existing structural and procedural rules, then propose persistent agent instructions so Apo becomes the markdown-as-source-of-truth memory backend — without forcing Jeremy’s Meta conventions onto someone else’s PKB.

Do **not** invent employer/gateway/multi-vault architecture. Stay on local engine + one vault root.

---

## Prompt (copy below)

````
You are onboarding Apo for this markdown vault.

Apo is a local MCP memory engine: files on disk are source of truth; sqlite-vec is a rebuildable index; tools search and surgically write notes (append_note, patch_note, write_note, find_notes, etc.).

### Hard rules for this session

1. **Discover before prescribe.** Do not write persistent rules, AGENT/AGENTS/CLAUDE files, or Cursor rules until I approve a draft.
2. **Preserve existing conventions.** Infer from this vault. Only propose Apo-shaped habits (search→anchor writes, check `ok`, folder=, deferred reindex) that fit what is already here.
3. **Do not import PARA/OKF/thread rotations or a canned profile** unless this vault already uses them, or I explicitly ask for a preset from `docs/profiles/` (e.g. PARA, llm-wiki).
4. **One vault.** Ignore multi-tenant / gateway product framing.
5. If Apo MCP is available, call `memory_status` first and confirm `APO_NOTES_ROOT` matches this workspace.
6. If I asked for a profile: read that profile’s **Behaviors** section and fold them into Draft A (especially consequential-turn writes, adapted to *that* layout).

### Phase 1 — Discover (read-only)

Inspect the vault root and nearby agent config. Parallelize reads where possible.

Collect evidence for:

**A. Layout**
- Top-level folders and what they appear to mean
- Presence of PARA (`inbox/`, `projects/`, `areas/`, `resources/`, `archives/`), Zettelkasten, second-brain, flat wiki, or mixed
- `index.md` / README / folder contracts if any

**B. Agent instruction surfaces (existing)**
- `AGENT.md`, `AGENTS.md`, `CLAUDE.md`, `README.md`
- `.cursor/rules/`, `.agents/`, `system/` guidance, skill files pointing at this vault
- Any mention of Basic Memory, memsearch, Obsidian-only workflows, or other memory backends

**C. Note physiology**
- Sample 8–15 notes across folders (prefer recent + representative)
- Frontmatter fields in actual use (title, tags, status, type, dates, links, …)
- Logging patterns (daily notes, changelogs, session logs, History sections)
- Link style: wikilinks `[[…]]` vs markdown links vs both

**D. Procedural rules already stated**
- Where new notes go; rename/archive rules; draft vs done; “don’t rewrite whole files”; inbox triage; etc.
- Quote short excerpts with paths — do not dump whole files into the proposal.

If Apo tools work: `list_directory` on `.`, `recent_activity`, and one `search_notes` smoke query. If MCP is missing, say so and continue with filesystem reads only.

### Phase 2 — Infer (structured brief)

Return a brief with these headings only:

1. **Vault shape** — one paragraph
2. **Conventions in force** — bullets (routing, frontmatter, links, logs)
3. **Conflicts / ambiguity** — bullets (overlapping instruction files, dead backends, empty contracts)
4. **Apo fit** — which Apo tools map cleanly; what should stay filesystem/Obsidian-only
5. **Recommended instruction surfaces** — which file(s) to create or patch for *this* agent host (Cursor vs Claude Code), minimizing duplication

### Phase 3 — Propose (draft only)

Produce **draft markdown** for me to approve, tailored to the inferred vault:

**Draft A — Persistent agent memory rule** (Cursor rule or Claude/AGENTS section), including:
- Apo as sole markdown memory backend for this vault (retire conflicting backends if any)
- Tool routing table adapted to *our* folders (not a generic PARA lecture)
- Write discipline: prefer append/patch; use heading/chunk_hash when available; `expected_mtime` when editing hot notes; `reindex_deferred` after batches
- Consequential-turn writes *only if* this vault already has a logging/decision habit — map onto existing paths (do not invent Meta’s session-log format unless we already use something like it)
- Explicit non-goals: code search, email, ticket systems stay outside Apo

**Draft B — Optional vault root pointer**
Short `AGENT.md` (or patch) only if missing or hollow: purpose, routing table from inference, pointer to agent-specific files.

**Draft C — Checklist** for me: MCP env paths, Ollama `bge-m3`, full app restart, smoke `search_notes` query using a term from this vault.

Label drafts clearly as PROPOSED. Do not write them yet.

### Phase 4 — Apply (only after I say yes)

1. Write approved drafts (surgical edits; no drive-by cleanup).
2. Re-check `memory_status` / a real `search_notes` query.
3. Summarize what changed and any follow-ups I must do manually (restart app, pull embeddings).

### Start now

Begin Phase 1. If the vault root is unclear, ask one clarifying question: absolute path of `APO_NOTES_ROOT`.
````

---

## Maintainer notes

- Keep this prompt **convention-agnostic**. Meta’s OKF/threads/session-log policy belongs in Jeremy’s vault rules, not here.
- Pair with [`quickstart.md`](./quickstart.md) for install + MCP registration.
- When sharing with teammates: repo access + these two docs is enough; do not send personal vault paths or internal deploy docs.
