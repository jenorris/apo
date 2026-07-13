"""Core: chunk markdown, embed, build a sqlite-vec index, and search it.

No server, no daemon. One sqlite file holds notes metadata + vectors.
Embeddings come from Ollama (GPU) by default, or fastembed (CPU) as fallback.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sqlite3
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import sqlite_vec

from . import config

# Query-embedding LRU (identical agent searches within TTL skip Ollama).
_query_embed_cache: dict[str, tuple[float, list[float]]] = {}
_query_embed_lock = threading.Lock()

# Schema bootstrap once per index path per process.
_schema_ready: set[str] = set()
# Sole index-writer connection (watch / CLI index) — reuse across commits.
_writer_local = threading.local()

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)")


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def chunk_markdown(text: str, max_chars: int, overlap: int) -> list[tuple[str, int, str]]:
    """Return [(heading_breadcrumb, heading_level, chunk_text)] greedily packed to ~max_chars.

    heading_level is the markdown level of the chunk's governing heading (0 = preamble).
    The breadcrumb alone can't recover it: skipped levels (H3 directly under H1) are
    collapsed out of the join.
    """
    body = strip_frontmatter(text)
    heading: list[str] = []
    blocks: list[tuple[str, int, str]] = []
    buf: list[str] = []

    def flush_block():
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                blocks.append((" › ".join(h for h in heading if h), len(heading), joined))
            buf.clear()

    for line in body.split("\n"):
        m = _HEADING.match(line)
        if m:
            flush_block()
            level, title = len(m.group(1)), m.group(2).strip()
            heading = heading[: level - 1] + [""] * max(0, level - 1 - len(heading)) + [title]
        else:
            buf.append(line)
    flush_block()

    chunks: list[tuple[str, int, str]] = []
    cur_head: str | None = None
    cur_level = 0
    cur: list[str] = []
    cur_len = 0

    def emit():
        nonlocal cur, cur_len, cur_head, cur_level
        if cur:
            chunks.append((cur_head or "", cur_level, "\n\n".join(cur).strip()))
        cur, cur_len, cur_head, cur_level = [], 0, None, 0

    for head, level, btext in blocks:
        if len(btext) > max_chars:
            emit()
            step = max(1, max_chars - overlap)
            for i in range(0, len(btext), step):
                chunks.append((head, level, btext[i : i + max_chars]))
            continue
        if cur and cur_len + len(btext) > max_chars:
            emit()
        if not cur:
            cur_head = head
            cur_level = level
        cur.append(btext)
        cur_len += len(btext) + 2
    emit()
    return chunks


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


def _embed_ollama(texts: list[str], batch: int = 64) -> list[list[float]]:
    out: list[list[float]] = []
    url = f"{config.OLLAMA_URL}/api/embed"
    for i in range(0, len(texts), batch):
        payload = json.dumps({"model": config.MODEL_NAME, "input": texts[i : i + batch]}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
        embs = data.get("embeddings")
        if not embs:
            raise RuntimeError(f"Ollama returned no embeddings (model={config.MODEL_NAME}): {data}")
        out.extend(embs)
    return out


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if config.EMBED_BACKEND == "ollama":
        return _embed_ollama(texts)
    return _embed_fastembed(texts)


def _normalize_query(query: str) -> str:
    return " ".join(query.split())


def query_embed(query: str) -> list[float]:
    """Embed a search query with a short TTL cache for repeated agent lookups."""
    key = _normalize_query(query)
    ttl = config.QUERY_EMBED_TTL
    now = time.monotonic()
    if ttl > 0 and key:
        with _query_embed_lock:
            hit = _query_embed_cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
    vec = embed([query])[0]
    if ttl > 0 and key:
        with _query_embed_lock:
            _query_embed_cache[key] = (now, vec)
            overflow = len(_query_embed_cache) - config.QUERY_EMBED_CACHE_SIZE
            if overflow > 0:
                oldest = sorted(_query_embed_cache, key=lambda k: _query_embed_cache[k][0])[:overflow]
                for k in oldest:
                    del _query_embed_cache[k]
    return vec


def clear_query_embed_cache() -> None:
    with _query_embed_lock:
        _query_embed_cache.clear()


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
def compute_chunk_id(
    source: str,
    start_line: int,
    end_line: int,
    content_hash: str,
    model: str,
) -> str:
    """Composite chunk ID aligned with memsearch / OpenClaw format."""
    raw = f"markdown:{source}:{start_line}:{end_line}:{content_hash}:{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _locate_chunk_lines(lines: list[str], chunk_text: str) -> tuple[int, int]:
    """Best-effort 1-based start/end lines for a chunk body."""
    needle = chunk_text.strip().split("\n")[0][:80]
    start = 1
    for i, line in enumerate(lines):
        if needle and needle in line:
            start = i + 1
            break
    end = min(len(lines), start + max(1, chunk_text.count("\n") + 3))
    return start, end


def _ensure_chunk_columns(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)").fetchall()}
    for name, ddl in (
        ("start_line", "INTEGER NOT NULL DEFAULT 1"),
        ("end_line", "INTEGER NOT NULL DEFAULT 1"),
        ("heading_level", "INTEGER NOT NULL DEFAULT 0"),
        ("chunk_hash", "TEXT"),
    ):
        if name not in cols:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {name} {ddl}")
    db.execute("CREATE INDEX IF NOT EXISTS chunks_hash ON chunks(chunk_hash)")


def connect(path: Path | None = None) -> sqlite3.Connection:
    index = Path(path or config.INDEX_PATH).resolve()
    key = str(index)
    db = sqlite3.connect(str(index), timeout=config.DB_TIMEOUT)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(f"PRAGMA busy_timeout={int(config.DB_TIMEOUT * 1000)}")
    if key not in _schema_ready:
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
            """
        )
        _ensure_chunk_columns(db)
        _schema_ready.add(key)
    return db


