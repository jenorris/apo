"""Index schema, connections, transactions, health, and storage primitives."""

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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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

# Schema bootstrap once per index path per process.
_schema_ready: set[str] = set()
# Process-local: skip meta check after the first ensure for this index path.
_hash_algo_ready: set[str] = set()
# Sole index-writer connection (watch / CLI index) — reuse across commits.
_writer_local = threading.local()
# Cached read-only connection per thread — reused across search/filter_notes/etc. calls.
_reader_local = threading.local()
# Per-index lock backoff + embed-drop retry state (keyed by resolved index path).
_index_health_lock = threading.Lock()
_index_health: dict[str, dict[str, float]] = {}


class IndexBusyError(Exception):
    """SQLite index is locked or busy (MCP should fail fast, not hang)."""

# Content identity for files.hash / chunks.content_hash. blake2b is stdlib-only and
# substantially faster than SHA-256 on large notes; digest sizes keep hex widths stable
# (64-char file hash, 16-char content hash) so columns and logs stay comparable.
HASH_ALGO = "blake2b"
def _ensure_files_columns(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(files)").fetchall()}
    if "frontmatter" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN frontmatter TEXT")
    if "bytes" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN bytes INTEGER")
    if "embed_fails" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN embed_fails INTEGER NOT NULL DEFAULT 0")
    if "embed_fail_hash" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN embed_fail_hash TEXT")
    if "embed_quarantined" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN embed_quarantined INTEGER NOT NULL DEFAULT 0")
    if "embed_fail_at" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN embed_fail_at REAL")

def _ensure_chunk_columns(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)").fetchall()}
    for name, ddl in (
        ("start_line", "INTEGER NOT NULL DEFAULT 1"),
        ("end_line", "INTEGER NOT NULL DEFAULT 1"),
        ("heading_level", "INTEGER NOT NULL DEFAULT 0"),
        ("chunk_hash", "TEXT"),
        # Body-only hash for embed reuse without re-hashing chunk text on every save.
        ("content_hash", "TEXT"),
        # Redundant copy of the vector also stored in vec_chunks: vec0 point/batch lookups
        # by rowid are ~200x slower than a plain table (measured: 87ms vs 0.4ms for 185
        # rows; re-measured on sqlite-vec 0.1.9: 150x for a 185-id IN batch, 245x for
        # per-note reuse, 15x for a 15k-chunk folder fetch) — it's built for KNN search,
        # not this access pattern. Existing rows backfill lazily (NULL until next touch);
        # _vectors_by_content_hash treats a miss as "not reusable" and falls back to
        # re-embedding, so this is safe without a forced rebuild.
        ("embedding", "BLOB"),
        ("section_bytes", "INTEGER"),
        # Table-awareness (0.6): section | table_header | table_row. NULL = section
        # (rows written before this column existed read as plain sections).
        ("chunk_kind", "TEXT"),
        ("parent_chunk_hash", "TEXT"),
        ("table_id", "TEXT"),
        ("table_schema_hash", "TEXT"),
        ("row_index", "INTEGER"),
        ("row_key", "TEXT"),
    ):
        if name not in cols:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {name} {ddl}")
    db.execute("CREATE INDEX IF NOT EXISTS chunks_hash ON chunks(chunk_hash)")
    db.execute("CREATE INDEX IF NOT EXISTS chunks_content_hash ON chunks(content_hash)")
    db.execute("CREATE INDEX IF NOT EXISTS chunks_table ON chunks(table_id)")


def _health_key(index: Path | None = None) -> str:
    return str(Path(index or vaults.index_path()).resolve())


def index_lock_backoff_active(*, index: Path | None = None) -> bool:
    """True while the watcher is backing off after sqlite lock errors on this index."""
    key = _health_key(index)
    now = time.monotonic()
    with _index_health_lock:
        h = _index_health.get(key, {})
        return float(h.get("backoff_until", 0.0)) > now


def note_index_lock_error(*, index: Path | None = None) -> float:
    """Record a lock/busy sqlite error; return seconds to sleep before retry."""
    key = _health_key(index)
    with _index_health_lock:
        h = _index_health.setdefault(key, {})
        n = int(h.get("consecutive", 0)) + 1
        h["consecutive"] = float(n)
        delay = min(
            config.WATCH_LOCK_BACKOFF_START * (2 ** (n - 1)),
            config.WATCH_LOCK_BACKOFF_MAX,
        )
        h["backoff_until"] = time.monotonic() + delay
        return delay


def clear_index_lock_health(*, index: Path | None = None) -> None:
    key = _health_key(index)
    with _index_health_lock:
        h = _index_health.get(key)
        if h is not None:
            h.pop("consecutive", None)
            h.pop("backoff_until", None)


def wal_bytes(*, index: Path | None = None) -> int:
    p = Path(index or vaults.index_path()).resolve()
    wal = Path(f"{p}-wal")
    try:
        return wal.stat().st_size if wal.is_file() else 0
    except OSError:
        return 0


def is_wal_over_limit(*, index: Path | None = None) -> bool:
    return wal_bytes(index=index) > config.WAL_LIMIT_BYTES


_MMAP_BYTES = 256 * 1024 * 1024


def _open_sqlite(index: Path, *, timeout: float, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        db = sqlite3.connect(
            f"file:{index}?mode=ro",
            uri=True,
            timeout=timeout,
        )
    else:
        db = sqlite3.connect(str(index), timeout=timeout)
    if not hasattr(db, "enable_load_extension"):
        raise RuntimeError(
            "This Python's sqlite3 cannot load extensions (needed for sqlite-vec). "
            "On macOS use Homebrew Python (`brew install python`), not the system/"
            "python.org build — see https://alexgarcia.xyz/sqlite-vec/python.html"
        )
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    busy_ms = int(timeout * 1000)
    db.execute(f"PRAGMA busy_timeout={busy_ms}")
    # vec0 KNN and folder scans read most of the file; mmap cut them ~3x on a 200MB index.
    db.execute(f"PRAGMA mmap_size={_MMAP_BYTES}")
    return db


def connect(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    index = Path(path or vaults.index_path()).resolve()
    key = str(index)
    if read_only and not index.is_file():
        # RO URI cannot create an index file — bootstrap schema once via a writer connection.
        bootstrap = connect(path=index, read_only=False)
        bootstrap.close()
    timeout = config.DB_READ_TIMEOUT if read_only else config.DB_TIMEOUT
    db = _open_sqlite(index, timeout=timeout, read_only=read_only)
    if not read_only:
        # Per-connection; durable under WAL except for the last commits on power loss.
        db.execute("PRAGMA synchronous=NORMAL")
    if not read_only and key not in _schema_ready:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(f"PRAGMA journal_size_limit={config.WAL_LIMIT_BYTES}")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta   (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files  (path TEXT PRIMARY KEY, mtime REAL, hash TEXT);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                ord INTEGER NOT NULL,
                heading TEXT,
                text TEXT NOT NULL,
                start_line INTEGER NOT NULL DEFAULT 1,
                end_line INTEGER NOT NULL DEFAULT 1,
                heading_level INTEGER NOT NULL DEFAULT 0,
                chunk_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);
            CREATE TABLE IF NOT EXISTS backlinks (
                source TEXT NOT NULL,
                target_key TEXT NOT NULL,
                target_stem TEXT NOT NULL,
                line INTEGER NOT NULL,
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS backlinks_target ON backlinks(target_key);
            CREATE INDEX IF NOT EXISTS backlinks_stem ON backlinks(target_stem);
            CREATE INDEX IF NOT EXISTS backlinks_source ON backlinks(source);
            CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
            """
        )
        _ensure_chunk_columns(db)
        _ensure_files_columns(db)
        _ensure_link_columns(db)
        _schema_ready.add(key)
    elif read_only and f"{key}#ro" not in _schema_ready:
        # Separate marker: a reader must not mark the index ready for the writer, or
        # the writer skips writer-only migrations (e.g. _ensure_link_columns).
        _ensure_chunk_columns(db)
        _ensure_files_columns(db)
        _schema_ready.add(f"{key}#ro")
    return db


def _tls_map(local: threading.local, attr: str) -> dict:
    m = getattr(local, attr, None)
    if m is None:
        m = {}
        setattr(local, attr, m)
    return m


def writer_connect(
    *, migrate_verbose: bool = False, ensure_hash: bool = True
) -> sqlite3.Connection:
    """Process-local connection for the sole index writer (watch / CLI index).

    Keyed by active index path so multi-vault watchers/writers do not share one sqlite.
    """
    key = _index_key()
    conns = _tls_map(_writer_local, "conns")
    pings = _tls_map(_writer_local, "ping_at")
    db = conns.get(key)
    now = time.monotonic()
    ping_iv = float(getattr(config, "READER_PING_INTERVAL", 5.0))
    if db is not None:
        last = float(pings.get(key, 0.0))
        if ping_iv <= 0 or (now - last) >= ping_iv:
            try:
                db.execute("SELECT 1")
                pings[key] = now
                if ensure_hash:
                    _ensure_hash_algo(db, verbose=migrate_verbose)
                return db
            except sqlite3.Error:
                writer_close(index_key=key)
        else:
            if ensure_hash:
                _ensure_hash_algo(db, verbose=migrate_verbose)
            return db
    db = connect()
    conns[key] = db
    pings[key] = now
    if ensure_hash:
        _ensure_hash_algo(db, verbose=migrate_verbose)
    return db


def writer_reset(*, index_key: str | None = None) -> None:
    """Rollback and close the cached writer after a poisoned transaction."""
    key = index_key if index_key is not None else _index_key()
    conns = _tls_map(_writer_local, "conns")
    db = conns.get(key)
    if db is not None:
        try:
            db.rollback()
        except sqlite3.Error:
            pass
    writer_close(index_key=index_key)


def writer_close(*, index_key: str | None = None) -> None:
    """Close writer connection(s). Default: active index only; pass '' to close all."""
    conns = _tls_map(_writer_local, "conns")
    pings = _tls_map(_writer_local, "ping_at")
    if index_key == "":
        keys = list(conns)
    else:
        keys = [index_key if index_key is not None else _index_key()]
    for key in keys:
        db = conns.pop(key, None)
        pings.pop(key, None)
        if db is None:
            continue
        try:
            db.close()
        except sqlite3.Error:
            pass
        _hash_algo_ready.discard(key)


def reader_connect() -> sqlite3.Connection:
    """Thread-local cached read-only connection, keyed by active index path.

    Every read function (search, filter_notes, recent_notes, list_backlinks,
    count_chunks, lookup_chunk, stats) previously opened a fresh connect() per call and
    closed it at the end — ~0.25ms of connect+extension-load+close overhead paid on every
    single read, when only the writer path cached a connection. Safe to keep open across
    calls: bare SELECTs aren't wrapped in an explicit transaction (nothing here holds a
    WAL read snapshot open past one query), and SQLite recompiles transparently if the
    schema changes underneath it (verified: a cached reader survives an external full
    rebuild — DROP+CREATE from another connection — with no error and no stale results).
    """
    key = _index_key()
    conns = _tls_map(_reader_local, "conns")
    pings = _tls_map(_reader_local, "ping_at")
    db = conns.get(key)
    now = time.monotonic()
    ping_iv = float(getattr(config, "READER_PING_INTERVAL", 5.0))
    if db is not None:
        last = float(pings.get(key, 0.0))
        if ping_iv <= 0 or (now - last) >= ping_iv:
            try:
                db.execute("SELECT 1")
                pings[key] = now
                return db
            except sqlite3.Error:
                reader_close(index_key=key)
        else:
            return db
    db = connect(read_only=True)
    conns[key] = db
    pings[key] = now
    return db


def reader_close(*, index_key: str | None = None) -> None:
    """Close reader connection(s). Default: active index only; pass '' to close all."""
    conns = _tls_map(_reader_local, "conns")
    pings = _tls_map(_reader_local, "ping_at")
    if index_key == "":
        keys = list(conns)
    else:
        keys = [index_key if index_key is not None else _index_key()]
    for key in keys:
        db = conns.pop(key, None)
        pings.pop(key, None)
        if db is None:
            continue
        try:
            db.close()
        except sqlite3.Error:
            pass


def _index_key() -> str:
    return str(Path(vaults.index_path()).resolve())


RRF_K = 60  # reciprocal-rank-fusion damping


def ensure_fts(db: sqlite3.Connection) -> None:
    """Backfill the FTS index from existing chunks (for indexes built pre-FTS). No embedding.

    Uses INSERT…SELECT so chunk text never materializes in bulk in Python — the
    cleanup (_index_text_for_embedding, same transform the live insert path uses)
    runs per-row via a registered scalar function instead. Does not commit —
    callers (_finalize_index_writes) own the transaction boundary.
    """
    row = db.execute("SELECT value FROM meta WHERE key='fts_ready'").fetchone()
    if row and row[0] == "1":
        return
    db.create_function("_apo_index_text", 2, _index_text_for_embedding, deterministic=True)
    db.execute("DELETE FROM chunks_fts")
    db.execute(
        "INSERT INTO chunks_fts(rowid, text) "
        "SELECT id, CASE WHEN chunk_kind = 'section_part' THEN '' "
        "ELSE _apo_index_text(text, COALESCE(chunk_kind, 'section')) END FROM chunks"
    )
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_ready','1')")


def _insert_pending_chunks(
    db: sqlite3.Connection,
    pending: list[PendingChunk] | list[tuple],
    vectors: list[list[float] | None],
) -> int:
    """Insert chunks with a real vector; silently drops any paired with a failed (None) embed.

    Batches via executemany with explicit ids so vec_chunks / FTS share the same rowids
    without a per-row lastrowid round-trip.
    """
    valid = [(row, vec) for row, vec in zip(pending, vectors) if vec is not None]
    if not valid:
        return 0
    _ensure_vec_table(db, len(valid[0][1]))
    # vec0 doesn't always honor rollback on a failed batch insert (a sqlite-vec
    # limitation) — an aborted batch can leave orphaned rows in vec_chunks/chunks_fts
    # that outlive the rolled-back chunks insert. Recomputing from chunks.id alone
    # would then re-collide with that debris on every retry, forever. Skip past it.
    start_id = max(
        int(db.execute("SELECT COALESCE(MAX(id), 0) FROM chunks").fetchone()[0]),
        int(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM vec_chunks").fetchone()[0]),
        int(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM chunks_fts").fetchone()[0]),
    )
    chunk_rows: list[tuple] = []
    vec_rows: list[tuple] = []
    fts_rows: list[tuple] = []
    for i, (row, vec) in enumerate(valid):
        rid = start_id + 1 + i
        rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id = row[:8]
        body_hash = row[8] if len(row) > 8 else _content_hash(ctext)
        section_bytes = row[9] if len(row) > 9 else len(ctext.encode("utf-8"))
        meta = row[10] if len(row) > 10 else None
        meta = meta if isinstance(meta, dict) else None
        blob = sqlite_vec.serialize_float32(vec)
        chunk_rows.append(
            (
                rid,
                rel,
                ordi,
                heading,
                ctext,
                start_line,
                end_line,
                hlevel,
                chunk_id,
                body_hash,
                blob,
                section_bytes,
                (meta or {}).get("chunk_kind") or "section",
                (meta or {}).get("parent_chunk_hash"),
                (meta or {}).get("table_id"),
                (meta or {}).get("table_schema_hash"),
                (meta or {}).get("row_index"),
                (meta or {}).get("row_key"),
            )
        )
        vec_rows.append((rid, blob))
        kind = (meta or {}).get("chunk_kind") or "section"
        # Window rows are vector-only: the parent section's FTS row already holds the
        # full text, so indexing it again would double-count keyword hits.
        fts_rows.append((rid, "" if kind == "section_part" else _index_text_for_embedding(ctext, kind)))
    db.executemany(
        """INSERT INTO chunks(id, path, ord, heading, text, start_line, end_line, heading_level,
                               chunk_hash, content_hash, embedding, section_bytes,
                               chunk_kind, parent_chunk_hash, table_id, table_schema_hash,
                               row_index, row_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        chunk_rows,
    )
    db.executemany("INSERT INTO vec_chunks(rowid, embedding) VALUES (?,?)", vec_rows)
    db.executemany("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", fts_rows)
    return len(valid)


def _finalize_index_writes(db: sqlite3.Connection, *, optimize: bool = False) -> None:
    """Commit index writes. ``optimize`` (full rebuild only): planner stats + FTS merge.

    Not for incremental writes — the FTS merge rewrites the whole FTS index (~80ms and
    a WAL the size of the FTS b-tree on a 25k-chunk vault).
    """
    ensure_fts(db)
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_ready','1')")
    db.commit()
    if optimize:
        db.execute("ANALYZE")
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        db.commit()
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.Error:
        pass


def checkpoint_wal_if_over_limit(*, verbose: bool = False) -> bool:
    """TRUNCATE checkpoint when the -wal file exceeds ``WAL_LIMIT_BYTES``."""
    if not is_wal_over_limit():
        return False
    try:
        db = writer_connect()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if verbose:
            print(
                f"  wal checkpoint truncate (wal now {wal_bytes()} bytes)",
                flush=True,
            )
        return True
    except sqlite3.Error:
        writer_reset()
        return False


def _files_embed_state(
    db: sqlite3.Connection, rel: str
) -> tuple[int, str | None, int, float]:
    row = db.execute(
        "SELECT embed_fails, embed_fail_hash, embed_quarantined, embed_fail_at "
        "FROM files WHERE path=?",
        (rel,),
    ).fetchone()
    if row is None:
        return 0, None, 0, 0.0
    return int(row[0] or 0), row[1], int(row[2] or 0), float(row[3] or 0.0)


def _is_embed_quarantined(db: sqlite3.Connection, rel: str, file_hash: str) -> bool:
    _fails, fail_hash, quarantined, _at = _files_embed_state(db, rel)
    return bool(quarantined) and fail_hash == file_hash


def _embed_retry_backoff_active(db: sqlite3.Connection, rel: str, file_hash: str) -> bool:
    fails, fail_hash, quarantined, fail_at = _files_embed_state(db, rel)
    if quarantined and fail_hash == file_hash:
        return True
    if fails <= 0 or fail_hash != file_hash:
        return False
    return (time.time() - fail_at) < config.EMBED_FAIL_BACKOFF


def _record_embed_drops(
    db: sqlite3.Connection,
    dropped: set[str],
    plans_by_rel: dict[str, Any],
    *,
    verbose: bool,
) -> set[str]:
    """Persist embed-fail counters; quarantine chronic drops. Returns quarantined paths."""
    quarantined: set[str] = set()
    now = time.time()
    for rel in dropped:
        plan = plans_by_rel.get(rel)
        if plan is None:
            continue
        fh = plan.file_hash
        fails, fail_hash, _q, _at = _files_embed_state(db, rel)
        fails = (fails + 1) if fail_hash == fh else 1
        if fails >= config.EMBED_FAIL_QUARANTINE:
            db.execute(
                "INSERT OR REPLACE INTO files(path, mtime, hash, frontmatter, bytes, "
                "embed_fails, embed_fail_hash, embed_quarantined, embed_fail_at) "
                "VALUES (?,?,?,?,?,?,?,1,?)",
                (
                    rel,
                    plan.mtime,
                    fh,
                    plan.frontmatter_json,
                    plan.file_bytes,
                    fails,
                    fh,
                    now,
                ),
            )
            quarantined.add(rel)
            if verbose:
                print(
                    f"  WARNING: quarantined embed for {rel!r} after {fails} drop(s)",
                    flush=True,
                )
        else:
            # Unstamped retry row — mtime/hash sentinels so the next pass still re-embeds.
            db.execute(
                "INSERT OR REPLACE INTO files(path, mtime, hash, frontmatter, bytes, "
                "embed_fails, embed_fail_hash, embed_quarantined, embed_fail_at) "
                "VALUES (?,?,?,?,?,?,?,0,?)",
                (rel, 0.0, "", plan.frontmatter_json, plan.file_bytes, fails, fh, now),
            )
            if verbose:
                print(
                    f"  WARNING: embed drop for {rel!r} — retry {fails}/"
                    f"{config.EMBED_FAIL_QUARANTINE} on next index",
                    flush=True,
                )
    if dropped and verbose and not quarantined:
        print(
            f"  WARNING: {len(dropped)} file(s) left unstamped after embed drop — "
            "will retry on next index (not a permanent mtime-skip)",
            flush=True,
        )
    return quarantined


def _retry_failed_embeds_degraded(
    part: list,
    texts: list[str],
    vectors: list[list[float] | None],
) -> list[list[float] | None]:
    """Give each failed chunk one more try on its leading half before the file is dropped.

    A chunk that cannot embed (input too long for the backend, a NaN-prone input) used
    to take its whole note out of the index. A vector from the head of the chunk keeps
    the note searchable; the warning names the path and ord only, never the content
    (this engine indexes compliance-sensitive notes). If the retry also fails the vector
    stays ``None`` and the caller drops the file as before, so a dead backend still
    leaves the file unstamped for the next pass.
    """
    limit = max(config.EMBED_MAX_CHARS // 2, 1000) if config.EMBED_MAX_CHARS > 0 else 3000
    out = list(vectors)
    for i, vec in enumerate(vectors):
        if vec is not None or len(texts[i]) <= limit:
            continue
        retry = embed([texts[i][:limit]], verbose=False)
        if retry and retry[0] is not None:
            out[i] = retry[0]
            logging.getLogger("apo.index").warning(
                "embed degraded for %s ord=%s: full input failed, indexed its first %d chars",
                part[i][0],
                part[i][1],
                limit,
            )
    return out


def _embed_and_store_pending(
    db: sqlite3.Connection,
    pending: list[PendingChunk] | list[tuple],
    *,
    verbose: bool = False,
) -> tuple[int, set[str]]:
    """Embed pending chunks in batches, committing after each batch for crash durability.

    Returns ``(stored_count, dropped_paths)`` — paths with any ``None`` vector must not
    receive a files mtime/hash stamp, or the next pass will mtime-skip and never retry.
    """
    if not pending:
        return 0, set()
    total = len(pending)
    batch = _embed_commit_batch()
    if verbose:
        print(
            f"  embedding {total} chunks via {config.EMBED_BACKEND}:{config.MODEL_NAME} ...",
            flush=True,
        )
    dropped: set[str] = set()
    stored = 0

    def _embed_part(part: list) -> list[list[float] | None]:
        part_texts = [_pending_index_text(t) for t in part]
        return _retry_failed_embeds_degraded(part, part_texts, embed(part_texts, verbose=False))

    parts = [pending[i : i + batch] for i in range(0, total, batch)]
    for n, (part, part_vectors) in enumerate(iter_embedded_in_order(_embed_part, parts)):
        i = n * batch
        if verbose:
            print(f"  … embedded {min(i + batch, total)}/{total}", flush=True)
        for row, vec in zip(part, part_vectors):
            if vec is None:
                dropped.add(row[0])
        filtered_p: list = []
        filtered_v: list = []
        for row, vec in zip(part, part_vectors):
            if row[0] in dropped or vec is None:
                continue
            filtered_p.append(row)
            filtered_v.append(vec)
        if filtered_p:
            stored += _insert_pending_chunks(db, filtered_p, filtered_v)
            db.commit()
            if verbose:
                print(
                    f"  … stored {min(i + batch, total)}/{total} ({stored} kept)",
                    flush=True,
                )
    if dropped and verbose:
        pass  # caller logs via _record_embed_drops
    return stored, dropped


def _ensure_vec_table(db: sqlite3.Connection, dim: int) -> None:
    cur = db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()
    if cur is None:
        db.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dim}])")
        db.execute("INSERT INTO meta(key,value) VALUES('dim',?)", (str(dim),))
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('model',?)", (config.MODEL_NAME,))
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('backend',?)", (config.EMBED_BACKEND,))
    elif int(cur[0]) != dim:
        raise SystemExit(
            f"Index dim {cur[0]} != model dim {dim}. Model changed — run `index --rebuild`."
        )


def _load_ignore() -> list[str]:
    from apo_engine.note_format import DEFAULT_YAML_IGNORE

    patterns = [".git/*", ".obsidian/*", "*.excalidraw.md", *DEFAULT_YAML_IGNORE]
    # Engine-level ignore file (APO_IGNORE) plus a vault-root .indexignore, if present.
    for ignore_file in (config.IGNORE_FILE, vaults.notes_root() / ".indexignore"):
        if ignore_file.exists():
            for line in ignore_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def _prune_dir_names(ignore: list[str]) -> set[str]:
    """Directory basenames to drop during ``os.walk`` (from ``name/*`` ignore patterns)."""
    names = {".git", ".obsidian", ".trash"}
    for raw in ignore:
        pat = raw.replace("\\", "/").strip()
        if pat.endswith("/*") and "/" not in pat[:-2] and not any(c in pat[:-2] for c in "*?["):
            names.add(pat[:-2])
    return names


def _iter_notes(root: Path, ignore: list[str]) -> Iterator[Path]:
    """Yield note paths, pruning ignored directories so we never descend into ``.obsidian`` etc."""
    from apo_engine.note_format import is_note_path

    ignore_res = _compile_ignore(ignore)
    prune = _prune_dir_names(ignore)
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in prune]
        for name in filenames:
            if not is_note_path(name):
                continue
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if _is_ignored(rel, ignore_res):
                continue
            yield p


