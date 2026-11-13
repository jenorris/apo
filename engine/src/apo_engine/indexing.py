"""Indexing and rebuild orchestration."""

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

from . import content, chunking, embeddings, index_db, link_index, retrieval
from . import ranking

_OWNED_MODULES = (content, chunking, embeddings, index_db, link_index, retrieval)

def _embed_commit_batch() -> int:
    try:
        value = int(getattr(config, "EMBED_COMMIT_BATCH", 64))
    except (TypeError, ValueError):
        value = 64
    return max(1, value)
def index_vault(rebuild: bool = False, limit: int | None = None, verbose: bool = True) -> IndexStats:
    t0 = time.time()
    root = vaults.notes_root()
    if not root.exists():
        raise SystemExit(f"NOTES_ROOT does not exist: {root}")

    try:
        return _index_vault_impl(
            rebuild=rebuild, limit=limit, verbose=verbose, root=root, t0=t0
        )
    except sqlite3.Error:
        writer_reset()
        raise


def _index_vault_impl(
    *,
    rebuild: bool,
    limit: int | None,
    verbose: bool,
    root: Path,
    t0: float,
) -> IndexStats:
    if rebuild:
        writer_close()
        _schema_ready.discard(_index_key())
        _hash_algo_ready.discard(_index_key())
        # Skip hash migrate — we're about to wipe tables anyway.
        db = writer_connect(ensure_hash=False)
        db.executescript(
            """
            DROP TABLE IF EXISTS chunks;
            DROP TABLE IF EXISTS vec_chunks;
            DROP TABLE IF EXISTS chunks_fts;
            DROP TABLE IF EXISTS backlinks;
            """
        )
        db.execute("DELETE FROM files")
        db.execute("DELETE FROM meta")
        db.commit()
        writer_close()
        _schema_ready.discard(_index_key())
        _hash_algo_ready.discard(_index_key())

    db = writer_connect(migrate_verbose=verbose)
    ignore = _load_ignore()
    known = {row[0]: (row[1], row[2]) for row in db.execute("SELECT path, mtime, hash FROM files")}
    on_disk: set[str] = set()

    pending: list[PendingChunk] = []
    # Defer files stamps until after embed — a premature mtime/hash stamp makes the next
    # pass skip the note forever when some chunks fail to embed.
    file_stamps: list[tuple[str, float, str, str | None]] = []
    stats = IndexStats()
    mtime_refreshed = False
    if not rebuild and links_backfill_needed(db):
        backfill_links(verbose=verbose)
        db = writer_connect()
    known_vaults = _known_vault_ids()
    link_sources: list[str] = []
    removed_paths: list[str] = []

    # Stream paths — avoid materializing the full vault path list in memory.
    notes_iter = _iter_notes(root, ignore)
    if limit is not None:
        notes_iter = islice(notes_iter, limit)

    for p in notes_iter:
        rel = p.relative_to(root).as_posix()
        on_disk.add(rel)
        try:
            st = p.stat()
        except OSError:
            continue
        prev = known.get(rel)
        # mtime match ⇒ skip read+hash (hash remains source of truth when mtime moves).
        if prev is not None and abs(float(prev[0]) - st.st_mtime) < 1e-6:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h = _file_hash(text)
        if prev and prev[1] == h:
            db.execute("UPDATE files SET mtime=? WHERE path=?", (st.st_mtime, rel))
            mtime_refreshed = True
            continue
        if _is_embed_quarantined(db, rel, h):
            db.execute("UPDATE files SET mtime=? WHERE path=?", (st.st_mtime, rel))
            mtime_refreshed = True
            continue
        if _embed_retry_backoff_active(db, rel, h):
            continue
        # Unconditional: a file whose embed keeps failing never gets stamped into
        # `files` (by design, see below), so it looks "added" on every scan forever —
        # without this, its backlinks rows never get cleared and pile up unbounded.
        _delete_path(db, rel)
        if prev and prev[1] == "":
            pass  # Embed-fail sentinel — rebuild chunks but keep fail counters on the files row.
        elif prev:
            db.execute("DELETE FROM files WHERE path=?", (rel,))
            stats.changed += 1
        else:
            stats.added += 1
        from apo_engine.note_format import chunk_yaml_note, is_mmd_note, is_yaml_note, parse_yaml_document

        if is_yaml_note(rel):
            fm = parse_yaml_document(text) or {}
            id_prefix = f"yaml:{rel}"
            fm_json = json.dumps(fm, default=str)
            note_rows: list[tuple] = []
            for ordi, (heading, hlevel, ctext, start_line, end_line) in enumerate(
                chunk_yaml_note(text, rel, fm)
            ):
                chash = _content_hash(ctext)
                chunk_id = compute_chunk_id(id_prefix, start_line, end_line, chash, config.MODEL_NAME)
                note_rows.append(
                    (rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id, chash,
                     len(ctext.encode("utf-8")), None)
                )
        elif is_mmd_note(rel):
            fm_json = note_catalog_json(text, rel, mtime=st.st_mtime)
            note_rows = _mmd_chunk_rows(text, rel, mtime=st.st_mtime)
        else:
            fm_json = note_catalog_json(text, rel)
            note_rows = _markdown_chunk_rows(text, rel)
        for row in note_rows:
            sec_bytes = row[9]
            if sec_bytes > config.SECTION_WARN_BYTES and (row[10] is None):
                import logging

                logging.getLogger("apo.index").warning(
                    "section %s:%s is %d bytes (> %d)",
                    rel,
                    row[2] or "(preamble)",
                    sec_bytes,
                    config.SECTION_WARN_BYTES,
                )
            pending.append(row)
        _insert_links(db, rel, _note_links(rel, text, fm_json, known_vaults=known_vaults))
        link_sources.append(rel)
        file_stamps.append((rel, st.st_mtime, h, fm_json, st.st_size))

    if limit is None:
        for rel in list(known):
            if rel not in on_disk:
                _delete_path(db, rel)
                db.execute("DELETE FROM files WHERE path=?", (rel,))
                removed_paths.append(rel)
                stats.removed += 1

    work_done = bool(pending) or stats.removed > 0 or stats.added > 0 or stats.changed > 0
    if work_done or mtime_refreshed:
        db.commit()

    dropped: set[str] = set()
    if pending:
        stats.chunks, dropped = _embed_and_store_pending(db, pending, verbose=verbose)
    plans_by_rel = {
        rel: _EmbedDropPlan(mtime, h, fm_json, file_bytes)
        for rel, mtime, h, fm_json, file_bytes in file_stamps
    }
    quarantined = _record_embed_drops(db, dropped, plans_by_rel, verbose=verbose) if dropped else set()
    stamped = 0
    for rel, mtime, h, fm_json, file_bytes in file_stamps:
        if rel in dropped:
            if rel not in quarantined:
                if rel in known:
                    stats.changed = max(0, stats.changed - 1)
                else:
                    stats.added = max(0, stats.added - 1)
            continue
        db.execute(
            "INSERT OR REPLACE INTO files(path, mtime, hash, frontmatter, bytes) VALUES (?,?,?,?,?)",
            (rel, mtime, h, fm_json, file_bytes),
        )
        stamped += 1
    if rebuild:
        resolve_links(db)
        _mark_links_current(db)
    elif link_sources or removed_paths:
        resolve_links(db, sources=link_sources, paths=link_sources + removed_paths)
    if stamped or dropped or work_done or mtime_refreshed or rebuild:
        _finalize_index_writes(db)

    stats.seconds = time.time() - t0
    return stats


