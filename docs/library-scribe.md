# Library scribe & self-correcting vaults

**Status:** Shipped (2026-08-15) — `flaws[]` on write + `vault(action=lint)` + trailing-WS auto-fix.
**Metaphor:** Apo is the **library scribe** — catalogue, format, keep shelves honest; few decisions of its own.
**Goal:** Raise **agentic contribution quality** and **vault longevity**. Agents absorb operational KM; humans keep ownership, ontology, and permanence.

Canonical product decision (personal PKB): Atlas `projects/apo-pkb/library-scribe-self-correcting.md`.

## Embeddings = the card catalog

**Ollama + `bge-m3` (with BM25) are how the scribe updates the library catalog** so agents can find what they need. After a write, the watcher re-chunks and re-embeds — catalog cards catch up to the shelves. The sqlite index is a **rebuildable card catalog**, not a second database and not the product identity.

That pipeline is a **source of pride** (local, faithful, fast enough for daily use). It is **not** the brand thesis: files remain source of truth; vault data + contracts + surgical write stay above hybrid search in the stack rank. Delete `index.db` anytime; `just reindex` rebuilds the catalog from the shelves.

| Prefer | When |
|--------|------|
| `filter_notes` | Typed / call-number lookup (`okf_type`, `status`, …) |
| `search_notes` | Meaning recall via the catalog |

## Why

Today’s MCP returns:

| Field | Meaning |
|-------|---------|
| `tip` | Agent habit coaching (`folder=`, `expected_mtime`, …) |
| `warning` | Ops / degraded mode (watcher, BM25-only, …) |
| OKF soft strings | Corpus quality mixed into warnings |

That conflates **how the agent uses tools** with **whether the vault is healthy**. Self-correcting vaults need a third channel: deterministic **corpus findings** the engine can prove, so the LLM corrects only where judgment is required.

## Channel split (normative)

| Field | Audience | Corpus quality? |
|-------|----------|-----------------|
| `tip` | Agent habits | No |
| `warning` | Ops / degraded | No |
| `flaws` | Note / vault defects | **Yes** |

OKF `enforcement: hard` stays a hard reject (`ok: false`). Soft OKF misses become structured `flaws[]` (not prose-only warnings) once this ships.

## `flaws[]` shape

```json
{
  "code": "okf.missing_field",
  "severity": "warn",
  "path": "areas/threads/example.md",
  "vault": "work",
  "evidence": {
    "field": "description",
    "expected": "non-empty string"
  },
  "remediation": "llm",
  "suggested_op": {
    "tool": "patch_note",
    "ops": [{"op": "set_field", "field": "description", "value": null}]
  },
  "message": "missing description (OKF soft floor)"
}
```

| Key | Rule |
|-----|------|
| `code` | Stable dotted id — agents branch on this, not `message` |
| `severity` | `info` \| `warn` \| `error` (non-fatal unless hard OKF) |
| `remediation` | `auto` (engine) \| `llm` (model judgment) \| `human` (ownership/taxonomy) |
| `suggested_op` | Optional; `value: null` means LLM must supply content |
| `evidence` | Anchors (`field`, `heading`, `chunk_hash`, …) |

## Loop

```text
detect (contract + usage + note)
  → auto-fix if remediation=auto and enabled
  → else emit flaws[]
agent (budget N per turn)
  → patch llm flaws once via suggested_op / evidence
  → surface human flaws; do not invent taxonomy
```

**Inline:** successful writes attach `flaws` for that path (OKF soft + format; archival when contracted).
**Sweep:** `vault(action=lint)` — folder/vault backlog, paginated (archival + note_lint detectors).
**Opt-in read:** `read_note(path, lint=true)`.
**Batch:** existing `vault-tools` OKF lint/fix share detector themes; MCP uses dotted `code`s.

## Detector catalog (v1 candidates)

| Code | Default remediation |
|------|---------------------|
| `okf.missing_field` | `llm` or `auto` if stampable |
| `okf.type_mismatch` | `llm` / `human` |
| `usage.frontmatter_floor` | `llm` |
| `usage.dialect_feature` | `llm` |
| `link.broken` | `llm` |
| `link.ambiguous` | `llm` |
| `layout.unexpected_folder` | `llm` |
| `status.body_conflict` | `llm` |
| `format.trailing_ws` | `auto` |

`habit.*` stays in `tip` — out of scope for `flaws`.

## Phases

| Phase | Deliverable |
|-------|-------------|
| 0 | This doc + PKB decision |
| **B** | Archival suggest: `flaws[]` + `vault(action=lint)` — [contracts/archival.md](contracts/archival.md) |
| **1** | Soft OKF → structured `flaws` (dual-emit `warnings`) |
| **2** | General lint detectors + merged `vault(action=lint)` |
| **3** | Mechanical `format.trailing_ws` auto-fix + `status: fixed` |
| **4** | Links / dialect / layout / usage floor detectors |
| **5** | Habit KPIs (`flaws_emitted`, `flaws_auto_fixed` on `vault(stats)`) |

## Non-goals

- “Is this good writing?” semantics
- Auto-deleting notes
- Unbounded lint→fix loops
- Replacing human review for governed corpora (e.g. GRC git SoT)

## Open questions

1. `read_note` lint: default off vs opt-in?
2. Server-side auto-fix vs echo-only for audit?
3. Engine interprets usage-contract floors vs separate lint-contract?
4. Read-only lint on compliance primary (fixes only via jj workspace)?
5. When to enable archival `mode: auto` (watcher place)?

## See also

- [agent-throughput.md](agent-throughput.md) — habit tips + `flaws[]` habits
- [contracts/](contracts/) — OKF / usage templates
- [contracts/archival.md](contracts/archival.md) — suggest-mode archival (shipped)
- [vault-tools/README.md](../vault-tools/README.md) — batch OKF lint/fix
- [search-quality.md](search-quality.md) — retrieval eval (orthogonal)