def _stamp_hash_algo(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('hash_algo', ?)",
        (HASH_ALGO,),
    )


def _migrate_hash_algo(db: sqlite3.Connection, *, verbose: bool = False) -> None:
    """Rewritten hashes for an existing index — no re-embed, chunk_hash anchors kept.

    Updates ``files.hash`` from vault files and ``chunks.content_hash`` from stored
    chunk text. Leaves ``chunk_hash`` alone so search anchors stay valid until a file
    is naturally reindexed.
    """
    root = vaults.notes_root()
    file_updates: list[tuple[str, str]] = []
    if root.exists():
        for (rel,) in db.execute("SELECT path FROM files"):
            full = root / rel
            if not full.is_file():
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_updates.append((_file_hash(text), rel))
    if file_updates:
        db.executemany("UPDATE files SET hash=? WHERE path=?", file_updates)
    else:
        n_files = int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        if n_files and not root.exists():
            raise SystemExit(
                f"hash migration: NOTES_ROOT does not exist ({root}) but index has "
                f"{n_files} files — set APO_NOTES_ROOT before migrating"
            )
        if n_files and root.exists() and verbose:
            print(
                f"  hash migration: 0/{n_files} files readable under {root} "
                f"(check APO_NOTES_ROOT)",
                flush=True,
            )

    chunk_updates = [
        (_content_hash(text or ""), rid)
        for rid, text in db.execute("SELECT id, text FROM chunks")
    ]
    if chunk_updates:
        db.executemany("UPDATE chunks SET content_hash=? WHERE id=?", chunk_updates)

    _stamp_hash_algo(db)
    db.commit()
    if verbose:
        print(
            f"  migrated hash_algo → {HASH_ALGO} "
            f"({len(file_updates)} files, {len(chunk_updates)} chunks)",
            flush=True,
        )