def writer_connect() -> sqlite3.Connection:
    """Process-local connection for the sole index writer (watch / index CLI)."""
    db = getattr(_writer_local, "conn", None)
    if db is not None:
        try:
            db.execute("SELECT 1")
            return db
        except sqlite3.Error:
            writer_close()
    db = connect()
    _writer_local.conn = db
    return db


def writer_close() -> None:
    db = getattr(_writer_local, "conn", None)
    if db is None:
        return
    try:
        db.close()
    except sqlite3.Error:
        pass
    _writer_local.conn = None


def _index_key() -> str:
    return str(Path(config.INDEX_PATH).resolve())


RRF_K = 60  # reciprocal-rank-fusion damping


def _fts_query(query: str) -> str | None:
    """Turn a natural-language query into a safe FTS5 MATCH (OR of quoted terms)."""
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1][:24]
    return " OR ".join(f'"{t}"' for t in terms) if terms else None


def ensure_fts(db: sqlite3.Connection) -> None:
    """Backfill the FTS index from existing chunks (for indexes built pre-FTS). No embedding."""
    row = db.execute("SELECT value FROM meta WHERE key='fts_ready'").fetchone()
    if row and row[0] == "1":
        return
    db.execute("DELETE FROM chunks_fts")
    db.executemany(
        "INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
        db.execute("SELECT id, text FROM chunks").fetchall(),
    )
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_ready','1')")
    db.commit()


def _insert_pending_chunks(
    db: sqlite3.Connection,
    pending: list[tuple[str, int, str, str, int, int, int, str]],
    vectors: list[list[float]],
) -> int:
    if not pending:
        return 0
    _ensure_vec_table(db, len(vectors[0]))
    for row, vec in zip(pending, vectors):
        rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id = row
        cur = db.execute(
            """INSERT INTO chunks(path, ord, heading, text, start_line, end_line, heading_level, chunk_hash)
               VALUES (?,?,?,?,?,?,?,?)""",
            (rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id),
        )
        db.execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES (?,?)",
            (cur.lastrowid, sqlite_vec.serialize_float32(vec)),
        )
        db.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", (cur.lastrowid, ctext))
    return len(pending)


def _finalize_index_writes(db: sqlite3.Connection) -> None:
    ensure_fts(db)
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_ready','1')")
    db.commit()


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
    patterns = [".git/*", ".obsidian/*", "*.excalidraw.md"]
    # Engine-level ignore file (APO_IGNORE) plus a vault-root .indexignore, if present.
    for ignore_file in (config.IGNORE_FILE, config.NOTES_ROOT / ".indexignore"):
        if ignore_file.exists():
            for line in ignore_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def _iter_notes(root: Path, ignore: list[str]) -> Iterator[Path]:
    for p in root.rglob("*.md"):
        rel = p.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in ignore):
            continue
        yield p


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


