"""Search-quality eval — labeled queries scored as hit@k and MRR@k.

The eval file is YAML and lives *outside* the repo (it references your vault's
paths). See ``docs/examples/search-eval.example.yaml``:

```yaml
vault: ""            # optional vault name (registry vault_id)
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

Two things ``hit@k``/``MRR`` alone miss, both handled here:

- **Staleness.** A vault reorg/migration can rename or delete every ``expect``
  path in a fixture; every query then "misses" and hit@k silently goes to 0%,
  indistinguishable from a real ranking regression. ``run_eval`` checks every
  ``expect`` (and ``expect_entity``, where the path still exists) against disk
  before scoring and reports ``stale_expect``/``stale_entity`` separately —
  see ``check_stale_expects``.
- **Result composition.** hit@k/MRR only ask "is a wanted path anywhere in the
  top k"; they say nothing about whether the other slots are distinct notes or
  near-duplicate siblings of the one already found (e.g. several rows from the
  same table). ``run_eval`` also reports ``composition``: mean distinct paths,
  mean max-same-path, and a ``chunk_kind`` breakdown, all at the eval's ``k``.

``check_eval_file``/``discover_eval_files`` back the ``check-evals`` CLI
command (``just check-evals``): run every fixture found under
``docs/examples/`` and ``~/.apo/``, fail loudly on any stale expect, and warn
on a hit@k drop past ``--regress-threshold`` points from a checked-in
``<fixture>.baseline.json`` snapshot.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from . import ops, vaults


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
            ck = str(r.get("chunk_kind") or "")
            if kind == "mermaid_file" and ck == "mermaid_header":
                pass
            else:
                continue
        if entity:
            text = str(r.get("text") or r.get("content") or r.get("snippet") or "").lower()
            if entity not in text:
                continue
        return i, r
    return None, None


def _resolve_vault_root(vault_name: str) -> Path | None:
    """Resolve a registered vault name to its filesystem root, or ``None``.

    Uses the same registry ``ops.search`` resolves through (``vaults.load_bindings``),
    so the staleness gate checks the *actual* vault an eval file's ``expect`` paths
    are meant to live in — not a guessed layout. Returns ``None`` (not a raised
    error) when the vault isn't loaded in this process; callers treat that as
    "couldn't check", never as "nothing stale".
    """
    try:
        default, bindings = vaults.load_bindings()
    except Exception:
        return None
    key = (vault_name or "").strip() or default
    binding = bindings.get(key)
    if binding is None:
        return None
    try:
        return binding.resolved().root
    except Exception:
        return None


def _expect_exists(root: Path, expect: str) -> bool:
    """A path ``expect`` entry (exact file) or ``prefix/`` entry (>=1 file under it)."""
    if expect.endswith("/"):
        p = root / expect
        if not p.is_dir():
            return False
        return any(child.is_file() for child in p.rglob("*"))
    return (root / expect).is_file()


def check_stale_expects(spec: dict[str, Any], *, vault: str = "") -> dict[str, Any]:
    """Check every ``expect`` (and ``expect_entity``, where resolvable) against disk.

    A ``stale_expect`` is an ``expect`` path that no longer exists in the vault —
    the single highest-value staleness signal: a vault reorg/migration can rename
    or delete a fixture's targets and every query then "misses", indistinguishable
    from a real ranking regression, until someone re-reads the fixture by hand.

    ``stale_entity`` is softer: the ``expect`` path(s) still exist, but the query's
    ``expect_entity`` string isn't found in any of them — usually content moved or
    was relabeled inside the file rather than the file disappearing outright.

    Returns ``{"resolved": False, ...}`` (empty stale lists, ``checked: 0``) when
    the vault root can't be resolved in this process.
    """
    vault_name = vault or str(spec.get("vault") or "")
    root = _resolve_vault_root(vault_name)
    if root is None:
        return {
            "resolved": False,
            "vault": vault_name or "default",
            "vault_root": None,
            "stale_expect": [],
            "stale_expect_count": 0,
            "stale_entity": [],
            "stale_entity_count": 0,
            "checked": 0,
        }

    stale_expect: list[dict[str, Any]] = []
    stale_entity: list[dict[str, Any]] = []
    checked = 0
    for q in spec.get("queries") or []:
        query = str(q.get("query") or "").strip()
        expect = [str(e).strip() for e in (q.get("expect") or []) if str(e).strip()]
        if not query or not expect:
            continue
        live_paths: list[str] = []
        for e in expect:
            checked += 1
            if _expect_exists(root, e):
                live_paths.append(e)
            else:
                stale_expect.append({"query": query, "expect": e})

        entity = str(q.get("expect_entity") or "").strip()
        if entity and live_paths:
            found = False
            for e in live_paths:
                if e.endswith("/"):
                    continue
                try:
                    text = (root / e).read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if entity.lower() in text.lower():
                    found = True
                    break
            if not found:
                stale_entity.append({"query": query, "expect_entity": entity, "expect": expect})

    return {
        "resolved": True,
        "vault": vault_name or "default",
        "vault_root": str(root),
        "stale_expect": stale_expect,
        "stale_expect_count": len(stale_expect),
        "stale_entity": stale_entity,
        "stale_entity_count": len(stale_entity),
        "checked": checked,
    }


def _composition_for(results: list[dict[str, Any]], *, cut: int) -> dict[str, Any]:
    """Result-composition snapshot for one query's top-``cut`` results.

    ``distinct_paths``: how many distinct source notes appear. ``max_same_path``:
    the largest number of results sharing one source (a hint that the other slots
    are near-duplicate siblings, not new information). ``kind_counts``: per-result
    ``chunk_kind`` tally (section / table_row / table_header / mermaid_* / ...).
    """
    top = results[:cut]
    sources = [str(r.get("source") or "") for r in top if r.get("source")]
    counts = Counter(sources)
    kinds = Counter(str(r.get("chunk_kind") or "unknown") for r in top)
    return {
        "distinct_paths": len(counts),
        "max_same_path": max(counts.values()) if counts else 0,
        "n": len(top),
        "kind_counts": dict(kinds),
    }


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

    stale = check_stale_expects(spec, vault=vault_name)
    stale_by_query: dict[str, list[str]] = {}
    for s in stale["stale_expect"]:
        stale_by_query.setdefault(s["query"], []).append(s["expect"])

    rows: list[dict[str, Any]] = []
    hits_at_k = 0
    rr_sum = 0.0
    reranked_any = False
    warnings: set[str] = set()
    comp_distinct_sum = 0
    comp_max_same_sum = 0
    comp_scored = 0
    kind_totals: Counter[str] = Counter()
    kind_total_n = 0

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
        results = out.get("results") or []
        row_detail: dict[str, Any] = {
            "query": query,
            "rank": rank,
            "expect": expect,
            "top": [str(r.get("source") or "") for r in results[:3]],
        }
        if expect_kind:
            row_detail["expect_chunk_kind"] = expect_kind
        if expect_entity:
            row_detail["expect_entity"] = expect_entity
        if hit:
            row_detail["hit_chunk_kind"] = hit.get("chunk_kind")
            row_detail["hit_row_key"] = hit.get("row_key")
            row_detail["hit_table_id"] = hit.get("table_id")
        row_stale = stale_by_query.get(query) or []
        if row_stale:
            row_detail["stale_expect"] = sorted(row_stale)
            row_detail["stale_expect_all"] = set(row_stale) == set(expect)

        comp = _composition_for(results, cut=cut)
        row_detail["distinct_paths_at_k"] = comp["distinct_paths"]
        row_detail["max_same_path_at_k"] = comp["max_same_path"]
        comp_distinct_sum += comp["distinct_paths"]
        comp_max_same_sum += comp["max_same_path"]
        comp_scored += 1
        kind_totals.update(comp["kind_counts"])
        kind_total_n += comp["n"]

        rows.append(row_detail)

    n = len(rows)
    scored = [r for r in rows if "error" not in r]
    composition = {
        "k": cut,
        "queries_scored": comp_scored,
        "mean_distinct_paths_at_k": round(comp_distinct_sum / comp_scored, 3) if comp_scored else 0.0,
        "mean_max_same_path_at_k": round(comp_max_same_sum / comp_scored, 3) if comp_scored else 0.0,
        "kind_share_at_k": (
            {kk: round(vv / kind_total_n, 4) for kk, vv in sorted(kind_totals.items())}
            if kind_total_n
            else {}
        ),
    }
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
        "stale_resolved": stale["resolved"],
        "stale_expect": stale["stale_expect"],
        "stale_expect_count": stale["stale_expect_count"],
        "stale_entity": stale["stale_entity"],
        "stale_entity_count": stale["stale_entity_count"],
        "composition": composition,
        "rows": rows,
    }


def format_report(report: dict[str, Any], *, verbose: bool = False) -> str:
    lines = [
        f"search-eval {report['file']} — vault={report['vault']} k={report['k']}",
        f"queries={report['queries']} errors={report['errors']} "
        f"hit@{report['k']}={report['hit_at_k']:.2%} MRR@{report['k']}={report['mrr_at_k']:.3f}"
        + (" (reranked)" if report.get("reranked") else ""),
    ]
    stale_n = report.get("stale_expect_count", 0)
    if stale_n:
        lines.append(
            f"!!! STALE FIXTURE: {stale_n} expect path(s) not found on disk — "
            "hit@k/MRR above are not trustworthy until this fixture is re-labeled !!!"
        )
    elif report.get("stale_resolved") is False:
        lines.append(
            "NOTE: could not resolve this fixture's vault in this process — "
            "staleness gate skipped (hit@k/MRR below are unverified against disk)."
        )
    for w in report.get("warnings") or []:
        lines.append(f"WARNING: {w}")
    for s in report.get("stale_expect") or []:
        lines.append(f"  STALE expect {s['expect']!r} for query {s['query']!r} — not found under vault root")
    for s in report.get("stale_entity") or []:
        lines.append(
            f"  STALE-ENTITY {s['expect_entity']!r} not found in {s['expect']} for query {s['query']!r}"
        )
    comp = report.get("composition") or {}
    if comp.get("queries_scored"):
        kind_share = ", ".join(f"{kk}={vv:.0%}" for kk, vv in (comp.get("kind_share_at_k") or {}).items())
        lines.append(
            f"composition@{comp['k']}: mean distinct_paths={comp['mean_distinct_paths_at_k']:.2f} "
            f"mean max_same_path={comp['mean_max_same_path_at_k']:.2f}"
            + (f"  kinds: {kind_share}" if kind_share else "")
        )
    for r in report["rows"]:
        if "error" in r:
            lines.append(f"  ERR  {r['query']!r}: {r['error']} {r.get('message', '')}")
        elif r["rank"] is None:
            tag = "STALE" if r.get("stale_expect_all") else "MISS "
            lines.append(f"  {tag} {r['query']!r} → wanted {r['expect']}; top: {r['top']}")
        elif verbose and r["rank"] is not None:
            extra = ""
            if r.get("hit_chunk_kind"):
                extra = f" kind={r['hit_chunk_kind']}"
                if r.get("hit_row_key"):
                    extra += f" key={r['hit_row_key']!r}"
            extra += f" distinct_paths@{report['k']}={r.get('distinct_paths_at_k')}"
            lines.append(f"  ok@{r['rank']} {r['query']!r}{extra}")
    return "\n".join(lines)


# --- Routine-run mechanism (`just check-evals`) ----------------------------- #
#
# A checked-in ``<fixture>.baseline.json`` snapshot sits next to each fixture
# file — simple JSON, no separate benchmarking system. ``check_eval_file`` runs
# one fixture, gates on stale expects, and warns on a hit@k drop past
# ``regress_threshold`` points versus that snapshot (or reports "no-baseline"
# when one hasn't been recorded yet — not a failure, just unverified).

DEFAULT_EVAL_DIRS: tuple[Path, ...] = (Path("docs/examples"), Path.home() / ".apo")


def discover_eval_files(dirs: list[str | Path] | None = None) -> list[Path]:
    """Find candidate eval fixture files under ``dirs`` (default: docs/examples + ~/.apo).

    Skips ``*.example.yaml``/``*.example.yml`` (templates meant to be copied and
    edited, not run as-is) and anything without a literal ``queries:`` key (cheap
    text scan, not a full parse) — e.g. ``~/.apo/search-eval.yaml``, which is an
    index/readme pointing at the real per-vault fixture files, not a fixture
    itself. Anything that still fails to load as a real eval spec is reported as
    a per-file error by ``check_eval_file``, not skipped silently.
    """
    search_dirs = list(DEFAULT_EVAL_DIRS) if dirs is None else [Path(d) for d in dirs]
    out: list[Path] = []
    seen: set[Path] = set()
    for d in search_dirs:
        d = d.expanduser()
        if not d.is_dir():
            continue
        candidates = sorted(d.glob("search-eval*.yaml")) + sorted(d.glob("search-eval*.yml"))
        for p in candidates:
            if p.name.endswith((".example.yaml", ".example.yml")):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "queries:" not in text:
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(p)
    return out


def _baseline_path(fixture: Path) -> Path:
    return fixture.parent / f"{fixture.stem}.baseline.json"


def load_baseline(fixture: str | Path) -> dict[str, Any] | None:
    p = _baseline_path(Path(fixture).expanduser())
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_baseline(fixture: str | Path, report: dict[str, Any]) -> Path:
    """Snapshot a report's headline numbers as the fixture's new baseline."""
    p = _baseline_path(Path(fixture).expanduser())
    snapshot = {
        "hit_at_k": report.get("hit_at_k"),
        "mrr_at_k": report.get("mrr_at_k"),
        "k": report.get("k"),
        "queries": report.get("queries"),
        "composition": report.get("composition"),
    }
    p.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def check_eval_file(
    fixture: str | Path,
    *,
    regress_threshold: float = 15.0,
    save_new_baseline: bool = False,
) -> dict[str, Any]:
    """Run one fixture end to end: score + stale gate + baseline regression check.

    Never raises — a fixture that can't load (bad YAML shape) or can't search
    (unknown vault, index unavailable) comes back with ``ok: False`` and an
    ``error`` message instead of blowing up the whole ``check-evals`` run.
    """
    fixture = Path(fixture).expanduser()
    try:
        report = run_eval(fixture)
    except Exception as e:
        return {"file": str(fixture), "ok": False, "error": str(e), "stale_expect_count": 0, "regressed": False}

    n = report.get("queries") or 0
    errs = report.get("errors") or 0
    if n and errs == n:
        first_err = next((r for r in report.get("rows") or [] if "error" in r), {})
        message = f"{first_err.get('error')} {first_err.get('message') or ''}".strip()
        return {
            "file": str(fixture),
            "ok": False,
            "error": f"all {n} quer{'y' if n == 1 else 'ies'} failed: {message}",
            "stale_expect_count": report.get("stale_expect_count", 0),
            "regressed": False,
        }

    baseline = load_baseline(fixture)
    regressed = False
    regression_points: float | None = None
    if baseline and baseline.get("hit_at_k") is not None:
        drop = (float(baseline["hit_at_k"]) - float(report.get("hit_at_k") or 0.0)) * 100.0
        regression_points = round(drop, 2)
        regressed = drop > regress_threshold

    if save_new_baseline:
        save_baseline(fixture, report)

    return {
        "file": str(fixture),
        "ok": True,
        "k": report.get("k"),
        "vault": report.get("vault"),
        "queries": n,
        "hit_at_k": report.get("hit_at_k"),
        "mrr_at_k": report.get("mrr_at_k"),
        "stale_expect_count": report.get("stale_expect_count", 0),
        "stale_entity_count": report.get("stale_entity_count", 0),
        "composition": report.get("composition"),
        "baseline": baseline,
        "baseline_path": str(_baseline_path(fixture)),
        "baseline_written": bool(save_new_baseline),
        "regressed": regressed,
        "regression_points": regression_points,
        "regress_threshold": regress_threshold,
    }


def check_evals_exit_code(results: list[dict[str, Any]]) -> int:
    """Distinct, priority-ordered exit codes for ``check-evals`` scripting.

    2 (stale expects) outranks 3 (regression) outranks 1 (a fixture errored
    outright) — staleness is the "your fixture lies" case this whole effort
    exists to catch loudly, so it always wins the exit code.
    """
    if any(r.get("stale_expect_count") for r in results):
        return 2
    if any(not r.get("ok") for r in results):
        return 1
    if any(r.get("regressed") for r in results):
        return 3
    return 0


def format_check_evals_report(results: list[dict[str, Any]]) -> str:
    lines = [f"check-evals — {len(results)} fixture(s)"]
    for r in results:
        if not r.get("ok"):
            lines.append(f"  ERROR {r['file']}: {r.get('error')}")
            continue
        flags = []
        if r.get("stale_expect_count"):
            flags.append(f"STALE×{r['stale_expect_count']}")
        if r.get("stale_entity_count"):
            flags.append(f"stale-entity×{r['stale_entity_count']}")
        if r.get("regressed"):
            flags.append(f"REGRESSED -{r['regression_points']:.1f}pt vs baseline")
        if r.get("baseline_written"):
            flags.append("baseline-written")
        elif not r.get("baseline"):
            flags.append("no-baseline")
        flag_s = f"  [{', '.join(flags)}]" if flags else "  [ok]"
        lines.append(
            f"  {r['file']}: hit@{r['k']}={r['hit_at_k']:.2%} MRR@{r['k']}={r['mrr_at_k']:.3f} "
            f"(queries={r['queries']}){flag_s}"
        )
    exit_code = check_evals_exit_code(results)
    if exit_code == 0:
        lines.append("OK — no stale expects, no baseline regressions.")
    else:
        lines.append(f"FAIL (exit {exit_code}) — see flags above.")
    return "\n".join(lines)