def _ensure_hash_algo(db: sqlite3.Connection, *, verbose: bool = False) -> None:
    """Guarantee ``files``/``chunks`` digests match ``HASH_ALGO`` before writes."""
    key = _index_key()
    if key in _hash_algo_ready:
        return
    row = db.execute("SELECT value FROM meta WHERE key='hash_algo'").fetchone()
    if row and row[0] == HASH_ALGO:
        _hash_algo_ready.add(key)
        return
    try:
        n_files = int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    except sqlite3.OperationalError:
        n_files = 0
    try:
        n_chunks = int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    except sqlite3.OperationalError:
        n_chunks = 0
    if n_files == 0 and n_chunks == 0:
        _stamp_hash_algo(db)
        db.commit()
    else:
        _migrate_hash_algo(db, verbose=verbose)
    _hash_algo_ready.add(key)


# Pending chunk row: path, ord, heading, text, start, end, hlevel, chunk_hash, body_hash
PendingChunk = tuple[str, int, str, str, int, int, int, str, str]
# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def count_chunks() -> int:
    db = reader_connect()
    return db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def lookup_chunk(chunk_hash: str, *, include_text: bool = True) -> dict | None:
    """Look up a chunk by hash. Set ``include_text=False`` for write-anchor metadata only."""
    db = reader_connect()
    tail = (
        "COALESCE(chunk_kind, 'section'), table_id, row_index, row_key, "
        "parent_chunk_hash, COALESCE(content_hash, '')"
    )
    if include_text:
        row = db.execute(
            f"""SELECT path, heading, text, start_line, end_line, heading_level, chunk_hash, {tail}
               FROM chunks WHERE chunk_hash = ? LIMIT 1""",
            (chunk_hash,),
        ).fetchone()
    else:
        row = db.execute(
            f"""SELECT path, heading, start_line, end_line, heading_level, chunk_hash, {tail}
               FROM chunks WHERE chunk_hash = ? LIMIT 1""",
            (chunk_hash,),
        ).fetchone()
    if not row:
        return None
    if include_text:
        (rel, heading, text, start_line, end_line, hlevel, chash,
         chunk_kind, table_id, row_index, row_key, parent, content_hash) = row
    else:
        (rel, heading, start_line, end_line, hlevel, chash,
         chunk_kind, table_id, row_index, row_key, parent, content_hash) = row
        text = None
    root = vaults.notes_root()
    out = {
        "source": str(root / rel),
        "path": rel,
        "heading": heading or "",
        "start_line": start_line,
        "end_line": end_line,
        "heading_level": hlevel,
        "chunk_hash": chash,
        "chunk_kind": chunk_kind or "section",
        "table_id": table_id,
        "row_index": row_index,
        "row_key": row_key,
        "parent_chunk_hash": parent,
        "index_content_hash": content_hash or "",
    }
    if include_text:
        out["content"] = text
    return out


