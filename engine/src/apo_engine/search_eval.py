"""Search-quality eval — labeled queries scored as hit@k and MRR@k.

The eval file is YAML and lives *outside* the repo (it references your vault's
paths). See ``docs/examples/search-eval.example.yaml``:

```yaml
vault: ""            # optional vault name (APO_VAULTS registry)
k: 5                 # cutoff (CLI -k overrides)
queries:
  - query: "quarterly planning ritual"
    expect: ["areas/planning/quarterly.md"]   # exact path, or folder prefix ending in /
    folder: ""       # optional folder= scope for this query
    exclude: []      # optional exclude globs for this query
```

A result counts as a hit when its path equals an ``expect`` entry, or — for
entries ending in ``/`` — starts with that prefix. Scoring runs through
``ops.search`` so it measures exactly what MCP/RPC clients get (including
rerank and degraded modes).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import ops


def _is_hit(result_path: str, expect: list[str]) -> bool:
    for e in expect:
        e = str(e).strip()
        if not e:
            continue
        if e.endswith("/"):
            if result_path.startswith(e):
                return True
        elif result_path == e:
            return True
    return False


def _score_hit(
    results: list[dict[str, Any]],
    *,
    expect: list[str],
    expect_chunk_kind: str = "",
    expect_entity: str = "",
    cut: int,
) -> tuple[int | None, dict[str, Any] | None]:
    """Return (rank, matching_result) for path / chunk_kind / entity constraints."""
    kind = (expect_chunk_kind or "").strip()
    entity = (expect_entity or "").strip().lower()
    for i, r in enumerate(results[:cut], start=1):
        src = str(r.get("source") or "")
        if not _is_hit(src, expect):
            continue
        if kind and str(r.get("chunk_kind") or "") != kind:
            continue
        if entity:
            text = str(r.get("text") or r.get("content") or r.get("snippet") or "").lower()
            if entity not in text:
                continue
        return i, r
    return None, None


def load_eval_file(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list):
        raise ValueError("eval file must be a mapping with a `queries` list")
    return data


def run_eval(
    eval_file: str | Path,
    *,
    k: int = 0,
    vault: str = "",
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    """Run every labeled query; return hit@k, MRR@k, and per-query detail.

    ``k`` / ``vault`` / ``exclude`` override the file-level defaults; per-query
    ``folder`` / ``exclude`` still apply.
    """
    spec = load_eval_file(eval_file)
    cut = k or int(spec.get("k") or 5)
    vault_name = vault or str(spec.get("vault") or "")
    global_exclude = exclude if exclude is not None else spec.get("exclude") or []

    rows: list[dict[str, Any]] = []
    hits_at_k = 0
    rr_sum = 0.0
    reranked_any = False
    warnings: set[str] = set()

    for q in spec["queries"]:
        query = str(q.get("query") or "").strip()
        expect = [str(e) for e in (q.get("expect") or [])]
        if not query or not expect:
            continue
        q_exclude = list(global_exclude) + list(q.get("exclude") or [])
        expect_entity = str(q.get("expect_entity") or "").strip()
        snippet = 1 if not expect_entity else 512
        out = ops.search(
            query,
            limit=cut,
            folder=str(q.get("folder") or ""),
            vault=vault_name,
            exclude=q_exclude or None,
            snippet_chars=snippet,
        )
        if not out.get("ok"):
            rows.append({"query": query, "error": out.get("error"), "message": out.get("message")})
            continue
        if out.get("reranked"):
            reranked_any = True
        if out.get("warning"):
            warnings.add(str(out["warning"]))
        expect_kind = str(q.get("expect_chunk_kind") or "").strip()
        expect_entity = str(q.get("expect_entity") or "").strip()
        rank, hit = _score_hit(
            out.get("results") or [],
            expect=expect,
            expect_chunk_kind=expect_kind,
            expect_entity=expect_entity,
            cut=cut,
        )
        if rank is not None:
            hits_at_k += 1
            rr_sum += 1.0 / rank
        row_detail: dict[str, Any] = {
            "query": query,
            "rank": rank,
            "expect": expect,
            "top": [str(r.get("source") or "") for r in (out.get("results") or [])[:3]],
        }
        if expect_kind:
            row_detail["expect_chunk_kind"] = expect_kind
        if expect_entity:
            row_detail["expect_entity"] = expect_entity
        if hit:
            row_detail["hit_chunk_kind"] = hit.get("chunk_kind")
            row_detail["hit_row_key"] = hit.get("row_key")
            row_detail["hit_table_id"] = hit.get("table_id")
        rows.append(row_detail)

    n = len(rows)
    scored = [r for r in rows if "error" not in r]
    return {
        "ok": True,
        "file": str(eval_file),
        "k": cut,
        "vault": vault_name or "default",
        "queries": n,
        "errors": sum(1 for r in rows if "error" in r),
        "hit_at_k": round(hits_at_k / len(scored), 4) if scored else 0.0,
        "mrr_at_k": round(rr_sum / len(scored), 4) if scored else 0.0,
        "reranked": reranked_any,
        "warnings": sorted(warnings),
        "rows": rows,
    }


def format_report(report: dict[str, Any], *, verbose: bool = False) -> str:
    lines = [
        f"search-eval {report['file']} — vault={report['vault']} k={report['k']}",
        f"queries={report['queries']} errors={report['errors']} "
        f"hit@{report['k']}={report['hit_at_k']:.2%} MRR@{report['k']}={report['mrr_at_k']:.3f}"
        + (" (reranked)" if report.get("reranked") else ""),
    ]
    for w in report.get("warnings") or []:
        lines.append(f"WARNING: {w}")
    for r in report["rows"]:
        if "error" in r:
            lines.append(f"  ERR  {r['query']!r}: {r['error']} {r.get('message', '')}")
        elif r["rank"] is None:
            lines.append(f"  MISS {r['query']!r} → wanted {r['expect']}; top: {r['top']}")
        elif verbose and r["rank"] is not None:
            extra = ""
            if r.get("hit_chunk_kind"):
                extra = f" kind={r['hit_chunk_kind']}"
                if r.get("hit_row_key"):
                    extra += f" key={r['hit_row_key']!r}"
            lines.append(f"  ok@{r['rank']} {r['query']!r}{extra}")
    return "\n".join(lines)
