"""Embedding backends, query embeddings, and embedding caches."""

from __future__ import annotations

import fnmatch
import hashlib
import heapq
import json
import logging
import os
import re
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cmp_to_key
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator

import sqlite_vec
import yaml

try:
    import numpy as _np
except ImportError:
    _np = None

from . import config, links, oversize, ref_catalog, rerank, table_markdown, vaults
from .index_models import Hit, IndexStats, QueueStats

def _embed_commit_batch() -> int:
    try:
        value = int(getattr(config, "EMBED_COMMIT_BATCH", 64))
    except (TypeError, ValueError):
        value = 64
    return max(1, value)


def has_nan(vector: list[float]) -> bool:
    return any(value != value for value in vector)

def normalize_query(query: str) -> str:
    return " ".join(query.split())
# Query-embedding LRU (identical agent searches within TTL skip Ollama).
_query_embed_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
_query_embed_lock = threading.Lock()
_query_embed_inflight: dict[tuple[str, str, str], Future[list[float]]] = {}
# >1 so concurrent MCP searches (each already on a worker thread) can embed in parallel.
# Reused across search() calls — avoid ThreadPoolExecutor create/teardown per query.
_http_client: Any = None
_http_client_lock = threading.Lock()


def _get_http_client() -> Any:
    """Shared keep-alive HTTP client for Ollama / llama-server (None if httpx absent)."""
    global _http_client
    if _http_client is not None:
        return _http_client or None
    with _http_client_lock:
        if _http_client is None:
            try:
                import httpx

                _http_client = httpx.Client(
                    timeout=120.0,
                    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                )
            except ImportError:
                _http_client = False
    return _http_client or None


def _post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    client = _get_http_client()
    if client is not None:
        try:
            resp = client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


# --------------------------------------------------------------------------- #
# Embedding backends
# --------------------------------------------------------------------------- #
_fastembed = None


def _embed_fastembed(texts: list[str]) -> list[list[float]]:
    global _fastembed
    if _fastembed is None:
        from fastembed import TextEmbedding

        _fastembed = TextEmbedding(model_name=config.MODEL_NAME)
    return [v.tolist() for v in _fastembed.embed(texts)]


def _ollama_embed_request(
    texts: list[str],
    *,
    keep_alive: str | int | None = None,
) -> list[list[float]]:
    url = f"{config.OLLAMA_URL}/api/embed"
    body: dict[str, Any] = {"model": config.MODEL_NAME, "input": texts}
    if keep_alive is not None and keep_alive != "":
        body["keep_alive"] = keep_alive
    data = _post_json(url, body)
    embs = data.get("embeddings")
    if not embs or len(embs) != len(texts):
        raise RuntimeError(f"Ollama returned {len(embs) if embs else 0} embeddings for {len(texts)} inputs")
    return embs


def _llamacpp_embed_request(texts: list[str]) -> list[list[float]]:
    """POST /v1/embeddings to a ``llama-server --embeddings`` process."""
    data = _post_json(
        f"{config.LLAMACPP_URL}/v1/embeddings",
        {"model": config.MODEL_NAME, "input": texts},
    )
    rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
    if len(rows) != len(texts):
        raise RuntimeError(f"llama-server returned {len(rows)} embeddings for {len(texts)} inputs")
    return [r["embedding"] for r in rows]


def _server_embed_request(
    texts: list[str],
    *,
    keep_alive: str | int | None = None,
) -> list[list[float]]:
    if config.EMBED_BACKEND == "llamacpp":
        return _llamacpp_embed_request(texts)
    return _ollama_embed_request(texts, keep_alive=keep_alive)


def _truncate_dim(vec: list[float] | None) -> list[float] | None:
    """Matryoshka truncation: first ``EMBED_DIM`` components, re-normalized."""
    dim = config.EMBED_DIM
    if vec is None or dim <= 0 or len(vec) <= dim:
        return vec
    head = vec[:dim]
    norm = sum(x * x for x in head) ** 0.5 or 1.0
    return [x / norm for x in head]


def _embed_batch_resilient(
    texts: list[str],
    poisoned: list[int],
    *,
    keep_alive: str | int | None = None,
) -> list[list[float] | None]:
    """Embed a batch; on HTTP error or NaN output, bisect to isolate and skip only the
    poisoned input(s) — a numerically-unstable chunk shouldn't fail the whole reindex.

    Seen in practice: some inputs make the quantized bge-m3 GGUF runner emit NaN, which
    Ollama itself then fails to JSON-encode (HTTP 500). Deterministic per input, unrelated
    to obvious content features (charset, length) — bisection is the only cheap isolator.

    `poisoned` collects a placeholder per skipped chunk, not the chunk text itself — vault
    content must never land in logs (this engine indexes compliance/employer-sensitive notes).
    """
    try:
        embs = _server_embed_request(texts, keep_alive=keep_alive)
        if not any(has_nan(v) for v in embs):
            return embs
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, TimeoutError):
        pass
    if len(texts) == 1:
        poisoned.append(1)
        return [None]
    mid = len(texts) // 2
    return _embed_batch_resilient(
        texts[:mid], poisoned, keep_alive=keep_alive
    ) + _embed_batch_resilient(texts[mid:], poisoned, keep_alive=keep_alive)