def note_chunk_order(rel: str) -> list[dict]:
    """Ordered chunk cursors for a note — nav prev/next, sibling, and ToC.

    One row per chunk (``ord`` order): title/breadcrumb, chunk_hash, kind, level.
    Reads the index only — no vault file read.
    """
    db = reader_connect()
    out: list[dict] = []
    for heading, chash, hlevel, kind, sbytes, row_key in db.execute(
        """SELECT heading, chunk_hash, heading_level, COALESCE(chunk_kind,'section'),
                  COALESCE(section_bytes, LENGTH(text)), COALESCE(row_key,'')
           FROM chunks WHERE path=? ORDER BY ord""",
        (rel,),
    ):
        out.append(
            {
                "heading": heading or "",
                "chunk_hash": chash or "",
                "heading_level": int(hlevel or 0),
                "chunk_kind": kind or "section",
                "section_bytes": int(sbytes or 0),
                "row_key": row_key or "",
            }
        )
    return out


def _deserialize_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _l2_sq_blob(qvec: list[float], blob: bytes) -> float:
    """Squared L2 between a query vector and a float32 embedding blob (no intermediate list)."""
    n = len(blob) // 4
    vals = memoryview(blob).cast("f")
    dist = 0.0
    qn = len(qvec)
    lim = n if n < qn else qn
    for i in range(lim):
        d = qvec[i] - vals[i]
        dist += d * d
    if n != qn:
        return dist + 1e9
    return dist