@dataclass
class IndexStats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    chunks: int = 0
    seconds: float = 0.0


def index_vault(rebuild: bool = False, limit: int | None = None, verbose: bool = True) -> IndexStats:
    t0 = time.time()
    root = config.NOTES_ROOT
    if not root.exists():
        raise SystemExit(f"NOTES_ROOT does not exist: {root}")

    if rebuild:
        writer_close()
        _schema_ready.discard(_index_key())
        db = writer_connect()
        db.executescript(
            "DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS vec_chunks; DROP TABLE IF EXISTS chunks_fts;"
        )
        db.execute("DELETE FROM files")
        db.execute("DELETE FROM meta")
        db.commit()
        writer_close()
        _schema_ready.discard(_index_key())

    db = writer_connect()
    ignore = _load_ignore()
    known = {row[0]: (row[1], row[2]) for row in db.execute("SELECT path, mtime, hash FROM files")}
    on_disk: set[str] = set()

    pending: list[tuple[str, int, str, str, int, int, int, str]] = []
    stats = IndexStats()
    mtime_refreshed = False

    notes = list(_iter_notes(root, ignore))
    if limit:
        notes = notes[:limit]

    for p in notes:
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
        if prev:
            _delete_path(db, rel)
            stats.changed += 1
        else:
            stats.added += 1
        lines = text.split("\n")
        for ordi, (heading, hlevel, ctext) in enumerate(chunk_markdown(text, config.MAX_CHARS, config.OVERLAP)):
            start_line, end_line = _locate_chunk_lines(lines, ctext)
            chash = _content_hash(ctext)
            chunk_id = compute_chunk_id(
                f"markdown:{rel}",
                start_line,
                end_line,
                chash,
                config.MODEL_NAME,
            )
            pending.append((rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id))
        db.execute(
            "INSERT OR REPLACE INTO files(path, mtime, hash) VALUES (?,?,?)",
            (rel, st.st_mtime, h),
        )

    if limit is None:
        for rel in list(known):
            if rel not in on_disk:
                _delete_path(db, rel)
                db.execute("DELETE FROM files WHERE path=?", (rel,))
                stats.removed += 1

    work_done = bool(pending) or stats.removed > 0 or stats.added > 0 or stats.changed > 0
    if work_done or mtime_refreshed:
        db.commit()

    vectors: list[list[float]] = []
    if pending:
        if verbose:
            print(
                f"  embedding {len(pending)} chunks via {config.EMBED_BACKEND}:{config.MODEL_NAME} ...",
                flush=True,
            )
        vectors = embed([t[3] for t in pending])
        stats.chunks = _insert_pending_chunks(db, pending, vectors)
        _finalize_index_writes(db)
    elif work_done:
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


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
@dataclass
class Hit:
    path: str
    heading: str
    text: str
    score: float
    chunk_hash: str = ""
    heading_level: int = 0
    start_line: int = 0
    end_line: int = 0
    source: str = ""


def count_chunks() -> int:
    db = connect()
    try:
        return db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        db.close()


def lookup_chunk(chunk_hash: str) -> dict | None:
    db = connect()
    try:
        row = db.execute(
            """SELECT path, heading, text, start_line, end_line, heading_level, chunk_hash
               FROM chunks WHERE chunk_hash = ? LIMIT 1""",
            (chunk_hash,),
        ).fetchone()
        if not row:
            return None
        rel, heading, text, start_line, end_line, hlevel, chash = row
        root = config.NOTES_ROOT
        return {
            "source": str(root / rel),
            "path": rel,
            "heading": heading or "",
            "content": text,
            "start_line": start_line,
            "end_line": end_line,
            "heading_level": hlevel,
            "chunk_hash": chash,
        }
    finally:
        db.close()


def _deserialize_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _vectors_by_content_hash(db: sqlite3.Connection, rel: str) -> dict[str, list[float]]:
    """Map chunk body hash → embedding for an existing path (before delete)."""
    out: dict[str, list[float]] = {}
    for rid, text in db.execute("SELECT id, text FROM chunks WHERE path=?", (rel,)):
        row = db.execute("SELECT embedding FROM vec_chunks WHERE rowid=?", (rid,)).fetchone()
        if not row:
            continue
        out[_content_hash(text)] = _deserialize_vec(row[0])
    return out