def embed_concurrency() -> int:
    """Embed batches allowed in flight at once; 1 for the in-process fastembed backend."""
    if config.EMBED_BACKEND not in ("ollama", "llamacpp"):
        return 1
    try:
        value = int(getattr(config, "EMBED_CONCURRENCY", 1))
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _embed_ollama(
    texts: list[str],
    batch: int | None = None,
    verbose: bool = False,
    *,
    keep_alive: str | int | None = None,
) -> list[list[float] | None]:
    batch = batch or _embed_commit_batch()
    out: list[list[float] | None] = []
    poisoned: list[int] = []
    parts = [texts[i : i + batch] for i in range(0, len(texts), batch)]
    for _, vecs in iter_embedded_in_order(
        lambda part: _embed_batch_resilient(part, poisoned, keep_alive=keep_alive), parts
    ):
        out.extend(vecs)
    if poisoned and verbose:
        print(
            f"  WARNING: {len(poisoned)} chunk(s) skipped — embedder returned NaN/error "
            f"(content omitted from logs; re-run with a healthy backend to recover them)",
            flush=True,
        )
    return out


def iter_embedded_in_order(fn, parts: list) -> Iterator[tuple[Any, Any]]:
    """Yield ``(part, fn(part))`` in input order, running up to ``embed_concurrency()``
    calls of ``fn`` ahead on worker threads while the caller consumes.

    ``fn`` must not touch SQLite — the caller keeps every write on its own thread.
    """
    workers = min(embed_concurrency(), len(parts))
    if workers <= 1:
        for part in parts:
            yield part, fn(part)
        return
    remaining = iter(parts)
    window: deque = deque()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="apo-embed") as pool:
        try:
            for part in islice(remaining, workers):
                window.append((part, pool.submit(fn, part)))
            while window:
                part, fut = window.popleft()
                result = fut.result()
                nxt = next(remaining, None)
                if nxt is not None:
                    window.append((nxt, pool.submit(fn, nxt)))
                yield part, result
        finally:
            for _, fut in window:
                fut.cancel()


def embed(
    texts: list[str],
    verbose: bool = False,
    *,
    keep_alive: str | int | None = None,
    as_query: bool = False,
) -> list[list[float] | None]:
    """Embed texts. Passages get ``DOC_PREFIX``; ``as_query`` texts are used as given
    (the caller already applied ``QUERY_PREFIX``). Output is cut to ``EMBED_DIM``."""
    if not texts:
        return []
    if config.DOC_PREFIX and not as_query:
        texts = [config.DOC_PREFIX + t for t in texts]
    if config.EMBED_BACKEND in ("ollama", "llamacpp"):
        out = _embed_ollama(texts, verbose=verbose, keep_alive=keep_alive)
    else:
        out = _embed_fastembed(texts)
    return [_truncate_dim(v) for v in out]
def _query_embed_meta_key(norm_query: str) -> str:
    model = (config.MODEL_NAME or "").strip()
    digest = hashlib.blake2b(f"{model}\0{norm_query}".encode(), digest_size=16).hexdigest()
    return f"qemb:{digest}"


def _query_embed_disk_ttl() -> float:
    ttl = getattr(config, "QUERY_EMBED_DISK_TTL", config.QUERY_EMBED_TTL)
    try:
        return float(ttl)
    except (TypeError, ValueError):
        return config.QUERY_EMBED_TTL


_QCACHE_PRUNE_INTERVAL = 600.0
_qcache_last_prune: dict[str, float] = {}
_qcache_local = threading.local()
_qcache_ready: set[str] = set()
_qcache_ready_lock = threading.Lock()


def query_embed_cache_path() -> Path:
    """Sidecar for persisted query embeddings — never index.db, which only the watcher writes."""
    index = Path(vaults.index_path())
    return index.with_name(f"{index.stem}.qcache.db")


def _qcache_conns() -> dict[str, sqlite3.Connection]:
    conns = getattr(_qcache_local, "conns", None)
    if conns is None:
        conns = {}
        _qcache_local.conns = conns
    return conns