_L2_BATCH_SIZE = 4096


def _l2_sq_batch(qvec: list[float], id_blob_pairs: list[tuple[int, bytes]]) -> list[tuple[float, int]]:
    """Squared L2 between ``qvec`` and many float32 embedding blobs.

    Vectorized with numpy when available — one bulk stack + one elementwise op per
    batch instead of a per-row Python loop (the dominant cost of a folder-scoped scan
    over thousands of chunks). Falls back to :func:`_l2_sq_blob` per row otherwise.
    Same dimension-mismatch handling as ``_l2_sq_blob`` (scored 1e9 above qvec, never
    picked — a defensive case for a stale embedding from a since-changed model dim).
    """
    if not id_blob_pairs:
        return []
    if _np is None:
        return [(_l2_sq_blob(qvec, blob), rid) for rid, blob in id_blob_pairs]

    qn = len(qvec)
    # float64 throughout — matches _l2_sq_blob's arithmetic (python floats), not the
    # float32 storage dtype, so ranking is bit-for-bit consistent with the fallback.
    qarr = _np.asarray(qvec, dtype=_np.float64)
    out: list[tuple[float, int]] = []
    same_dim_ids: list[int] = []
    rows: list[Any] = []
    for rid, blob in id_blob_pairs:
        if len(blob) // 4 != qn:
            out.append((1e9, rid))
            continue
        rows.append(_np.frombuffer(blob, dtype="<f4").astype(_np.float64))
        same_dim_ids.append(rid)
    if rows:
        mat = _np.vstack(rows)
        diffs = mat - qarr
        dists = _np.einsum("ij,ij->i", diffs, diffs)
        out.extend(zip(dists.tolist(), same_dim_ids))
    return out