@dataclass
class _FilePlan:
    rel: str
    full_path: Path
    mtime: float
    file_hash: str
    text: str = ""
    pending: list[tuple[str, int, str, str, int, int, int, str]] = field(default_factory=list)


def index_file(full_path: Path, verbose: bool = False) -> int:
    """Reindex one note. Returns files updated (0 if unchanged or missing after purge)."""
    full = Path(full_path).resolve()
    n = index_files([full], verbose=verbose)
    if not full.is_file():
        return 0
    return n


def index_files(paths: list[Path] | set[Path], *, verbose: bool = False) -> int:
    """Index many notes with partial chunk reuse and one batched Ollama embed."""
    root = config.NOTES_ROOT
    plans: list[_FilePlan] = []
    purge_rels: list[str] = []

    for raw in sorted(paths, key=lambda p: str(p)):
        full_path = Path(raw).resolve()
        try:
            rel = full_path.relative_to(root).as_posix()
        except ValueError as e:
            raise ValueError(f"path outside vault root: {full_path}") from e
        if not full_path.is_file():
            purge_rels.append(rel)
            continue
        text = full_path.read_text(encoding="utf-8", errors="replace")
        file_hash = _file_hash(text)
        mtime = full_path.stat().st_mtime
        plans.append(
            _FilePlan(rel=rel, full_path=full_path, mtime=mtime, file_hash=file_hash, text=text)
        )

    db = writer_connect()

    # Hash-skip unchanged; keep only plans that need work.
    active: list[_FilePlan] = []
    for plan in plans:
        prev = db.execute("SELECT hash FROM files WHERE path=?", (plan.rel,)).fetchone()
        if prev and prev[0] == plan.file_hash:
            db.execute("UPDATE files SET mtime=? WHERE path=?", (plan.mtime, plan.rel))
            continue
        lines = plan.text.split("\n")
        for ordi, (heading, hlevel, ctext) in enumerate(
            chunk_markdown(plan.text, config.MAX_CHARS, config.OVERLAP)
        ):
            start_line, end_line = _locate_chunk_lines(lines, ctext)
            body_hash = _content_hash(ctext)
            chunk_id = compute_chunk_id(
                f"markdown:{plan.rel}", start_line, end_line, body_hash, config.MODEL_NAME
            )
            plan.pending.append(
                (plan.rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id)
            )
        active.append(plan)

    # Load reusable embeddings before deletes.
    reuse: dict[str, dict[str, list[float]]] = {}
    for plan in active:
        reuse[plan.rel] = _vectors_by_content_hash(db, plan.rel)

    for rel in purge_rels:
        _delete_path_by_rel(db, rel)
    for plan in active:
        _delete_path(db, plan.rel)
    db.commit()

    # Assign vectors: reuse by body hash, else queue for embed.
    all_pending: list[tuple[str, int, str, str, int, int, int, str]] = []
    all_vectors: list[list[float]] = []
    texts_to_embed: list[str] = []
    embed_slots: list[int] = []  # index into all_pending / all_vectors

    for plan in active:
        by_hash = reuse.get(plan.rel, {})
        for row in plan.pending:
            body_hash = _content_hash(row[3])
            slot = len(all_pending)
            all_pending.append(row)
            if body_hash in by_hash:
                all_vectors.append(by_hash[body_hash])
            else:
                all_vectors.append([])  # placeholder
                texts_to_embed.append(row[3])
                embed_slots.append(slot)

    if texts_to_embed:
        if verbose:
            total = len(all_pending)
            print(
                f"  embedding {len(texts_to_embed)}/{total} chunks "
                f"across {len(active)} file(s) ...",
                flush=True,
            )
        embs = embed(texts_to_embed)
        for slot, vec in zip(embed_slots, embs):
            all_vectors[slot] = vec
    elif verbose and active:
        print(f"  reused all chunks for {len(active)} file(s) (no embed)", flush=True)

    if all_pending:
        _insert_pending_chunks(db, all_pending, all_vectors)
    for plan in active:
        db.execute(
            "INSERT OR REPLACE INTO files(path, mtime, hash) VALUES (?,?,?)",
            (plan.rel, plan.mtime, plan.file_hash),
        )
    if purge_rels or active:
        _finalize_index_writes(db)
    else:
        db.commit()  # mtime-only updates

    return len(active) + len(purge_rels)