def _delete_path(db: sqlite3.Connection, rel: str) -> None:
    ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE path=?", (rel,))]
    if ids:
        qs = ",".join("?" * len(ids))
        for tbl in ("vec_chunks", "chunks_fts"):
            try:
                db.execute(f"DELETE FROM {tbl} WHERE rowid IN ({qs})", ids)
            except sqlite3.OperationalError:
                pass
        db.execute(f"DELETE FROM chunks WHERE id IN ({qs})", ids)
    db.execute("DELETE FROM backlinks WHERE source=?", (rel,))
@dataclass
class _EmbedDropPlan:
    mtime: float
    file_hash: str
    frontmatter_json: str | None
    file_bytes: int


@dataclass
class _FilePlan:
    rel: str
    full_path: Path
    mtime: float
    file_hash: str
    file_bytes: int = 0
    text: str = ""
    pending: list[PendingChunk] = field(default_factory=list)
    frontmatter_json: str | None = None
    links: list[links.Link] = field(default_factory=list)


def index_file(full_path: Path, verbose: bool = False) -> int:
    """Reindex one note. Returns files updated (0 if unchanged or missing after purge)."""
    full = Path(full_path).resolve()
    n = index_files([full], verbose=verbose)
    if not full.is_file():
        return 0
    return n


def index_files(paths: list[Path] | set[Path], *, verbose: bool = False) -> int:
    """Index many notes with partial chunk reuse and one batched Ollama embed."""
    try:
        return _index_files_impl(paths, verbose=verbose)
    except sqlite3.Error:
        writer_reset()
        raise


