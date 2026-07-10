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
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import sqlite_vec

from . import config

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)")


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def chunk_markdown(text: str, max_chars: int, overlap: int) -> list[tuple[str, str]]:
    """Return [(heading_breadcrumb, chunk_text)] greedily packed to ~max_chars."""
    body = strip_frontmatter(text)
    heading: list[str] = []
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush_block():
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                blocks.append((" › ".join(h for h in heading if h), joined))
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

    chunks: list[tuple[str, str]] = []
    cur_head: str | None = None
    cur: list[str] = []
    cur_len = 0

    def emit():
        nonlocal cur, cur_len, cur_head
        if cur:
            chunks.append((cur_head or "", "\n\n".join(cur).strip()))
        cur, cur_len, cur_head = [], 0, None

    for head, btext in blocks:
        if len(btext) > max_chars:
            emit()
            step = max(1, max_chars - overlap)
            for i in range(0, len(btext), step):
                chunks.append((head, btext[i : i + max_chars]))
            continue
        if cur and cur_len + len(btext) > max_chars:
            emit()
        if not cur:
            cur_head = head
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


def _heading_level(title: str, heading: str) -> int:
    if not heading:
        return 0
    for part in heading.split(" › "):
        if part.strip().lower() == title.strip().lower():
            return heading.count(" › ") + 1
    return heading.count(" › ") + 1 if heading else 0


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
    db = sqlite3.connect(path or config.INDEX_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA journal_mode=WAL")
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
    return db


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
    if config.IGNORE_FILE.exists():
        for line in config.IGNORE_FILE.read_text().splitlines():
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

    db = connect()
    if rebuild:
        db.executescript(
            "DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS vec_chunks; DROP TABLE IF EXISTS chunks_fts;"
        )
        db.execute("DELETE FROM files")
        db.execute("DELETE FROM meta")
        db.commit()
        db.close()
        db = connect()

    ignore = _load_ignore()
    known = {row[0]: (row[1], row[2]) for row in db.execute("SELECT path, mtime, hash FROM files")}
    on_disk: set[str] = set()

    pending: list[tuple[str, int, str, str, int, int, int, str]] = []
    stats = IndexStats()

    notes = list(_iter_notes(root, ignore))
    if limit:
        notes = notes[:limit]

    for p in notes:
        rel = p.relative_to(root).as_posix()
        on_disk.add(rel)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h = _file_hash(text)
        prev = known.get(rel)
        if prev and prev[1] == h:
            continue  # unchanged
        if prev:
            _delete_path(db, rel)
            stats.changed += 1
        else:
            stats.added += 1
        lines = text.split("\n")
        for ordi, (heading, ctext) in enumerate(chunk_markdown(text, config.MAX_CHARS, config.OVERLAP)):
            start_line, end_line = _locate_chunk_lines(lines, ctext)
            hlevel = _heading_level(heading.split(" › ")[-1] if heading else "", heading)
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
            (rel, p.stat().st_mtime, h),
        )

    if limit is None:
        for rel in list(known):
            if rel not in on_disk:
                _delete_path(db, rel)
                db.execute("DELETE FROM files WHERE path=?", (rel,))
                stats.removed += 1

    if pending:
        if verbose:
            print(
                f"  embedding {len(pending)} chunks via {config.EMBED_BACKEND}:{config.MODEL_NAME} ...",
                flush=True,
            )
        vectors = embed([t[3] for t in pending])
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
        stats.chunks = len(pending)

    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_ready','1')")
    db.commit()
    db.close()
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


def index_file(full_path: Path, verbose: bool = False) -> int:
    """Reindex one note by absolute path. Returns chunk count embedded."""
    root = config.NOTES_ROOT
    full_path = full_path.resolve()
    try:
        rel = full_path.relative_to(root).as_posix()
    except ValueError as e:
        raise ValueError(f"path outside vault root: {full_path}") from e
    if not full_path.is_file():
        _delete_path_by_rel(connect(), rel)
        return 0
    text = full_path.read_text(encoding="utf-8", errors="replace")
    db = connect()
    _delete_path(db, rel)
    pending: list[tuple] = []
    lines = text.split("\n")
    for ordi, (heading, ctext) in enumerate(chunk_markdown(text, config.MAX_CHARS, config.OVERLAP)):
        start_line, end_line = _locate_chunk_lines(lines, ctext)
        hlevel = _heading_level(heading.split(" › ")[-1] if heading else "", heading)
        chash = _content_hash(ctext)
        chunk_id = compute_chunk_id(
            f"markdown:{rel}", start_line, end_line, chash, config.MODEL_NAME
        )
        pending.append((rel, ordi, heading, ctext, start_line, end_line, hlevel, chunk_id))
    if pending:
        if verbose:
            print(f"  embedding {len(pending)} chunks for {rel} ...", flush=True)
        vectors = embed([t[3] for t in pending])
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
    db.execute(
        "INSERT OR REPLACE INTO files(path, mtime, hash) VALUES (?,?,?)",
        (rel, full_path.stat().st_mtime, _file_hash(text)),
    )
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_ready','1')")
    db.commit()
    db.close()
    return len(pending)


def _delete_path_by_rel(db: sqlite3.Connection, rel: str) -> None:
    _delete_path(db, rel)
    db.execute("DELETE FROM files WHERE path=?", (rel,))


def purge_source(full_path: Path) -> bool:
    try:
        rel = full_path.resolve().relative_to(config.NOTES_ROOT).as_posix()
    except ValueError:
        return False
    db = connect()
    _delete_path_by_rel(db, rel)
    db.commit()
    db.close()
    return True


def search(
    query: str,
    k: int = 8,
    exclude: list[str] | None = None,
    folder: str = "",
    hybrid: bool = True,
) -> list[Hit]:
    db = connect()
    if db.execute("SELECT value FROM meta WHERE key='dim'").fetchone() is None:
        db.close()
        raise SystemExit("Index is empty — run `apo-engine index` first.")

    n = max(k * 6, 50)  # candidate pool per retriever
    fused: dict[int, float] = {}

    # --- dense: vector KNN ---
    qvec = embed([query])[0]
    vrows = db.execute(
        "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(qvec), n),
    ).fetchall()
    cosine = {rid: 1.0 - dist / 2.0 for rid, dist in vrows}
    for rank, (rid, _) in enumerate(vrows):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)

    # --- lexical: FTS5 BM25 ---
    if hybrid:
        ensure_fts(db)
        match = _fts_query(query)
        if match:
            try:
                frows = db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, n),
                ).fetchall()
            except sqlite3.OperationalError:
                frows = []
            for rank, (rid,) in enumerate(frows):
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)

    if not fused:
        db.close()
        return []
    top = max(fused.values())

    hits: list[Hit] = []
    folder_prefix = folder.replace("\\", "/").strip("/")
    for rid in sorted(fused, key=lambda i: fused[i], reverse=True):
        row = db.execute(
            """SELECT path, heading, text, chunk_hash, heading_level, start_line, end_line
               FROM chunks WHERE id = ?""",
            (rid,),
        ).fetchone()
        if row is None:
            continue
        path, heading, text, chunk_hash, hlevel, start_line, end_line = row
        if folder_prefix and not path.startswith(folder_prefix):
            continue
        if exclude and any(fnmatch.fnmatch(path, pat) for pat in exclude):
            continue
        score = cosine.get(rid, fused[rid] / top)
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