def _delete_path_by_rel(db: sqlite3.Connection, rel: str) -> None:
    _delete_path(db, rel)
    db.execute("DELETE FROM files WHERE path=?", (rel,))


def purge_source(full_path: Path) -> bool:
    try:
        rel = full_path.resolve().relative_to(config.NOTES_ROOT).as_posix()
    except ValueError:
        return False
    db = writer_connect()
    _delete_path_by_rel(db, rel)
    db.commit()
    return True


def search(
    query: str,
    k: int = 8,
    exclude: list[str] | None = None,
    folder: str = "",
    hybrid: bool = True,
) -> list[Hit]:
    """Hybrid retrieval: dense KNN + FTS5 BM25 fused with reciprocal-rank fusion.

    Hit.score is the fused RRF strength normalized to the best candidate
    (1.0 = top hit), so scores are monotonic with ranking — comparable within
    one result set, not across queries.
    """
    db = connect()
    if db.execute("SELECT value FROM meta WHERE key='dim'").fetchone() is None:
        db.close()
        raise SystemExit("Index is empty — run `apo-engine index` first.")

    n = max(k * 4, config.SEARCH_CANDIDATES)
    fused: dict[int, float] = {}
    frows: list[tuple] = []

    # Overlap Ollama query embed with FTS (lexical path does not need the vector).
    with ThreadPoolExecutor(max_workers=1) as pool:
        embed_fut = pool.submit(query_embed, query)
        if hybrid:
            fts_ready = db.execute("SELECT value FROM meta WHERE key='fts_ready'").fetchone()
            if fts_ready and fts_ready[0] == "1":
                match = _fts_query(query)
                if match:
                    try:
                        frows = db.execute(
                            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                            (match, n),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        frows = []
        qvec = embed_fut.result()

    vrows = db.execute(
        "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(qvec), n),
    ).fetchall()
    for rank, (rid, _) in enumerate(vrows):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (rid,) in enumerate(frows):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)

    if not fused:
        db.close()
        return []
    top = max(fused.values())

    folder_prefix = folder.replace("\\", "/").strip("/")
    ranked = sorted(fused, key=lambda i: fused[i], reverse=True)
    # Over-fetch a bit so folder/exclude filters still fill k; one IN query vs N round-trips.
    fetch_n = min(len(ranked), max(k * 4, k + 16))
    ids = ranked[:fetch_n]
    by_id: dict[int, tuple] = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        for row in db.execute(
            f"""SELECT id, path, heading, text, chunk_hash, heading_level, start_line, end_line
                FROM chunks WHERE id IN ({placeholders})""",
            ids,
        ):
            by_id[row[0]] = row[1:]

    hits: list[Hit] = []
    for rid in ranked:
        row = by_id.get(rid)
        if row is None:
            continue
        path, heading, text, chunk_hash, hlevel, start_line, end_line = row
        if folder_prefix and not path.startswith(folder_prefix + "/"):
            continue
        if exclude and any(fnmatch.fnmatch(path, pat) for pat in exclude):
            continue
        score = fused[rid] / top
        hits.append(
            Hit(
                path=path,
                heading=heading or "",
                text=text,
                score=score,
                chunk_hash=chunk_hash or "",
                heading_level=int(hlevel or 0),
                start_line=int(start_line or 1),
                end_line=int(end_line or 1),
                source=str(config.NOTES_ROOT / path),
            )
        )
        if len(hits) >= k:
            break
    db.close()
    return hits


def stats() -> dict:
    db = connect()
    out = {
        "notes": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "chunks": 0,
        "model": None,
        "backend": None,
        "dim": None,
        "index": str(config.INDEX_PATH),
    }
    try:
        out["chunks"] = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    for key in ("model", "backend", "dim"):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        out[key] = row[0] if row else None
    db.close()
    return out


@dataclass
class QueueStats:
    purged: int = 0
    indexed: int = 0
    vault_stats: IndexStats | None = None


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

    coll = collection or config.COLLECTION
    out = QueueStats()

    rebuild = deferred.consume_rebuild(coll)
    if rebuild is not None:
        out.vault_stats = index_vault(rebuild=bool(rebuild.get("force")), verbose=verbose)
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