def _index_files_impl(paths: list[Path] | set[Path], *, verbose: bool = False) -> int:
    """Index many notes with partial chunk reuse and one batched Ollama embed."""
    root = vaults.notes_root()
    db = writer_connect()
    candidates: list[tuple[str, Path, float]] = []  # rel, path, mtime
    purge_rels: list[str] = []
    ignore_res = _compile_ignore(_load_ignore())
    known_vaults = _known_vault_ids()

    for raw in sorted(paths, key=lambda p: str(p)):
        full_path = Path(raw).resolve()
        try:
            rel = full_path.relative_to(root).as_posix()
        except ValueError as e:
            raise ValueError(f"path outside vault root: {full_path}") from e
        if _is_ignored(rel, ignore_res):
            # High-churn / vault .indexignore — drop any stale catalog rows.
            purge_rels.append(rel)
            continue
        if not full_path.is_file():
            purge_rels.append(rel)
            continue
        try:
            candidates.append((rel, full_path, full_path.stat().st_mtime))
        except OSError:
            # Vanished/locked between is_file and stat — skip; don't abort the batch.
            continue

    # One catalog lookup for the whole batch instead of N+1 SELECTs.
    known: dict[str, tuple[float, str]] = {}
    if candidates:
        rels = [c[0] for c in candidates]
        ph = ",".join("?" * len(rels))
        for path, mtime, h in db.execute(
            f"SELECT path, mtime, hash FROM files WHERE path IN ({ph})", rels
        ):
            known[path] = (float(mtime), h)

    plans: list[_FilePlan] = []
    for rel, full_path, st_mtime in candidates:
        prev = known.get(rel)
        if prev is not None and abs(prev[0] - st_mtime) < 1e-6:
            continue
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_hash = _file_hash(text)
        if prev is not None and prev[1] == file_hash:
            db.execute("UPDATE files SET mtime=? WHERE path=?", (st_mtime, rel))
            continue
        if _is_embed_quarantined(db, rel, file_hash):
            db.execute("UPDATE files SET mtime=? WHERE path=?", (st_mtime, rel))
            continue
        if _embed_retry_backoff_active(db, rel, file_hash):
            continue
        try:
            file_bytes = full_path.stat().st_size
        except OSError:
            file_bytes = len(text.encode("utf-8"))
        plan = _FilePlan(
            rel=rel,
            full_path=full_path,
            mtime=st_mtime,
            file_hash=file_hash,
            file_bytes=file_bytes,
            text=text,
        )
        from apo_engine.note_format import chunk_yaml_note, is_mmd_note, is_yaml_note, parse_yaml_document

        if is_yaml_note(rel):
            fm = parse_yaml_document(text) or {}
            id_prefix = f"yaml:{plan.rel}"
            plan.frontmatter_json = json.dumps(fm, default=str)
            for ordi, (heading, hlevel, ctext, start_line, end_line) in enumerate(
                chunk_yaml_note(text, rel, fm)
            ):
                body_hash = _content_hash(ctext)
                chunk_id = compute_chunk_id(id_prefix, start_line, end_line, body_hash, config.MODEL_NAME)
                plan.pending.append(
                    (plan.rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id,
                     body_hash, len(ctext.encode("utf-8")), None)
                )
        elif is_mmd_note(rel):
            plan.frontmatter_json = note_catalog_json(text, rel, mtime=st_mtime)
            plan.pending.extend(_mmd_chunk_rows(text, rel, mtime=st_mtime))
        else:
            plan.frontmatter_json = note_catalog_json(text, rel)
            plan.pending.extend(_markdown_chunk_rows(text, rel))
        plan.links = _note_links(rel, text, plan.frontmatter_json, known_vaults=known_vaults)
        plans.append(plan)

    active = plans

    # Load reusable embeddings before deletes.
    reuse: dict[str, dict[str, list[float]]] = {}
    for plan in active:
        reuse[plan.rel] = _vectors_by_content_hash(db, plan.rel)

    for rel in purge_rels:
        _delete_path_by_rel(db, rel)
    for plan in active:
        prev = known.get(plan.rel)
        _delete_path(db, plan.rel)
        if not (prev is not None and prev[1] == ""):
            db.execute("DELETE FROM files WHERE path=?", (plan.rel,))
        _insert_links(db, plan.rel, plan.links)
    db.commit()

    # Assign vectors: reuse by body hash, else queue for embed.
    all_pending: list[PendingChunk] = []
    all_vectors: list[list[float] | None] = []
    texts_to_embed: list[str] = []
    embed_slots: list[int] = []
    pending_owner: list[str] = []  # path per all_pending slot

    for plan in active:
        by_hash = reuse.get(plan.rel, {})
        for row in plan.pending:
            body_hash = row[8]
            slot = len(all_pending)
            all_pending.append(row)
            pending_owner.append(plan.rel)
            if body_hash in by_hash:
                all_vectors.append(by_hash[body_hash])
            else:
                all_vectors.append(None)  # placeholder filled below
                texts_to_embed.append(_pending_index_text(row))
                embed_slots.append(slot)

    if texts_to_embed:
        if verbose:
            total = len(all_pending)
            print(
                f"  embedding {len(texts_to_embed)}/{total} chunks "
                f"across {len(active)} file(s) ...",
                flush=True,
            )
        embs = embed(texts_to_embed, verbose=verbose)
        for slot, vec in zip(embed_slots, embs):
            all_vectors[slot] = vec
    elif verbose and active:
        print(f"  reused all chunks for {len(active)} file(s) (no embed)", flush=True)

    dropped = {
        pending_owner[i]
        for i, vec in enumerate(all_vectors)
        if vec is None and i < len(pending_owner)
    }
    # Notes with no chunks never appear in all_vectors — they are not dropped.
    if all_pending:
        for i in range(0, len(all_pending), _embed_commit_batch()):
            batch = _embed_commit_batch()
            part_p = all_pending[i : i + batch]
            part_v = all_vectors[i : i + batch]
            part_o = pending_owner[i : i + batch]
            keep_p: list = []
            keep_v: list = []
            for row, vec, owner in zip(part_p, part_v, part_o):
                if owner in dropped or vec is None:
                    continue
                keep_p.append(row)
                keep_v.append(vec)
            if keep_p:
                _insert_pending_chunks(db, keep_p, keep_v)
                db.commit()
            if verbose and texts_to_embed:
                print(
                    f"  … stored {min(i + batch, len(all_pending))}/{len(all_pending)} chunks",
                    flush=True,
                )
    quarantined: set[str] = set()
    if dropped:
        plans_by_rel = {plan.rel: plan for plan in active}
        quarantined = _record_embed_drops(db, dropped, plans_by_rel, verbose=verbose)
    stamped = 0
    for plan in active:
        if plan.rel in dropped:
            continue
        db.execute(
            "INSERT OR REPLACE INTO files(path, mtime, hash, frontmatter, bytes) VALUES (?,?,?,?,?)",
            (plan.rel, plan.mtime, plan.file_hash, plan.frontmatter_json, plan.file_bytes),
        )
        stamped += 1
    if active or purge_rels:
        touched = [plan.rel for plan in active]
        resolve_links(db, sources=touched, paths=touched + purge_rels)
    if purge_rels or stamped or dropped:
        _finalize_index_writes(db)
    else:
        db.commit()  # mtime-only updates

    return stamped + len(purge_rels)


