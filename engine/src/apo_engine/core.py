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
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import sqlite_vec
import yaml

from . import config

# Query-embedding LRU (identical agent searches within TTL skip Ollama).
_query_embed_cache: dict[str, tuple[float, list[float]]] = {}
_query_embed_lock = threading.Lock()

# Schema bootstrap once per index path per process.
_schema_ready: set[str] = set()
# Sole index-writer connection (watch / CLI index) — reuse across commits.
_writer_local = threading.local()

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_FRONTMATTER_YAML = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)")
_WIKILINK = re.compile(r"\[\[([^\]#|]+)(?:[#|][^\]]*)?\]\]")


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


def _has_nan(vec: list[float]) -> bool:
    return any(x != x for x in vec)


def _ollama_embed_request(texts: list[str]) -> list[list[float]]:
    url = f"{config.OLLAMA_URL}/api/embed"
    payload = json.dumps({"model": config.MODEL_NAME, "input": texts}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    embs = data.get("embeddings")
    if not embs or len(embs) != len(texts):
        raise RuntimeError(f"Ollama returned {len(embs) if embs else 0} embeddings for {len(texts)} inputs")
    return embs


def _embed_batch_resilient(texts: list[str], poisoned: list[int]) -> list[list[float] | None]:
    """Embed a batch; on HTTP error or NaN output, bisect to isolate and skip only the
    poisoned input(s) — a numerically-unstable chunk shouldn't fail the whole reindex.

    Seen in practice: some inputs make the quantized bge-m3 GGUF runner emit NaN, which
    Ollama itself then fails to JSON-encode (HTTP 500). Deterministic per input, unrelated
    to obvious content features (charset, length) — bisection is the only cheap isolator.

    `poisoned` collects a placeholder per skipped chunk, not the chunk text itself — vault
    content must never land in logs (this engine indexes compliance/employer-sensitive notes).
    """
    try:
        embs = _ollama_embed_request(texts)
        if not any(_has_nan(v) for v in embs):
            return embs
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, TimeoutError):
        pass
    if len(texts) == 1:
        poisoned.append(1)
        return [None]
    mid = len(texts) // 2
    return _embed_batch_resilient(texts[:mid], poisoned) + _embed_batch_resilient(texts[mid:], poisoned)


def _embed_ollama(texts: list[str], batch: int = 64, verbose: bool = False) -> list[list[float] | None]:
    out: list[list[float] | None] = []
    poisoned: list[int] = []
    for i in range(0, len(texts), batch):
        out.extend(_embed_batch_resilient(texts[i : i + batch], poisoned))
    if poisoned and verbose:
        print(
            f"  WARNING: {len(poisoned)} chunk(s) skipped — embedder returned NaN/error "
            f"(content omitted from logs; re-run with a healthy backend to recover them)",
            flush=True,
        )
    return out


def embed(texts: list[str], verbose: bool = False) -> list[list[float] | None]:
    if not texts:
        return []
    if config.EMBED_BACKEND == "ollama":
        return _embed_ollama(texts, verbose=verbose)
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