def _qcache_reset(path: Path | None = None) -> None:
    target = query_embed_cache_path() if path is None else path
    key = str(target.resolve())
    with _qcache_ready_lock:
        _qcache_ready.discard(key)
    db = _qcache_conns().pop(key, None)
    if db is not None:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _qcache_connect(*, create: bool) -> sqlite3.Connection | None:
    path = query_embed_cache_path()
    key = str(path.resolve())
    if not create and not path.exists():
        _qcache_reset(path)
        return None
    conns = _qcache_conns()
    db = conns.get(key)
    if db is not None:
        with _qcache_ready_lock:
            ready = key in _qcache_ready
        if ready and path.exists():
            return db
        _qcache_reset(path)
    db = sqlite3.connect(str(path), timeout=0.25)
    try:
        db.execute("PRAGMA synchronous=NORMAL")
        with _qcache_ready_lock:
            ready = key in _qcache_ready
        if not ready:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS qemb "
                "(key TEXT PRIMARY KEY, model TEXT, at REAL, q TEXT, vec BLOB)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS qemb_model_at ON qemb(model, at)")
            with _qcache_ready_lock:
                _qcache_ready.add(key)
    except sqlite3.Error:
        db.close()
        raise
    conns[key] = db
    return db


def _unpack_vec(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _load_query_embed_disk(norm_query: str) -> list[float] | None:
    if not norm_query or not getattr(config, "QUERY_EMBED_DISK_CACHE", False):
        return None
    ttl = _query_embed_disk_ttl()
    if ttl <= 0:
        return None
    try:
        db = _qcache_connect(create=False)
        if db is None:
            return None
        row = db.execute(
            "SELECT vec FROM qemb WHERE key=? AND model=? AND at>=?",
            (_query_embed_meta_key(norm_query), config.MODEL_NAME, time.time() - ttl),
        ).fetchone()
        if not row or not row[0]:
            return None
        return _unpack_vec(row[0]) or None
    except (OSError, sqlite3.Error, struct.error, TypeError, ValueError):
        _qcache_reset()
        return None


def _save_query_embed_disk(norm_query: str, vec: list[float]) -> None:
    if not norm_query or not vec or not getattr(config, "QUERY_EMBED_DISK_CACHE", False):
        return
    ttl = _query_embed_disk_ttl()
    if ttl <= 0:
        return
    try:
        db = _qcache_connect(create=True)
        if db is None:
            return
        now = time.time()
        db.execute(
            "INSERT OR REPLACE INTO qemb(key, model, at, q, vec) VALUES (?,?,?,?,?)",
            (
                _query_embed_meta_key(norm_query),
                config.MODEL_NAME,
                now,
                norm_query,
                sqlite_vec.serialize_float32(vec),
            ),
        )
        path_key = str(query_embed_cache_path())
        if now - _qcache_last_prune.get(path_key, 0.0) >= _QCACHE_PRUNE_INTERVAL:
            db.execute("DELETE FROM qemb WHERE at < ?", (now - ttl,))
            _qcache_last_prune[path_key] = now
        db.commit()
    except (OSError, sqlite3.Error, struct.error, TypeError, ValueError):
        _qcache_reset()


def _hydrate_query_embed_from_disk(*, limit: int | None = None) -> int:
    """Load recent valid disk query embeddings into the in-memory LRU."""
    if not getattr(config, "QUERY_EMBED_DISK_CACHE", False):
        return 0
    cap = limit if limit is not None else int(getattr(config, "QUERY_EMBED_WARM_DISK_LIMIT", 64))
    if cap <= 0:
        return 0
    ttl = _query_embed_disk_ttl()
    if ttl <= 0:
        return 0
    try:
        db = _qcache_connect(create=False)
        if db is None:
            return 0
        rows = db.execute(
            "SELECT q, vec FROM qemb WHERE model=? AND at>=? ORDER BY at DESC LIMIT ?",
            (config.MODEL_NAME, time.time() - ttl, cap),
        ).fetchall()
    except (OSError, sqlite3.Error):
        _qcache_reset()
        return 0
    now_mono = time.monotonic()
    candidates: list[tuple[str, list[float]]] = []
    for q, blob in rows:
        if not isinstance(q, str) or not q.strip() or not blob:
            continue
        try:
            vec = _unpack_vec(blob)
        except (struct.error, TypeError):
            continue
        if vec:
            candidates.append((normalize_query(q), vec))
    hydrated = 0
    with _query_embed_lock:
        for q, vec in candidates:
            if q in _query_embed_cache:
                continue
            _query_embed_cache[q] = (now_mono, vec)
            _query_embed_cache.move_to_end(q)
            hydrated += 1
        while len(_query_embed_cache) > config.QUERY_EMBED_CACHE_SIZE:
            _query_embed_cache.popitem(last=False)
    return hydrated


def warm_query_embed(*, preload_ollama: bool | None = None) -> dict[str, Any]:
    """Hydrate query-embed disk cache and optionally preload the Ollama embed model."""
    stats: dict[str, Any] = {"disk_hydrated": 0, "ollama_warmed": False}
    stats["disk_hydrated"] = _hydrate_query_embed_from_disk()
    if preload_ollama is None:
        preload_ollama = getattr(config, "QUERY_EMBED_WARM_ON_START", True)
    if preload_ollama and config.EMBED_BACKEND == "ollama":
        keep_alive = getattr(config, "QUERY_EMBED_KEEP_ALIVE", "5m") or "5m"
        try:
            _ollama_embed_request(["."], keep_alive=keep_alive)
            stats["ollama_warmed"] = True
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, TimeoutError, OSError):
            pass
    return stats