def reembed_one(rel: str, *, verbose: bool = False) -> int:
    """Watcher-only re-embed of a single note by vault-relative path.

    Named entry point for the sole index-writer (watch / CLI). Delegates to
    :func:`index_files`, which reuses per-chunk vectors by ``content_hash`` — so a
    one-cell table edit re-embeds only the changed ``table_row`` chunk, not the whole
    table. MCP/RPC must never call this; they enqueue the path and let the watcher run.
    """
    return index_files([vaults.notes_root() / rel], verbose=verbose)


def reembed_batch(rels: list[str] | set[str], *, verbose: bool = False) -> int:
    """Watcher-only batched re-embed (CSV import / column ops touch many rows at once).

    Routes through :func:`index_files` so unchanged rows are reused and only the
    changed rows hit the embed backend in one batched call.
    """
    root = vaults.notes_root()
    return index_files([root / r for r in rels], verbose=verbose)


def _delete_path_by_rel(db: sqlite3.Connection, rel: str) -> None:
    _delete_path(db, rel)
    db.execute("DELETE FROM files WHERE path=?", (rel,))


def purge_source(full_path: Path) -> bool:
    try:
        rel = full_path.resolve().relative_to(vaults.notes_root()).as_posix()
    except ValueError:
        return False
    db = writer_connect()
    _delete_path_by_rel(db, rel)
    resolve_links(db, paths=[rel])
    db.commit()
    return True
