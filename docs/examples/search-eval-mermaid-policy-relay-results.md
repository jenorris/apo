# Mermaid policy-relay eval results

**Date:** 2026-08-24
**Index:** `~/.apo/index-compliance.db` (rebuild, 1808 chunks, ollama:bge-m3)
**Eval fixtures:** `search-eval-mermaid-compliance.yaml`, `search-eval-mermaid-policy-relay.yaml`

## Gates

| Tier | Metric | Gate | Result | Verdict |
|------|--------|------|--------|---------|
| A — Unit flatten/index | unittest | 100% | 19/19 pass | **PASS** |
| B — Catalog baseline | hit@3 | ≥80% | **86.36%** (MRR 0.864) | **PASS** |
| C — Policy relay (catalog) | hit@3 | ≥75% | **83.33%** (MRR 0.708) | **PASS** |
| D — Policy relay (vault-wide) | hit@3 | ≥60% | **50.00%** → **60.00%** (after vault-wide boost) | **PASS** |
| Rerank A/B (`APO_RERANK=1`) | overall | — | 68.18% (same as baseline; C 83%, D 50%) | no lift |

## Verdict on stringification

Flattened `chunks.text` **successfully relays** architecture entities (Stripe, skypad, faber, ECS, CDE, RAPI, Tuition) for **catalog-scoped** search. Diagnostic dump shows zero raw Mermaid syntax in `mermaid_node` rows; catalog prefix + entity tokens are present.

**Vault-wide policy relay** initially failed (50%) because policy/`pages` outranked `.mmd` without `folder=`. A query-gated vault-wide boost (2026-08-24) lifts Tier D to **60%** (gate pass). Remaining misses are mostly wrong diagram family or diagrams absent from the fused pool entirely — not missing flatten tokens.

## Vault-wide boost (engine)

When `folder` is empty and the query matches architecture vocabulary (`CDE`, `cardholder`, `Stripe`, `ECS`, `data flow`, …):

- `diagram.mmd` ×1.35, `mermaid_*` ×1.22
- `pages/` ×0.70 (table_row ×0.55)
- Widen fused candidate fetch to 48, re-sort by boosted score, cut to k

Catalog-scoped multipliers unchanged (1.18 / 1.10 / 0.82 / 0.75).

## Tier B misses (3/22)

| Query | Failure mode | Notes |
|-------|--------------|-------|
| GradGuard renters program billing API | Wrong diagram family + entity | Path at rank 3; `expect_entity: RAPI` / `mermaid_node` not in top-3 scoring |
| tuition program billing in standard flow | Entity/chunk-kind | Correct path dominates top-3, but no top hit satisfies `mermaid_node` + `Tuition` together |
| RAPI to Stripe payment edge | Wrong diagram / chunk-kind | Expected `mermaid_edge`; enrollment + cardholder outrank |

## Tier C misses (2/12)

| Query | Failure mode | Notes |
|-------|--------------|-------|
| remote access cardholder data environment network | Chunk-kind strictness | **Correct path @1**, but hit was not `mermaid_file`/`mermaid_header` |
| PCI compliant third party payment processors | Wrong diagram family + entity | Hit integrated flows, not standard-data-flow Stripe node |

## Tier D misses after vault-wide boost (4/10)

| Query | Top-3 composition | Failure mode |
|-------|-------------------|--------------|
| how does GradGuard transmit cardholder data… | 3× policies | Diagrams never entered fused top-48 |
| what AWS resources are in the CDE | cardholder-data-flow ×2 + policy | Wrong diagram family (want PCI network) |
| no CHD storage payment processor architecture | integrated flows + policy | Wrong diagram family (want standard-data-flow) |
| agency cortana call center agent data flow | cardholder-data-flow ×3 | Wrong diagram (cortana node also on CHD flow); pages demoted ✓ |

Pre-boost misses that **cleared**: `CDE ECS container Stripe payment` (policy beat → diagram hit).

## Recommendations (updated)

1. **Agent habit:** still prefer `folder=diagrams/mermaid-catalog` for architecture questions — highest precision.
2. **Eval hygiene:** drop or soften `expect_chunk_kind: mermaid_file` on remote-access policy-relay query (path@1 already correct).
3. **Vault-wide boost — shipped (this branch):** query-gated path multipliers + pages demotion; D 50%→60%.
4. **Do not** enable default rerank for this problem — A/B showed no lift.
5. **Next if D still soft:** supplemental FTS over `diagrams/mermaid-catalog/**/diagram.mmd` when architecture query and no diagram in fused pool (covers “transmit cardholder data…” miss).

## Artifacts

- Unit tests: `engine/tests/test_mermaid_flatten.py`, `test_catalog_retrieval_boost.py`, extended `test_mermaid_index.py`, fixture `fixtures/cardholder-data-flow.mmd`
- Eval YAML: `docs/examples/search-eval-mermaid-policy-relay.yaml`
- Local JSON: `/tmp/mermaid-catalog-baseline.json`, `/tmp/mermaid-policy-relay.json`, `/tmp/mermaid-policy-relay-boost.json`