def query_embed(query: str) -> list[float]:
    """Embed a search query with a short TTL cache for repeated agent lookups.

    Applies ``config.QUERY_PREFIX`` (asymmetric BGE query instruction) on the query
    side only — the passage/index path in :func:`embed` is never prefixed, so toggling
    the prefix needs no reindex. Cache key is the raw (unprefixed) normalized query.
    Concurrent misses for the same model, prefix, and query share one backend call.
    """
    key = normalize_query(query)
    ttl = config.QUERY_EMBED_TTL
    now = time.monotonic()
    prefix = getattr(config, "QUERY_PREFIX", "") or ""
    flight_key = (config.MODEL_NAME, prefix, key)
    owner = False
    future: Future[list[float]] | None = None
    if key:
        with _query_embed_lock:
            if ttl > 0:
                hit = _query_embed_cache.get(key)
                if hit is not None and now - hit[0] < ttl:
                    _query_embed_cache.move_to_end(key)
                    return hit[1]
            future = _query_embed_inflight.get(flight_key)
            if future is None:
                future = Future()
                _query_embed_inflight[flight_key] = future
                owner = True
        if not owner:
            return future.result()
    try:
        if ttl > 0 and key:
            disk = _load_query_embed_disk(key)
            if disk is not None:
                with _query_embed_lock:
                    _query_embed_cache[key] = (now, disk)
                    _query_embed_cache.move_to_end(key)
                    while len(_query_embed_cache) > config.QUERY_EMBED_CACHE_SIZE:
                        _query_embed_cache.popitem(last=False)
                if future is not None:
                    future.set_result(disk)
                return disk
        keep_alive = getattr(config, "QUERY_EMBED_KEEP_ALIVE", "5m") or None
        vec = embed(
            [prefix + query if prefix else query],
            keep_alive=keep_alive if config.EMBED_BACKEND == "ollama" else None,
            as_query=True,
        )[0]
        if ttl > 0 and key and vec is not None:
            with _query_embed_lock:
                _query_embed_cache[key] = (now, vec)
                _query_embed_cache.move_to_end(key)
                while len(_query_embed_cache) > config.QUERY_EMBED_CACHE_SIZE:
                    _query_embed_cache.popitem(last=False)
            _save_query_embed_disk(key, vec)
        if future is not None:
            future.set_result(vec)
        return vec
    except BaseException as exc:
        if future is not None:
            future.set_exception(exc)
        raise
    finally:
        if owner:
            with _query_embed_lock:
                if _query_embed_inflight.get(flight_key) is future:
                    _query_embed_inflight.pop(flight_key, None)


def clear_query_embed_cache() -> None:
    with _query_embed_lock:
        _query_embed_cache.clear()
    try:
        qdb = _qcache_connect(create=False)
        if qdb is not None:
            qdb.execute("DELETE FROM qemb")
            qdb.commit()
    except (OSError, sqlite3.Error):
        pass
    finally:
        _qcache_reset()
    try:
        # Legacy rows from when the cache lived in index.db meta.
        # Call-time import: index_db imports this module at load time.
        from .index_db import writer_connect

        db = writer_connect()
        db.execute("DELETE FROM meta WHERE key LIKE 'qemb:%'")
        db.commit()
    except (OSError, sqlite3.Error):
        pass

__owned__ = ['_embed_commit_batch', 'has_nan', 'normalize_query', '_query_embed_cache', '_query_embed_lock', '_http_client', '_http_client_lock', '_get_http_client', '_post_json', '_fastembed', '_embed_fastembed', '_ollama_embed_request', '_llamacpp_embed_request', '_server_embed_request', '_truncate_dim', '_embed_batch_resilient', 'embed_concurrency', '_embed_ollama', 'iter_embedded_in_order', 'embed', '_query_embed_meta_key', '_query_embed_disk_ttl', '_QCACHE_PRUNE_INTERVAL', '_qcache_last_prune', '_qcache_local', '_qcache_ready', '_qcache_ready_lock', 'query_embed_cache_path', '_qcache_conns', '_qcache_reset', '_qcache_connect', '_unpack_vec', '_load_query_embed_disk', '_save_query_embed_disk', '_hydrate_query_embed_from_disk', 'warm_query_embed', 'query_embed', 'clear_query_embed_cache']