def process_queues(
    collection: str | None = None,
    *,
    scan_vault: bool = False,
    consume_index: bool = True,
    verbose: bool = False,
) -> QueueStats:
    """Single-writer entry point: consume MCP queues, then optional vault scan.

    When ``consume_index`` is False (watcher debounce path), deferred index paths
    are left for the caller to coalesce; purge/rebuild/scan still run here.
    """
    from . import deferred

    coll = collection or vaults.collection()
    out = QueueStats()

    rebuild = deferred.consume_rebuild(coll)
    if rebuild is not None:
        deferred.mark_rebuild_running(coll)
        try:
            out.vault_stats = index_vault(rebuild=bool(rebuild.get("force")), verbose=verbose)
        finally:
            deferred.clear_rebuild_running(coll)
        return out

    for path in deferred.consume_purge_queue(coll):
        if purge_source(Path(path)):
            out.purged += 1

    if consume_index:
        to_index: list[Path] = []
        for path in deferred.consume_index_queue(coll):
            p = Path(path)
            if p.exists():
                to_index.append(p)
            else:
                try:
                    if purge_source(p):
                        out.purged += 1
                except (OSError, ValueError):
                    pass
        if to_index:
            out.indexed += index_files(to_index, verbose=verbose)

    if scan_vault:
        out.vault_stats = index_vault(verbose=verbose)

    return out


def _wire_modules() -> None:
    context = dict(globals())
    context["ranking"] = ranking
    for module in _OWNED_MODULES:
        for name in module.__owned__:
            context[name] = getattr(module, name)
    context.update(
        {
            "_body_start_line": content.body_start_line,
            "_compile_ignore": content.compile_ignore,
            "_content_hash": content.content_hash,
            "_file_hash": content.file_hash,
            "_is_ignored": content.is_ignored,
            "_has_nan": embeddings.has_nan,
            "_normalize_query": embeddings.normalize_query,
            "_escape_like": retrieval.escape_like,
            "_fts_query": retrieval.fts_query,
        }
    )
    for module in _OWNED_MODULES:
        module.__dict__.update(context)
    globals().update(context)
    for module in _OWNED_MODULES:
        for name in module.__owned__:
            globals()[name] = getattr(module, name)


def _set_compat_attr(name: str, value: object) -> None:
    globals()[name] = value
    for module in _OWNED_MODULES:
        if name in module.__dict__:
            setattr(module, name, value)


_wire_modules()