def _folder_vector_via_global_knn(
    db: sqlite3.Connection,
    qvec: list[float],
    folder_prefix: str,
    n: int,
) -> list[tuple[int, float]]:
    """Approximate folder KNN via global vec0 oversample + path filter (large folders)."""
    oversample = max(n * 12, n)
    try:
        vrows = db.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(qvec), oversample),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not vrows:
        return []
    ids = [r[0] for r in vrows]
    placeholders = ",".join("?" * len(ids))
    paths = {
        row[0]: row[1]
        for row in db.execute(
            f"SELECT id, path FROM chunks WHERE id IN ({placeholders})",
            ids,
        )
    }
    prefix = folder_prefix.replace("\\", "/").strip("/") + "/"
    out: list[tuple[int, float]] = []
    for rid, dist in vrows:
        path = paths.get(rid)
        if path and path.startswith(prefix):
            out.append((rid, dist))
        if len(out) >= n:
            break
    return out


def _collapse_part_hits(db: sqlite3.Connection, vrows: list[tuple]) -> list[tuple]:
    """Replace ``section_part`` hits with their parent section, best rank wins.

    Window rows exist only so a long section can be found by text deep inside it; the
    result a caller wants is the section. Parts never appear in FTS, so only vector
    results need this. A part whose parent row is gone (mid-reindex) is dropped.
    """
    if not vrows:
        return vrows
    ids = [r[0] for r in vrows]
    ph = ",".join("?" * len(ids))
    parts = {
        rid: parent
        for rid, parent in db.execute(
            f"SELECT id, parent_chunk_hash FROM chunks WHERE id IN ({ph}) AND chunk_kind='section_part'",
            ids,
        )
    }
    if not parts:
        return vrows
    parent_hashes = sorted({p for p in parts.values() if p})
    parent_ids: dict[str, int] = {}
    if parent_hashes:
        ph = ",".join("?" * len(parent_hashes))
        parent_ids = {
            h: pid
            for h, pid in db.execute(
                f"SELECT chunk_hash, id FROM chunks WHERE chunk_hash IN ({ph}) AND chunk_kind='section'",
                parent_hashes,
            )
        }
    out: list[tuple] = []
    seen: set[int] = set()
    for row in vrows:
        rid = row[0]
        if rid in parts:
            rid = parent_ids.get(parts[rid])
            if rid is None:
                continue
            row = (rid, *row[1:])
        if rid in seen:
            continue
        seen.add(rid)
        out.append(row)
    return out


def _folder_path_ranges(db: sqlite3.Connection, folder_prefix: str) -> tuple[str, list[str]]:
    """SQL predicate + params matching ``path LIKE '<folder>/%'`` that can use ``chunks_path``.

    LIKE is ASCII case-insensitive, so it cannot seek the BINARY index and scans every
    chunk row (blobs included). Resolve the stored spellings of the folder from the
    covering index first, then seek one ``[folder/, folder0)`` range per spelling
    ('0' is the byte after '/').
    """
    n = len(folder_prefix) + 1
    spellings = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT substr(path, 1, ?) FROM chunks WHERE path LIKE ? ESCAPE '\\'",
            (n, _escape_like(folder_prefix) + "/%"),
        )
    ]
    if not spellings:
        return "0", []
    params: list[str] = []
    for s in spellings:
        params.extend((s, s[:-1] + "0"))
    return " OR ".join(["(path >= ? AND path < ?)"] * len(spellings)), params


