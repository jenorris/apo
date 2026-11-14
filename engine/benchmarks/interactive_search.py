"""Reproducible interactive-search microbenchmarks.

The fixture is synthetic and hermetic. It exercises the same multi-vault fanout,
query-embedding cache, and SQLite reader paths as the MCP search tool without
reading a configured vault.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from apo_engine import config, core, embeddings, index_db, ops, vaults


_DIM = 16


def _fake_embed(texts: list[str], **_kwargs: Any) -> list[list[float]]:
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * _DIM
        for token in re.findall(r"\w+", text.lower()):
            slot = int(hashlib.md5(token.encode()).hexdigest(), 16) % _DIM
            vec[slot] += 1.0
        norm = sum(value * value for value in vec) ** 0.5 or 1.0
        out.append([value / norm for value in vec])
    return out


def _percentile_ms(samples_s: list[float], percentile: float) -> float:
    ordered = sorted(samples_s)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index] * 1000.0, 3)


def _timing(samples_s: list[float]) -> dict[str, float]:
    return {
        "p50_ms": _percentile_ms(samples_s, 0.50),
        "p95_ms": _percentile_ms(samples_s, 0.95),
        "mean_ms": round(sum(samples_s) * 1000.0 / len(samples_s), 3),
    }


def _git_provenance(source: Path) -> dict[str, Any]:
    try:
        start = source if source.is_dir() else source.parent
        root = subprocess.check_output(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        revision = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain", "--untracked-files=all"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        diff = subprocess.check_output(
            ["git", "-C", root, "diff", "--binary", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return {
            "revision": revision,
            "dirty": bool(status.strip()),
            "tracked_diff_sha256": hashlib.sha256(diff).hexdigest()[:16] if diff else None,
        }
    except (OSError, subprocess.CalledProcessError):
        return {"revision": "unknown", "dirty": None, "tracked_diff_sha256": None}


@contextmanager
def _indexed_fixture() -> Iterator[dict[str, vaults.VaultBinding]]:
    tmp = Path(tempfile.mkdtemp(prefix="apo-search-bench-"))
    entries: dict[str, dict[str, str]] = {}
    for vault_id in ("alpha", "beta"):
        root = tmp / vault_id
        contracts = root / "system" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "usage-contract.schema.yaml").write_text(
            f"vault_id: {vault_id}\n",
            encoding="utf-8",
        )
        for folder in ("docs", "notes"):
            target = root / folder
            target.mkdir(parents=True)
            for index in range(12):
                (target / f"{vault_id}-{folder}-{index}.md").write_text(
                    f"# {vault_id} {folder} {index}\n\n"
                    f"parallelzzz interactive search {vault_id} {folder} "
                    + "filler " * index
                    + "\n",
                    encoding="utf-8",
                )
        entries[vault_id] = {
            "root": str(root),
            "index": str(tmp / f"{vault_id}.db"),
            "collection": f"bench_{vault_id}",
        }

    registry = tmp / "vaults.json"
    registry.write_text(
        json.dumps({"default": "alpha", "vaults": entries}),
        encoding="utf-8",
    )
    patches = [
        mock.patch.dict(
            os.environ,
            {
                "APO_VAULTS": str(registry),
                "APO_VAULT_PATHS": "",
                "APO_VAULTS_ROOT": "",
                "APO_COLLECTION_ROOT": "",
                "APO_DEFAULT_VAULT": "",
            },
            clear=False,
        ),
        mock.patch.object(core, "embed", _fake_embed),
        mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", []),
        mock.patch.object(config, "QUERY_EMBED_DISK_CACHE", False),
    ]
    try:
        for patch in patches:
            patch.start()
        vaults.invalidate_bindings_cache()
        core.clear_query_embed_cache()
        _default, bindings = vaults.load_bindings()
        for vault_id in ("alpha", "beta"):
            with vaults.bind(bindings[vault_id]):
                core.index_vault(rebuild=True, verbose=False)
        yield bindings
    finally:
        core.clear_query_embed_cache()
        core.writer_close(index_key="")
        core.reader_close(index_key="")
        reset_pool = getattr(ops, "_reset_search_fanout_pool", None)
        if reset_pool is not None:
            reset_pool()
        vaults.invalidate_bindings_cache()
        for patch in reversed(patches):
            patch.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _fanout_search() -> dict[str, Any]:
    out = ops.search(
        "parallelzzz",
        vaults=["alpha", "beta"],
        folders=["docs", "notes"],
        hybrid=False,
        limit=20,
    )
    if not out["ok"] or len(out["results"]) != 20:
        raise RuntimeError(f"unexpected fanout result: {out}")
    return out


def _result_hash(out: dict[str, Any]) -> str:
    stable_results = [
        {
            key: row.get(key)
            for key in ("qualified_path", "source", "content", "score")
        }
        for row in out["results"]
    ]
    return hashlib.sha256(
        json.dumps(stable_results, sort_keys=True).encode()
    ).hexdigest()[:16]


def _fanout_benchmark(samples: int) -> dict[str, Any]:
    read_opens = 0
    lock = threading.Lock()
    real_connect = index_db.connect

    def counted_connect(*args: Any, **kwargs: Any):
        nonlocal read_opens
        if kwargs.get("read_only"):
            with lock:
                read_opens += 1
        return real_connect(*args, **kwargs)

    with mock.patch.object(index_db, "connect", counted_connect):
        for _ in range(5):
            _fanout_search()
        read_opens = 0
        durations: list[float] = []
        result_hash = ""
        for _ in range(samples):
            started = time.perf_counter()
            out = _fanout_search()
            durations.append(time.perf_counter() - started)
            result_hash = _result_hash(out)

    return {
        "samples": samples,
        **_timing(durations),
        "searches_per_second": round(samples / sum(durations), 2),
        "reader_connections_opened_after_warmup": read_opens,
        "result_hash": result_hash,
    }


def _concurrent_fanout_benchmark(samples: int) -> dict[str, Any]:
    callers = 4
    durations: list[float] = []
    expected_hash = ""
    with ThreadPoolExecutor(max_workers=callers) as pool:
        for _ in range(samples):
            barrier = threading.Barrier(callers)

            def run() -> dict[str, Any]:
                barrier.wait()
                return _fanout_search()

            started = time.perf_counter()
            results = [future.result() for future in [pool.submit(run) for _ in range(callers)]]
            durations.append(time.perf_counter() - started)
            hashes = {_result_hash(result) for result in results}
            if len(hashes) != 1:
                raise RuntimeError("concurrent fanout searches returned different results")
            expected_hash = hashes.pop()

    total_searches = samples * callers
    return {
        "samples": samples,
        "concurrent_callers": callers,
        **_timing(durations),
        "searches_per_second": round(total_searches / sum(durations), 2),
        "result_hash": expected_hash,
    }


def _singleflight_benchmark(samples: int, delay_ms: float) -> dict[str, Any]:
    workers = 8
    backend_calls = 0
    calls_lock = threading.Lock()
    backend_lock = threading.Lock()

    def serialized_embed(texts: list[str], **_kwargs: Any) -> list[list[float]]:
        nonlocal backend_calls
        with backend_lock:
            with calls_lock:
                backend_calls += 1
            time.sleep(delay_ms / 1000.0)
            return _fake_embed(texts)

    durations: list[float] = []
    with (
        mock.patch.object(config, "QUERY_EMBED_DISK_CACHE", False),
        mock.patch.object(config, "QUERY_EMBED_TTL", 600.0),
        mock.patch.object(embeddings, "embed", serialized_embed),
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        for sample in range(samples):
            with embeddings._query_embed_lock:
                embeddings._query_embed_cache.clear()
            barrier = threading.Barrier(workers)
            query = f"single flight query {sample}"

            def run() -> list[float]:
                barrier.wait()
                return embeddings.query_embed(query)

            started = time.perf_counter()
            results = [future.result() for future in [pool.submit(run) for _ in range(workers)]]
            durations.append(time.perf_counter() - started)
            if any(result != results[0] for result in results[1:]):
                raise RuntimeError("single-flight callers received different vectors")

    return {
        "samples": samples,
        "concurrent_callers": workers,
        "simulated_backend_delay_ms": delay_ms,
        **_timing(durations),
        "backend_calls": backend_calls,
        "backend_calls_per_round": round(backend_calls / samples, 2),
    }


def _cold_warm_benchmark(samples: int, delay_ms: float) -> dict[str, Any]:
    backend_calls = 0

    def delayed_embed(texts: list[str], **_kwargs: Any) -> list[list[float]]:
        nonlocal backend_calls
        backend_calls += 1
        time.sleep(delay_ms / 1000.0)
        return _fake_embed(texts)

    cold: list[float] = []
    warm: list[float] = []
    with (
        mock.patch.object(config, "QUERY_EMBED_DISK_CACHE", False),
        mock.patch.object(embeddings, "embed", delayed_embed),
    ):
        for sample in range(samples):
            query = f"parallelzzz cache probe {sample}"
            started = time.perf_counter()
            first = ops.search(query, vault="alpha", limit=5)
            cold.append(time.perf_counter() - started)
            started = time.perf_counter()
            second = ops.search(query, vault="alpha", limit=5)
            warm.append(time.perf_counter() - started)
            if first["results"] != second["results"]:
                raise RuntimeError("cold and warm searches returned different results")

    return {
        "samples": samples,
        "simulated_backend_delay_ms": delay_ms,
        "cold": _timing(cold),
        "warm": _timing(warm),
        "backend_calls": backend_calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--singleflight-delay-ms", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be >= 1")

    with _indexed_fixture():
        metrics = {
            "environment": {
                "runtime_git": _git_provenance(Path(ops.__file__).resolve()),
                "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "fanout": _fanout_benchmark(args.samples),
            "fanout_concurrent": _concurrent_fanout_benchmark(args.samples),
            "singleflight": _singleflight_benchmark(
                args.samples,
                args.singleflight_delay_ms,
            ),
            "cold_warm": _cold_warm_benchmark(
                args.samples,
                args.singleflight_delay_ms,
            ),
        }
    payload = json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