def _locate_chunk_lines(lines: list[str], chunk_text: str, search_from: int = 0) -> tuple[int, int]:
    """Best-effort 1-based start/end lines for a chunk body.

    Searches from `search_from` (0-based) onward, not always from line 1 — callers
    processing a file's chunks in document order must thread the previous chunk's
    start_line through as the next search_from. Without this, two chunks that happen
    to start with identical text (repeated status/template boilerplate is common)
    both resolve to the *first* occurrence, silently misdirecting chunk_hash-anchored
    writes (append_note) into the wrong section.
    """
    needle = chunk_text.strip().split("\n")[0][:80]
    start = search_from + 1
    for i in range(search_from, len(lines)):
        if needle and needle in lines[i]:
            start = i + 1
            break
    end = min(len(lines), start + max(1, chunk_text.count("\n") + 3))
    return start, end


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_YAML.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_wikilinks(text: str) -> list[tuple[int, str, str, str]]:
    """Return (line, target_key, target_stem, line_text) for each [[wiki-link]] in text."""
    if "[[" not in text:
        return []
    rows: list[tuple[int, str, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _WIKILINK.finditer(line):
            target = m.group(1).strip().removesuffix(".md").lower()
            if not target:
                continue
            stem = target.rsplit("/", 1)[-1]
            rows.append((lineno, target, stem, line.strip()[:200]))
    return rows


# --------------------------------------------------------------------------- #
# Frontmatter query matching (filter_notes)
# --------------------------------------------------------------------------- #
def _loose_eq(a, b) -> bool:
    if isinstance(a, type(b)) or isinstance(b, type(a)):
        return a == b
    return str(a).strip().lower() == str(b).strip().lower()


def _loose_cmp(a, b) -> int:
    try:
        fa, fb = float(a), float(b)
        return (fa > fb) - (fa < fb)
    except (TypeError, ValueError):
        sa, sb = str(a), str(b)
        return (sa > sb) - (sa < sb)


def _match_condition(value, cond) -> bool:
    if not isinstance(cond, dict):
        if isinstance(value, list):
            return any(_loose_eq(x, cond) for x in value)
        return value is not None and _loose_eq(value, cond)

    for op, rhs in cond.items():
        if op == "$exists":
            if bool(rhs) != (value is not None):
                return False
            continue
        if value is None:
            return False
        if op == "$eq":
            if not _match_condition(value, rhs):
                return False
        elif op == "$ne":
            if _match_condition(value, rhs):
                return False
        elif op == "$contains":
            if isinstance(value, list):
                if not any(_loose_eq(x, rhs) for x in value):
                    return False
            elif isinstance(value, str):
                if str(rhs).lower() not in value.lower():
                    return False
            else:
                return False
        elif op in ("$lt", "$lte", "$gt", "$gte"):
            c = _loose_cmp(value, rhs)
            if op == "$lt" and c >= 0:
                return False
            if op == "$lte" and c > 0:
                return False
            if op == "$gt" and c <= 0:
                return False
            if op == "$gte" and c < 0:
                return False
        else:
            return False  # unknown operator never matches
    return True


def _ensure_files_columns(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(files)").fetchall()}
    if "frontmatter" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN frontmatter TEXT")


def _ensure_chunk_columns(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)").fetchall()}
    for name, ddl in (
        ("start_line", "INTEGER NOT NULL DEFAULT 1"),
        ("end_line", "INTEGER NOT NULL DEFAULT 1"),
        ("heading_level", "INTEGER NOT NULL DEFAULT 0"),
        ("chunk_hash", "TEXT"),
        # Redundant copy of the vector also stored in vec_chunks: vec0 point/batch lookups
        # by rowid are ~200x slower than a plain table (measured: 87ms vs 0.4ms for 185
        # rows) — it's built for KNN search, not this access pattern. Existing rows backfill
        # lazily (NULL until next touch); _vectors_by_content_hash treats a miss as "not
        # reusable" and falls back to re-embedding, so this is safe without a forced rebuild.
        ("embedding", "BLOB"),
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
            """
        )
        _ensure_chunk_columns(db)
        _ensure_files_columns(db)
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
    vectors: list[list[float] | None],
) -> int:
    """Insert chunks with a real vector; silently drops any paired with a failed (None) embed."""
    valid = [(row, vec) for row, vec in zip(pending, vectors) if vec is not None]
    if not valid:
        return 0
    _ensure_vec_table(db, len(valid[0][1]))
    for row, vec in valid:
        rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id = row
        blob = sqlite_vec.serialize_float32(vec)
        cur = db.execute(
            """INSERT INTO chunks(path, ord, heading, text, start_line, end_line, heading_level,
                                   chunk_hash, embedding)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id, blob),
        )
        db.execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES (?,?)",
            (cur.lastrowid, blob),
        )
        db.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", (cur.lastrowid, ctext))
    return len(valid)


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
        locate_cursor = 0
        for ordi, (heading, hlevel, ctext) in enumerate(chunk_markdown(text, config.MAX_CHARS, config.OVERLAP)):
            start_line, end_line = _locate_chunk_lines(lines, ctext, search_from=locate_cursor)
            locate_cursor = start_line
            chash = _content_hash(ctext)
            chunk_id = compute_chunk_id(
                f"markdown:{rel}",
                start_line,
                end_line,
                chash,
                config.MODEL_NAME,
            )
            pending.append((rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id))
        fm = _parse_frontmatter(text)
        wikilinks = _extract_wikilinks(text)
        if wikilinks:
            db.executemany(
                "INSERT INTO backlinks(source, target_key, target_stem, line, text) VALUES (?,?,?,?,?)",
                [(rel, tk, ts, ln, tx) for ln, tk, ts, tx in wikilinks],
            )
        db.execute(
            "INSERT OR REPLACE INTO files(path, mtime, hash, frontmatter) VALUES (?,?,?,?)",
            (rel, st.st_mtime, h, json.dumps(fm, default=str) if fm else None),
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
        vectors = embed([t[3] for t in pending], verbose=verbose)
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
    db.execute("DELETE FROM backlinks WHERE source=?", (rel,))


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
    mtime: float = 0.0


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
    """Map chunk body hash → embedding for an existing path (before delete).

    Reads chunks.embedding (a plain column), not vec_chunks — vec0 point/batch lookups by
    rowid measured ~200x slower than an equivalent plain-table query (it's built for KNN
    search, not this access pattern). Rows written before this column existed have NULL
    here until next touched; a miss just means "not reusable", falling back to re-embed.
    """
    out: dict[str, list[float]] = {}
    for text, blob in db.execute("SELECT text, embedding FROM chunks WHERE path=?", (rel,)):
        if blob is None:
            continue
        out[_content_hash(text)] = _deserialize_vec(blob)
    return out


@dataclass
class _FilePlan:
    rel: str
    full_path: Path
    mtime: float
    file_hash: str
    text: str = ""
    pending: list[tuple[str, int, str, str, int, int, int, str]] = field(default_factory=list)
    frontmatter_json: str | None = None
    wikilinks: list[tuple[int, str, str, str]] = field(default_factory=list)


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
        locate_cursor = 0
        for ordi, (heading, hlevel, ctext) in enumerate(
            chunk_markdown(plan.text, config.MAX_CHARS, config.OVERLAP)
        ):
            start_line, end_line = _locate_chunk_lines(lines, ctext, search_from=locate_cursor)
            locate_cursor = start_line
            body_hash = _content_hash(ctext)
            chunk_id = compute_chunk_id(
                f"markdown:{plan.rel}", start_line, end_line, body_hash, config.MODEL_NAME
            )
            plan.pending.append(
                (plan.rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id)
            )
        fm = _parse_frontmatter(plan.text)
        plan.frontmatter_json = json.dumps(fm, default=str) if fm else None
        plan.wikilinks = _extract_wikilinks(plan.text)
        active.append(plan)

    # Load reusable embeddings before deletes.
    reuse: dict[str, dict[str, list[float]]] = {}
    for plan in active:
        reuse[plan.rel] = _vectors_by_content_hash(db, plan.rel)

    for rel in purge_rels:
        _delete_path_by_rel(db, rel)
    for plan in active:
        _delete_path(db, plan.rel)
        if plan.wikilinks:
            db.executemany(
                "INSERT INTO backlinks(source, target_key, target_stem, line, text) VALUES (?,?,?,?,?)",
                [(plan.rel, tk, ts, ln, tx) for ln, tk, ts, tx in plan.wikilinks],
            )
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
        embs = embed(texts_to_embed, verbose=verbose)
        for slot, vec in zip(embed_slots, embs):
            all_vectors[slot] = vec
    elif verbose and active:
        print(f"  reused all chunks for {len(active)} file(s) (no embed)", flush=True)

    if all_pending:
        _insert_pending_chunks(db, all_pending, all_vectors)
    for plan in active:
        db.execute(
            "INSERT OR REPLACE INTO files(path, mtime, hash, frontmatter) VALUES (?,?,?,?)",
            (plan.rel, plan.mtime, plan.file_hash, plan.frontmatter_json),
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
    if folder or exclude:
        # folder/exclude filtering happens after ranking (below) — a small global candidate
        # pool can silently starve a scoped query even when the folder clearly has matches,
        # since KNN/FTS rank across the whole corpus with no knowledge of the scope. Widen
        # the pool so scoped searches draw from (up to) the full index instead of the same
        # top-N used for unscoped queries.
        total_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n = min(total_chunks, max(n, 2000))
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
    if folder or exclude:
        # Fetch every fused candidate's path — a small overfetch margin still starves the
        # filter when only a few of the (now much larger) candidate pool are actually in
        # scope; the whole point of widening `n` above is wasted if we truncate again here.
        fetch_n = len(ranked)
    else:
        # Over-fetch a bit so folder/exclude filters still fill k; one IN query vs N round-trips.
        fetch_n = min(len(ranked), max(k * 4, k + 16))
    ids = ranked[:fetch_n]
    by_id: dict[int, tuple] = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        # Join files.mtime here — one query, versus a filesystem stat() per hit later
        # (callers commonly want "modified" alongside a result; the value is already
        # cached in the index and this costs nothing extra to include).
        for row in db.execute(
            f"""SELECT c.id, c.path, c.heading, c.text, c.chunk_hash, c.heading_level,
                       c.start_line, c.end_line, f.mtime
                FROM chunks c LEFT JOIN files f ON f.path = c.path
                WHERE c.id IN ({placeholders})""",
            ids,
        ):
            by_id[row[0]] = row[1:]

    hits: list[Hit] = []
    for rid in ranked:
        row = by_id.get(rid)
        if row is None:
            continue
        path, heading, text, chunk_hash, hlevel, start_line, end_line, mtime = row
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
                mtime=float(mtime or 0.0),
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


# --------------------------------------------------------------------------- #
# Catalog queries — frontmatter filter, backlinks, recent (index-backed, no vault scan)
# --------------------------------------------------------------------------- #
def filter_notes(where: dict, folder: str = "", limit: int = 20) -> tuple[int, list[tuple[float, str, dict]]]:
    """Deterministic frontmatter query over the cached `files.frontmatter` column.

    Returns (total_matches, top-`limit` matches), each match (mtime, path, frontmatter),
    sorted by mtime desc. No filesystem walk — reads the index only.
    """
    db = connect()
    try:
        rows = db.execute(
            "SELECT path, mtime, frontmatter FROM files WHERE frontmatter IS NOT NULL"
        ).fetchall()
    finally:
        db.close()
    folder_prefix = folder.replace("\\", "/").strip("/")
    matches: list[tuple[float, str, dict]] = []
    for path, mtime, fm_json in rows:
        if folder_prefix and not path.startswith(folder_prefix + "/"):
            continue
        try:
            fm = json.loads(fm_json) if fm_json else {}
        except json.JSONDecodeError:
            fm = {}
        if all(_match_condition(fm.get(k), cond) for k, cond in where.items()):
            matches.append((mtime, path, fm))
    matches.sort(key=lambda t: t[0], reverse=True)
    return len(matches), matches[:limit]


def _escape_like(s: str) -> str:
    """Escape SQLite LIKE wildcards so a literal `_`/`%` in a path segment isn't treated as one."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def recent_notes(limit: int = 10, folder: str = "") -> list[tuple[str, float]]:
    """(path, mtime) for the most recently modified notes, index-backed — no per-file stat()."""
    db = connect()
    try:
        folder_prefix = folder.replace("\\", "/").strip("/")
        if folder_prefix:
            rows = db.execute(
                "SELECT path, mtime FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY mtime DESC LIMIT ?",
                (_escape_like(folder_prefix) + "/%", limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT path, mtime FROM files ORDER BY mtime DESC LIMIT ?", (limit,)
            ).fetchall()
        return rows
    finally:
        db.close()


def list_backlinks(
    target_keys: set[str], exclude_source: str = "", limit: int = 100
) -> list[tuple[str, int, str]]:
    """(source path, line, line text) for notes linking to any of target_keys (stem or full path)."""
    if not target_keys:
        return []
    db = connect()
    try:
        keys = list(target_keys)
        qs = ",".join("?" * len(keys))
        sql = f"""SELECT source, line, text FROM backlinks
                  WHERE (target_key IN ({qs}) OR target_stem IN ({qs}))"""
        params: list = [*keys, *keys]
        if exclude_source:
            sql += " AND source != ?"
            params.append(exclude_source)
        sql += " ORDER BY source, line LIMIT ?"
        params.append(limit)
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


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