def _scoped_vector_hits(
    db: sqlite3.Connection,
    qvec: list[float],
    folder_prefix: str,
    n: int,
    *,
    prefer_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    """Exact L2 ranks over folder-scoped ``chunks.embedding``.

    When ``prefer_ids`` is set (hybrid + FTS hits), score those first. If the folder is
    large (``> SCOPED_VECTOR_FULL_SCAN_MAX``) and we have prefer_ids, skip the full
    folder scan — FTS already constrained candidates. Otherwise scan the folder with a
    bounded heap (distances computed in ``_L2_BATCH_SIZE``-row batches, not a full sort
    or a full in-memory materialization of every blob in the folder).
    """
    scored: list[tuple[float, int]] = []

    if prefer_ids:
        ph = ",".join("?" * len(prefer_ids))
        rows = db.execute(
            f"SELECT id, embedding FROM chunks WHERE id IN ({ph}) AND embedding IS NOT NULL",
            prefer_ids,
        ).fetchall()
        scored.extend(_l2_sq_batch(qvec, rows))

    max_full = int(getattr(config, "SCOPED_VECTOR_FULL_SCAN_MAX", 500))
    skip_full = bool(prefer_ids) and len(prefer_ids) >= n
    range_sql, range_params = "", []
    if not skip_full:
        range_sql, range_params = _folder_path_ranges(db, folder_prefix)
        # Cheap count, capped at max_full + 1: only the threshold matters.
        folder_cnt = db.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM chunks WHERE embedding IS NOT NULL "
            f"AND ({range_sql}) LIMIT ?)",
            (*range_params, max_full + 1),
        ).fetchone()[0]
        if folder_cnt > max_full:
            if prefer_ids:
                # Huge folder and we already have FTS prefs: stay prefiltered.
                skip_full = True
            else:
                approx = _folder_vector_via_global_knn(db, qvec, folder_prefix, n)
                if approx:
                    return approx

    if not skip_full:
        seen = {rid for _, rid in scored}
        cur = db.execute(
            f"SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL AND ({range_sql})",
            range_params,
        )
        heap: list[tuple[float, int]] = []

        def _score_batch(batch: list[tuple[int, bytes]]) -> None:
            for dist, rid in _l2_sq_batch(qvec, batch):
                if len(heap) < n:
                    heapq.heappush(heap, (-dist, rid))  # max-heap via negation
                elif dist < -heap[0][0]:
                    heapq.heapreplace(heap, (-dist, rid))

        batch: list[tuple[int, bytes]] = []
        for rid, blob in cur:
            if rid in seen:
                continue
            batch.append((rid, blob))
            if len(batch) >= _L2_BATCH_SIZE:
                _score_batch(batch)
                batch = []
        if batch:
            _score_batch(batch)
        for neg_dist, rid in heap:
            scored.append((-neg_dist, rid))

    scored.sort()
    return [(rid, dist) for dist, rid in scored[:n]]


def _vectors_by_content_hash(db: sqlite3.Connection, rel: str) -> dict[str, list[float]]:
    """Map chunk body hash → embedding for an existing path (before delete).

    Prefers the ``content_hash`` column (no re-hash). Rows written before that column
    existed fall back to hashing ``text``. Reads ``chunks.embedding``, not vec_chunks.
    """
    out: dict[str, list[float]] = {}
    for chash, text, blob in db.execute(
        "SELECT content_hash, text, embedding FROM chunks WHERE path=?",
        (rel,),
    ):
        if blob is None:
            continue
        key = chash or _content_hash(text)
        out[key] = _deserialize_vec(blob)
    return out
def stats() -> dict:
    db = reader_connect()
    out = {
        "notes": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "chunks": 0,
        "model": None,
        "backend": None,
        "dim": None,
        "index": str(vaults.index_path()),
    }
    try:
        out["chunks"] = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    for key in ("model", "backend", "dim"):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        out[key] = row[0] if row else None
    return out


# Heuristic threshold for "one source file has an implausible number of link
# occurrences" (the v0.28.1 backlinks-unbounded-growth bug: 1,954,634 backlinks
# rows for 56 real links in one 30KB file — a ~35,000x blowup). A legitimate
# note, even a heavily cross-linked MOC/hub page, rarely carries more than a
# few hundred distinct wiki-link occurrences in its body. 500 sits comfortably
# above normal hub-note authoring while still catching runaway growth many
# orders of magnitude before the db file balloons to gigabytes.
BACKLINKS_PER_FILE_WARN = int(os.environ.get("APO_BACKLINKS_PER_FILE_WARN", "500"))


