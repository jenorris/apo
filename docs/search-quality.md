# Search quality — measure it, don't vibe it

Apo ships a labeled eval harness so retrieval changes are argued with numbers.
Everything below runs through `ops.search` — the exact path MCP/RPC clients use.

## Eval harness

```bash
cp docs/examples/search-eval.example.yaml ~/.apo/search-eval.yaml   # then label it
just search-eval --file ~/.apo/search-eval.yaml
just search-eval --file ~/.apo/search-eval.yaml --json              # machine-readable
```

The eval file lives **outside the repo** (it names your vault's paths). Label
20–30 queries you actually ask, phrased how you'd ask them — not title copies.
Metrics: **hit@k** (any expected path in top k) and **MRR@k**.

## Measured results (reference desk, 2026-08-03)

25 paraphrased queries over a ~1,900-note PARA vault (Ollama `bge-m3`, k=5):

| Configuration | hit@5 | MRR@5 | Latency/query |
|---|---:|---:|---:|
| Baseline hybrid (BM25 + vector RRF) | 60% | 0.425 | ~0.2 s |
| `--exclude 'inbox/daily/*' 'archives/*'` | **68%** | 0.510 | ~0.2 s |
| Rerank `Xenova/ms-marco-MiniLM-L-6-v2` | 68% | 0.473 | ~0.9 s |
| Rerank + exclude | 68% | **0.513** | ~0.9 s |
| Rerank `BAAI/bge-reranker-base` | 64% | 0.426 | ~4.7 s |

Honest read:

1. **Noise exclusion is the big, free win.** Session logs and archived journals
   crowd out canonical notes in unscoped recall. Configure **per vault** in
   `system/contracts/search-contract.schema.yaml`:

   ```yaml
   search_contract_version: "0.1"
   default_exclude:
     - inbox/daily/*
     - archives/*
   ```

   Audit/session vaults should ship `default_exclude: []` — their primary
   content lives under `inbox/daily`. Legacy desk-wide fallback:
   `APO_SEARCH_EXCLUDE` env (deprecated; used only when no vault contract).

   `folder=`-scoped searches and caller-provided `exclude=` are never touched;
   responses carry `default_exclude` so agents can see the filter was applied.

2. **The reranker is a marginal, opt-in refinement.** MiniLM matches the
   exclude win on hit@5 and adds ~0.003 MRR on top of it — at ~4-5× query
   latency. Larger is not better: `bge-reranker-base` scored *worse* here and
   is too slow for interactive use. Run your own eval before enabling it.

## Reranker setup (opt-in)

```bash
cd engine && .venv/bin/pip install -e '.[rerank]'   # fastembed ONNX cross-encoder
APO_RERANK=1 just search "your query"                # or set in MCP server env
```

- First use downloads the model to the local HF cache; scoring is CPU-only and local.
- `APO_RERANK_MODEL` / `APO_RERANK_POOL` tune model and candidate pool (default 24).
- Responses set `"reranked": true`; failures (missing extra, load error) fall
  back to fused order with a `warning` — never a hard error.

## Degraded mode (embed backend down)

If the query embedding fails (Ollama not running), search **falls back to
BM25-only** and returns a `warning` naming the backend and the fix — it never
silently returns `[]`. The CLI prints the same warning to stderr.

## Acceptance rule for retrieval changes

Any change to chunking, fusion, demotion, or reranking must show its eval
before/after in the PR description (`just search-eval --json`). No lift, no merge.
