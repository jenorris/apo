# Mermaid notes in Apo — agent habits

Parallel to [yaml-notes.md](yaml-notes.md). Diagram sources: standalone `.mmd` files and fenced ` ```mermaid ` blocks in markdown.

## Chunk kinds

| `chunk_kind` | Use |
|---|---|
| `mermaid_file` | Whole-diagram / catalog summary recall |
| `mermaid_header` | Subgraph list / diagram type |
| `mermaid_node` | Entity lookup (primary) |
| `mermaid_edge` | Flow / integration questions |

Flattened text is stored in `chunks.text` (not raw Mermaid syntax) — same pattern as `table_row`.

## Search habits

1. `filter_notes({"okf_type": "Diagram"}, folder="diagrams/mermaid-catalog")` for catalog browse
2. `search_notes(query, folder="diagrams/mermaid-catalog")` for semantic node hits
3. On `mermaid_node` hit: `read_note(chunk_hash=…)` or `read_note(chunk_hash=…, format=node)`

Fenced diagrams in `.md` use the same chunk kinds; no catalog filter unless path matches contract.

## Catalog join

Paths matching `diagrams/mermaid-catalog/<slug>/diagram.mmd` merge [catalog.yaml](catalog.yaml) at index time:

- `diagram_id` := catalog `slug`
- `title`, `type`, `confluence_page_id`, `lucid_id` from catalog entry

Optional YAML frontmatter at top of `.mmd` overrides catalog fields.

## Contract

Ship [mermaid-contract.schema.yaml](mermaid-contract.schema.yaml) under `system/contracts/`. Tunables:

- `chunk_strategy`: `file_only` | `nodes` | `nodes_and_edges`
- `flatten_template`
- `validation`: `soft` | `hard` | `off`

Cross-reference: [table-contract.schema.yaml](table-contract.schema.yaml) — shared `chunk_kind`-aware search habits.

## Scratchpad

`scratchpad(format=mmd)` workshops `.mmd` buffers; promote via `write_note(path, scratchpad=session_id)` (raw write, no frontmatter wrapper).

## Reindex

After engine upgrade: `just index --vault compliance` and `just index --vault work` for fenced-md recall.

## Eval

Copy examples to `~/.apo/`:

- `search-eval-mermaid-compliance.yaml`
- `search-eval-mermaid-work-fenced.yaml`

Run: `just search-eval --file ~/.apo/search-eval-mermaid-compliance.yaml`

## Search tuning (compliance catalog)

Post-reindex benchmark (2026-08-19, k=3): compliance **54.5%** hit@3 (gate ≥80%); work fenced-md **75%** (gate ≥70%, passes).

### Observed failure modes

| Mode | Example | Cause |
|---|---|---|
| Page beats `.mmd` | “renters pro API data flow” | `pages/*.md` Confluence bodies share slug/title tokens; table rows and prose rank above `diagram.mmd` node chunks |
| Entity substring | `expect_entity: Tuition` on standard-data-flow | Flattened node text uses `Tuition Program` / `TAPI`, not bare `Tuition` |
| Chunk-kind strictness | `expect_chunk_kind: mermaid_file` | Top hit is often `mermaid_header` or `mermaid_node`; whole-file chunk is lower rank |
| Wrong diagram family | “AWS network diagram” | PCI edition (`network-diagram-pci`) scores above non-PCI `network-diagram` |

### Tuning options (engine / contract / eval)

1. **Search-contract `default_exclude` (compliance)** — when `folder=diagrams/mermaid-catalog`, exclude `diagrams/mermaid-catalog/pages/**` unless query mentions “confluence” / “page body”. Keeps companion markdown for publish workflow without polluting diagram recall.
2. **Path suffix boost** — small FTS/embed rerank bump for `**/diagram.mmd` and `chunk_kind in (mermaid_node, mermaid_edge)` under catalog paths (mirror table_row habits).
3. **Catalog join enrichment** — inject catalog `title`, `type`, and slug aliases into `mermaid_file` + `mermaid_header` chunk text so slug queries (“integrated-api-v2”) hit `.mmd` not only `pages/integrated-api-v2.md`.
4. **Flatten template tokens** — add synonym tokens at index time (e.g. `Tuition` from `Tuition Program`, `RAPI` from node id) for entity-style queries without loosening eval.
5. **Eval hygiene** — align `expect_entity` with flattened labels; treat `mermaid_header` as satisfying `mermaid_file` when path matches; optional `exclude: [diagrams/mermaid-catalog/pages/**]` in eval YAML to measure pure `.mmd` recall.
6. **A/B rerank** — `APO_RERANK=1 just search-eval` after (1)+(2); gate remains ≥80% hit@3 on compliance file.

Re-run after each change:

```bash
just search-eval --file ~/.apo/search-eval-mermaid-compliance.yaml
just search-eval --file ~/.apo/search-eval-mermaid-work-fenced.yaml
```