def index_health() -> dict[str, Any]:
    """Point-in-time diagnostics for the active vault's index db.

    Surfaces the signals that would have caught the two most recent production
    bugs before someone had to read the raw sqlite files by hand:

    * v0.28.1 — a ``backlinks`` row per source file growing unbounded
      (``backlinks_per_file_max`` / ``flags: backlinks_runaway``).
    * v0.28.3 — ``vec_chunks`` (and, in principle, ``chunks_fts``) rows
      surviving a vec0 batch insert that didn't honor SQL rollback, leaving
      rows with no matching ``chunks.id`` (``vec_chunks_orphans`` /
      ``fts_orphans`` / ``flags: vec_orphans`` / ``fts_orphans``).

    Scoped to whatever vault is active via ``vaults.bind()`` (or the legacy
    single-vault config when nothing is bound) — callers that want every
    registered vault iterate bindings and call this once per vault, the same
    pattern ``stats()`` callers already use.
    """
    idx_path = Path(vaults.index_path()).resolve()
    db = reader_connect()

    def _count(sql: str) -> int:
        try:
            row = db.execute(sql).fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row and row[0] is not None else 0

    try:
        db_bytes = idx_path.stat().st_size if idx_path.is_file() else 0
        last_index_ts = idx_path.stat().st_mtime if idx_path.is_file() else None
    except OSError:
        db_bytes = 0
        last_index_ts = None

    def _id_fingerprint(sql: str) -> tuple | None:
        try:
            return tuple(db.execute(sql).fetchone())
        except sqlite3.OperationalError:
            return None

    files = _count("SELECT COUNT(*) FROM files")
    backlinks_rows = _count("SELECT COUNT(*) FROM backlinks")
    backlinks_per_file_max = _count(
        "SELECT COALESCE(MAX(c), 0) FROM (SELECT COUNT(*) AS c FROM backlinks GROUP BY source)"
    )
    # rowid on both vec_chunks and chunks_fts is chunks.id by construction
    # (_insert_pending_chunks shares one explicit id across all three inserts) —
    # any row whose rowid has no matching chunks.id is debris from an aborted
    # batch that vec0/fts5 didn't roll back with the rest of the transaction.
    #
    # Queried via each virtual table's own shadow table, not the vtab itself:
    # vec0's rowid lives in vec_chunks_rowids.rowid (NOT its "chunk_id" column —
    # that's vec0's own internal storage-block id, an unrelated concept that
    # collides in name with Apo's chunks; verified against the vtab's own
    # answer before relying on this). Going through the vtab materializes every
    # vector (a full scan of the whole embedding blob column) just to check
    # rowid membership — seconds on a large index vs. near-instant here.
    #
    # The anti-join still costs ~15ms per table on a 25k-chunk index, so it only runs
    # when the id-set fingerprints (count, sum, sum of squares) differ. Equal
    # fingerprints with unequal sets need several missing and extra ids that cancel
    # in both sums at once.
    fp_sql = "SELECT COUNT(*), COALESCE(SUM({c}), 0), TOTAL({c} * {c}) FROM {t}"
    chunk_fp = _id_fingerprint(fp_sql.format(c="id", t="chunks"))
    vec_fp = _id_fingerprint(fp_sql.format(c="rowid", t="vec_chunks_rowids"))
    fts_fp = _id_fingerprint(fp_sql.format(c="id", t="chunks_fts_docsize"))
    chunks = int(chunk_fp[0]) if chunk_fp else 0
    vec_chunks = int(vec_fp[0]) if vec_fp else 0
    fts_rows = int(fts_fp[0]) if fts_fp else 0
    vec_chunks_orphans = (
        _count("SELECT COUNT(*) FROM vec_chunks_rowids WHERE rowid NOT IN (SELECT id FROM chunks)")
        if vec_fp and vec_fp != chunk_fp
        else 0
    )
    fts_orphans = (
        _count("SELECT COUNT(*) FROM chunks_fts_docsize WHERE id NOT IN (SELECT id FROM chunks)")
        if fts_fp and fts_fp != chunk_fp
        else 0
    )
    quarantined = _count("SELECT COUNT(*) FROM files WHERE embed_quarantined=1")

    # Watch-loop hook health (git-sync / optima-merge / desk-reprojection) —
    # a small persisted heartbeat the watcher writes per vault; see
    # `watch_health.py`. Read-side only here: a stalled or hung hook needs to
    # surface through the same `index_health()`/`apo-engine doctor` path an
    # operator already checks, per docs/watcher-scheduler-separation.md.
    from . import watch_health

    hooks = watch_health.read_hook_health(vaults.notes_root())

    flags: list[str] = []
    if vec_chunks_orphans > 0:
        flags.append("vec_orphans")
    if fts_orphans > 0:
        flags.append("fts_orphans")
    if backlinks_per_file_max > BACKLINKS_PER_FILE_WARN:
        flags.append("backlinks_runaway")
    if is_wal_over_limit(index=idx_path):
        flags.append("wal_over_limit")
    if quarantined > 0:
        flags.append("embed_quarantined")
    flags.extend(watch_health.hook_health_flags(hooks))

    return {
        "index": str(idx_path),
        "db_bytes": db_bytes,
        "wal_bytes": wal_bytes(index=idx_path),
        "last_index_ts": last_index_ts,
        "files": files,
        "chunks": chunks,
        "vec_chunks": vec_chunks,
        "fts_rows": fts_rows,
        "backlinks_rows": backlinks_rows,
        "backlinks_per_file_max": backlinks_per_file_max,
        "vec_chunks_orphans": vec_chunks_orphans,
        "fts_orphans": fts_orphans,
        "quarantined": quarantined,
        "hooks": hooks,
        "flags": flags,
    }

__owned__ = ['reader_connect', '_schema_ready', '_hash_algo_ready', '_writer_local', '_reader_local', '_index_health_lock', '_index_health', 'IndexBusyError', 'HASH_ALGO', '_ensure_files_columns', '_ensure_chunk_columns', '_health_key', 'index_lock_backoff_active', 'note_index_lock_error', 'clear_index_lock_health', 'wal_bytes', 'is_wal_over_limit', '_MMAP_BYTES', '_open_sqlite', 'connect', '_tls_map', 'writer_connect', 'writer_reset', 'writer_close', 'reader_close', '_index_key', 'RRF_K', 'ensure_fts', '_insert_pending_chunks', '_finalize_index_writes', 'checkpoint_wal_if_over_limit', '_files_embed_state', '_is_embed_quarantined', '_embed_retry_backoff_active', '_record_embed_drops', '_retry_failed_embeds_degraded', '_embed_and_store_pending', '_ensure_vec_table', '_load_ignore', '_prune_dir_names', '_iter_notes', '_stamp_hash_algo', '_migrate_hash_algo', '_ensure_hash_algo', 'PendingChunk', 'count_chunks', 'lookup_chunk', 'note_chunk_order', '_deserialize_vec', '_l2_sq_blob', '_L2_BATCH_SIZE', '_l2_sq_batch', '_folder_vector_via_global_knn', '_collapse_part_hits', '_folder_path_ranges', '_scoped_vector_hits', '_vectors_by_content_hash', 'stats', 'BACKLINKS_PER_FILE_WARN', 'index_health']
