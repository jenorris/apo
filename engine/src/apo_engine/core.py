"""Core: chunk markdown, embed, build a sqlite-vec index, and search it.

No server, no daemon. One sqlite file holds notes metadata + vectors.
Embeddings come from Ollama (Metal/GPU) by default, or fastembed (ONNX) when configured.
"""
from __future__ import annotations

import fnmatch
import os
import hashlib
import json
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
from typing import Any, Iterator
import heapq

import sqlite_vec
import yaml

from . import config
from . import rerank
from . import table_markdown
from . import vaults

# Query-embedding LRU (identical agent searches within TTL skip Ollama).
_query_embed_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
_query_embed_lock = threading.Lock()
# Reused across search() calls — avoid ThreadPoolExecutor create/teardown per query.
# >1 so concurrent MCP searches (each already on a worker thread) can embed in parallel.
_search_pool = ThreadPoolExecutor(max_workers=4)

# Schema bootstrap once per index path per process.
_schema_ready: set[str] = set()
# Process-local: skip meta check after the first ensure for this index path.
_hash_algo_ready: set[str] = set()
# Sole index-writer connection (watch / CLI index) — reuse across commits.
_writer_local = threading.local()
# Cached read-only connection per thread — reused across search/filter_notes/etc. calls.
_reader_local = threading.local()

# Content identity for files.hash / chunks.content_hash. blake2b is stdlib-only and
# substantially faster than SHA-256 on large notes; digest sizes keep hex widths stable
# (64-char file hash, 16-char content hash) so columns and logs stay comparable.
HASH_ALGO = "blake2b"

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_FRONTMATTER_YAML = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)")
_WIKILINK = re.compile(r"\[\[([^\]#|]+)(?:[#|][^\]]*)?\]\]")
_FM_KEY_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# How many embeddings to commit per batch during vault index (matches Ollama batch).
_EMBED_COMMIT_BATCH = 64
# Exclude-only searches: widen KNN pool without scanning the whole corpus.
_EXCLUDE_CANDIDATE_FLOOR = 500


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def _body_start_line(text: str) -> tuple[str, int]:
    """Return (body_text, 1-based line number of the first body line)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return text, 1
    fm = m.group(0)
    return text[len(fm) :], fm.count("\n") + 1


def _slice_lines(text: str, start_char: int, end_char: int, base_line: int) -> tuple[int, int]:
    """Inclusive 1-based line span for ``text[start_char:end_char]`` starting at ``base_line``."""
    if end_char <= start_char:
        line = base_line + text[:start_char].count("\n")
        return line, line
    start = base_line + text[:start_char].count("\n")
    end = base_line + text[: max(start_char, end_char - 1)].count("\n")
    return start, end


def section_markdown(text: str) -> list[tuple[str, int, str, int, int]]:
    """Return [(heading_breadcrumb, level, section_text, start_line, end_line)] — one row per section.

    Each markdown heading starts a section that runs until the next heading of the same
    or higher level. ``level`` 0 = preamble before the first heading. Boundaries come from
    the shared :mod:`apo_engine.markdown_sections` splitter so an index span, a read span,
    and a patch span all resolve to the same lines.
    """
    from .markdown_sections import BREADCRUMB_SEP, split_sections

    body, body_line = _body_start_line(text)
    if not body.strip():
        return []
    lines = body.split("\n")

    sections: list[tuple[str, int, str, int, int]] = []
    for span in split_sections(lines):
        start_idx = span.heading_line if span.heading_line >= 0 else span.body_start
        section_text = "\n".join(lines[start_idx : span.body_end]).strip()
        if not section_text:
            continue
        breadcrumb = BREADCRUMB_SEP.join(span.breadcrumb)
        start_line = body_line + start_idx
        end_line = body_line + span.body_end - 1 if span.body_end > start_idx else start_line
        sections.append((breadcrumb, span.level, section_text, start_line, end_line))
    return sections




def chunk_markdown(
    text: str, max_chars: int = 0, overlap: int = 0
) -> list[tuple[str, int, str, int, int]]:
    """Deprecated alias — markdown notes are section-indexed; max_chars/overlap ignored."""
    del max_chars, overlap
    return section_markdown(text)


def _note_title(text: str, rel: str) -> str:
    """Breadcrumb root for row embeds: frontmatter title, else filename stem."""
    fm = note_frontmatter(text, rel) or {}
    title = fm.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    stem = rel.rsplit("/", 1)[-1]
    for suf in (".md", ".markdown"):
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            break
    return stem.replace("-", " ").replace("_", " ").strip()


def _markdown_chunk_rows(text: str, rel: str) -> list[tuple]:
    """Produce index rows for a markdown note: prose sections + table header/rows.

    Returns 11-field pending tuples
    ``(rel, ord, breadcrumb, text, start_line, end_line, level, chunk_id, content_hash,
    section_bytes, meta)`` where ``meta`` is ``None`` for prose sections or a dict with
    ``chunk_kind`` / table identity for ``table_header`` / ``table_row`` chunks.

    Each table stays inside its owning section chunk (prose recall unchanged) *and*
    additionally emits one ``table_header`` chunk plus one ``table_row`` chunk per data
    row so natural-language queries retrieve the specific row. Tables are never split
    mid-table.
    """
    from .markdown_sections import BREADCRUMB_SEP
    from . import table_markdown as tm

    id_prefix = f"markdown:{rel}"
    model = config.MODEL_NAME
    note_title = _note_title(text, rel)

    sections = section_markdown(text)
    rows: list[tuple] = []
    ord_counter = 0

    # Map absolute line → owning section (breadcrumb + chunk_id) for table parenting.
    section_spans: list[tuple[int, int, str, str]] = []  # (start, end, breadcrumb, chunk_id)
    for breadcrumb, level, ctext, start_line, end_line in sections:
        chash = _content_hash(ctext)
        chunk_id = compute_chunk_id(id_prefix, start_line, end_line, chash, model)
        sec_bytes = len(ctext.encode("utf-8"))
        rows.append(
            (rel, ord_counter, breadcrumb, ctext, start_line, end_line, level, chunk_id, chash, sec_bytes, None)
        )
        ord_counter += 1
        section_spans.append((start_line, end_line, breadcrumb, chunk_id))

    body, body_line = _body_start_line(text)
    if not body.strip():
        return rows
    body_lines = body.split("\n")
    tables = tm.find_tables(body_lines)
    # Optional vault table-contract key_column (first matching path glob wins).
    key_column: str | None = None
    try:
        from .table_contract import key_column_for

        key_column = key_column_for(vaults.notes_root(), rel)
    except Exception:
        key_column = None
    for t in tables:
        abs_start = body_line + t.start_line  # 1-based file line of header row
        parent_breadcrumb = ""
        parent_chunk_hash = ""
        for s_start, s_end, bc, cid in section_spans:
            if s_start <= abs_start <= s_end:
                parent_breadcrumb = bc
                parent_chunk_hash = cid
        crumbs = [note_title] + [c for c in parent_breadcrumb.split(BREADCRUMB_SEP) if c]
        table_id = tm.table_id_for(rel, abs_start, t.table_index)
        schema_hash = tm.table_schema_hash(t.headers)
        level = 0
        # Header chunk — schema recall.
        htext = tm.header_flatten_text(crumbs, t.headers)
        hhash = _content_hash(htext)
        hid = compute_chunk_id(id_prefix, abs_start, abs_start + 1, hhash, model)
        rows.append(
            (
                rel, ord_counter, parent_breadcrumb, htext, abs_start, abs_start + 1, level,
                hid, hhash, len(htext.encode("utf-8")),
                {
                    "chunk_kind": "table_header",
                    "parent_chunk_hash": parent_chunk_hash,
                    "table_id": table_id,
                    "table_schema_hash": schema_hash,
                    "row_index": None,
                    "row_key": None,
                },
            )
        )
        ord_counter += 1
        # One chunk per data row.
        first_data_line = abs_start + 2  # header + delimiter
        for ri, row in enumerate(t.rows):
            rtext = tm.row_flatten_text(crumbs, t.headers, row)
            rhash = _content_hash(rtext)
            row_line = first_data_line + ri
            rid = compute_chunk_id(id_prefix, row_line, row_line, rhash, model)
            row_key = tm.row_key_for(t, ri, key_column=key_column)
            rows.append(
                (
                    rel, ord_counter, parent_breadcrumb, rtext, row_line, row_line, level,
                    rid, rhash, len(rtext.encode("utf-8")),
                    {
                        "chunk_kind": "table_row",
                        "parent_chunk_hash": parent_chunk_hash,
                        "table_id": table_id,
                        "table_schema_hash": schema_hash,
                        "row_index": ri,
                        "row_key": row_key,
                    },
                )
            )
            ord_counter += 1
    # Fenced ```mermaid blocks — parallel chunks (table analog).
    from . import mermaid_markdown as mm
    from . import mermaid_index as mi

    fences = mm.find_mermaid_fences(body_lines)
    for fence in fences:
        abs_start = body_line + fence.start_line + 1  # first content line (1-based)
        parent_breadcrumb = ""
        parent_chunk_hash = ""
        for s_start, s_end, bc, cid in section_spans:
            if s_start <= abs_start <= s_end:
                parent_breadcrumb = bc
                parent_chunk_hash = cid
        crumbs = [note_title] + [c for c in parent_breadcrumb.split(BREADCRUMB_SEP) if c]
        mermaid_rows, ord_counter = mi.chunk_mermaid_rows(
            fence.text,
            rel,
            breadcrumb=crumbs,
            block_index=fence.block_index,
            abs_start_line=abs_start,
            parent_chunk_hash=parent_chunk_hash,
            id_prefix=id_prefix,
            model_name=model,
            ord_start=ord_counter,
        )
        rows.extend(mermaid_rows)
    return rows


def _mmd_chunk_rows(text: str, rel: str, mtime: float | None = None) -> list[tuple]:
    """Index rows for a standalone ``.mmd`` diagram file."""
    from . import mermaid_index as mi

    id_prefix = f"mmd:{rel}"
    model = config.MODEL_NAME
    fm = mi.merge_frontmatter_for_mmd(text, rel, mtime=mtime)
    title = str(fm.get("title") or rel.split("/")[-1].replace(".mmd", "").replace("-", " ").title())
    rows, _ = mi.chunk_mermaid_rows(
        text,
        rel,
        breadcrumb=[title],
        abs_start_line=1,
        id_prefix=id_prefix,
        model_name=model,
        ord_start=0,
    )
    return rows


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


_search_degraded: ContextVar[str | None] = ContextVar("apo_search_degraded", default=None)
_search_rerank: ContextVar[dict | None] = ContextVar("apo_search_rerank", default=None)


def last_search_degraded() -> str | None:
    """Reason the most recent search() in this context ran degraded, or None."""
    return _search_degraded.get()


def last_search_rerank() -> dict | None:
    """Rerank status for the most recent search() in this context, or None."""
    return _search_rerank.get()


def _normalize_query(query: str) -> str:
    return " ".join(query.split())


def query_embed(query: str) -> list[float]:
    """Embed a search query with a short TTL cache for repeated agent lookups.

    Applies ``config.QUERY_PREFIX`` (asymmetric BGE query instruction) on the query
    side only — the passage/index path in :func:`embed` is never prefixed, so toggling
    the prefix needs no reindex. Cache key is the raw (unprefixed) normalized query.
    """
    key = _normalize_query(query)
    ttl = config.QUERY_EMBED_TTL
    now = time.monotonic()
    if ttl > 0 and key:
        with _query_embed_lock:
            hit = _query_embed_cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                _query_embed_cache.move_to_end(key)
                return hit[1]
    prefix = getattr(config, "QUERY_PREFIX", "") or ""
    vec = embed([prefix + query if prefix else query])[0]
    if ttl > 0 and key and vec is not None:
        with _query_embed_lock:
            _query_embed_cache[key] = (now, vec)
            _query_embed_cache.move_to_end(key)
            while len(_query_embed_cache) > config.QUERY_EMBED_CACHE_SIZE:
                _query_embed_cache.popitem(last=False)
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
    """16-hex-char body hash (blake2b-64) — embed reuse key, not a security digest."""
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).hexdigest()


def _construct_yaml_timestamp(loader: yaml.SafeLoader, node: yaml.Node):
    """Parse YAML timestamps; keep invalid date-like scalars as strings.

    PyYAML matches ``YYYY-MM-DD`` (and friends) as timestamps before validating
    month/day. Bad vault values like ``2017-00-00`` then raise ``ValueError``
    from ``datetime.date`` — which is *not* a ``YAMLError``, so a bare
    ``safe_load`` can crash the watcher. Fall back to the original scalar.
    """
    try:
        return yaml.constructor.SafeConstructor.construct_yaml_timestamp(loader, node)
    except (ValueError, OverflowError):
        return loader.construct_scalar(node)


class _FrontmatterLoader(yaml.SafeLoader):
    """SafeLoader that tolerates invalid YAML 1.1 timestamp scalars."""


_FrontmatterLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    _construct_yaml_timestamp,
)


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_YAML.match(text)
    if not m:
        return {}
    try:
        data = yaml.load(m.group(1), Loader=_FrontmatterLoader)
    except (yaml.YAMLError, ValueError, OverflowError):
        return {}
    return data if isinstance(data, dict) else {}


def note_frontmatter(text: str, path: str | Path | None = None) -> dict | None:
    """Catalog fields for a note.

    Markdown: fenced YAML frontmatter, or ``None`` when absent.
    YAML note (``path`` ends in ``.yaml``/``.yml``): whole-file mapping.
    """
    if path is not None:
        from apo_engine.note_format import is_yaml_note, parse_yaml_document

        if is_yaml_note(path):
            return parse_yaml_document(text)
    if not _FRONTMATTER_YAML.match(text):
        return None
    return _parse_frontmatter(text)


def note_catalog_json(text: str, rel: str, mtime: float | None = None) -> str | None:
    """JSON for ``files.frontmatter``. Empty mapping stores ``{}`` (not NULL)."""
    from apo_engine.note_format import is_mmd_note, is_yaml_note, parse_yaml_document

    if is_mmd_note(rel):
        from apo_engine.mermaid_index import merge_frontmatter_for_mmd

        return json.dumps(merge_frontmatter_for_mmd(text, rel, mtime=mtime), default=str)
    if is_yaml_note(rel):
        fm = parse_yaml_document(text)
        if fm is None:
            return None
        return json.dumps(fm, default=str)
    if not _FRONTMATTER_YAML.match(text):
        return None
    fm = _parse_frontmatter(text)
    # Include empty fenced frontmatter so filter_notes can see the note.
    return json.dumps(fm, default=str)


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
        elif op == "$in":
            if not isinstance(rhs, (list, tuple)) or not rhs:
                return False
            if isinstance(value, list):
                if not any(_loose_eq(x, y) for x in value for y in rhs):
                    return False
            elif not any(_loose_eq(value, y) for y in rhs):
                return False
        elif op == "$elemMatch":
            # At least one list dict element satisfies all inner predicates (AND).
            if not isinstance(value, list) or not isinstance(rhs, dict) or not rhs:
                return False
            if not any(
                isinstance(el, dict)
                and all(_match_condition(el.get(k), c) for k, c in rhs.items())
                for el in value
            ):
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


def _match_where_clause(fm: dict, key: str, cond) -> bool:
    """Match one ``where`` clause; dotted / ``[id=…]`` keys expand via fm_path."""
    from apo_engine.fm_path import path_needs_python_match, resolve_values

    if path_needs_python_match(key):
        values = resolve_values(fm, key)
        if isinstance(cond, dict) and set(cond) == {"$exists"}:
            exists = bool(values)
            return bool(cond["$exists"]) == exists
        if not values:
            return False
        return any(_match_condition(v, cond) for v in values)
    return _match_condition(fm.get(key), cond)


def _ensure_files_columns(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(files)").fetchall()}
    if "frontmatter" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN frontmatter TEXT")
    if "bytes" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN bytes INTEGER")


def _ensure_ref_catalog_tables(db: sqlite3.Connection) -> None:
    """Frontmatter-only cache for ``filter_notes(ref=)`` (see ``git_catalog``)."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ref_trees (
            tree_oid TEXT PRIMARY KEY,
            commit_oid TEXT NOT NULL,
            built_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ref_files (
            tree_oid TEXT NOT NULL,
            path TEXT NOT NULL,
            mtime REAL NOT NULL,
            frontmatter TEXT,
            PRIMARY KEY (tree_oid, path)
        );
        CREATE INDEX IF NOT EXISTS ref_files_tree ON ref_files(tree_oid);
        CREATE VIRTUAL TABLE IF NOT EXISTS ref_fts USING fts5(
            tree_oid UNINDEXED,
            path UNINDEXED,
            mtime UNINDEXED,
            text
        );
        """
    )


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
        # rows) — it's built for KNN search, not this access pattern. Existing rows backfill
        # lazily (NULL until next touch); _vectors_by_content_hash treats a miss as "not
        # reusable" and falls back to re-embedding, so this is safe without a forced rebuild.
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


def connect(path: Path | None = None) -> sqlite3.Connection:
    index = Path(path or vaults.index_path()).resolve()
    key = str(index)
    db = sqlite3.connect(str(index), timeout=config.DB_TIMEOUT)
    if not hasattr(db, "enable_load_extension"):
        raise RuntimeError(
            "This Python's sqlite3 cannot load extensions (needed for sqlite-vec). "
            "On macOS use Homebrew Python (`brew install python`), not the system/"
            "python.org build — see https://alexgarcia.xyz/sqlite-vec/python.html"
        )
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    # busy_timeout is per-connection (must be set every time); journal_mode is a persistent
    # property of the database file itself — setting it on an already-WAL file still forces
    # SQLite to open/verify the -wal file each time (measured ~0.28ms), pure overhead paid on
    # every read-only connect() (search/filter_notes/recent_notes/... open one per call).
    # Only need to assert it once per process, same lifetime as the schema-bootstrap check.
    db.execute(f"PRAGMA busy_timeout={int(config.DB_TIMEOUT * 1000)}")
    if key not in _schema_ready:
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
        _ensure_ref_catalog_tables(db)
        _schema_ready.add(key)
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
                pass
        else:
            return db
    db = connect()
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


def _fts_query(query: str) -> str | None:
    """Turn a natural-language query into a safe FTS5 MATCH string.

    Short queries (≤4 terms) use AND for precision; longer ones keep OR for recall
    (agent searches are often multi-keyword phrases that would under-match with AND).
    """
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1][:24]
    if not terms:
        return None
    # ≤2 terms: AND (precise agent lookups). Longer: OR (multi-keyword recall).
    joined = (
        " AND ".join(f'"{t}"' for t in terms)
        if len(terms) <= 2
        else " OR ".join(f'"{t}"' for t in terms)
    )
    return joined


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
        "SELECT id, _apo_index_text(text, COALESCE(chunk_kind, 'section')) FROM chunks"
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
    start_id = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM chunks").fetchone()[0])
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
        fts_rows.append((rid, _index_text_for_embedding(ctext, (meta or {}).get("chunk_kind") or "section")))
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


def _finalize_index_writes(db: sqlite3.Connection) -> None:
    ensure_fts(db)
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_ready','1')")
    db.commit()


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
    if verbose:
        print(
            f"  embedding {total} chunks via {config.EMBED_BACKEND}:{config.MODEL_NAME} ...",
            flush=True,
        )
    # Embed everything first so we know which paths failed before writing any chunks.
    all_vectors: list[list[float] | None] = []
    batch = _EMBED_COMMIT_BATCH
    for i in range(0, total, batch):
        part = pending[i : i + batch]
        all_vectors.extend(embed([_pending_index_text(t) for t in part], verbose=False))
        if verbose:
            print(f"  … embedded {min(i + batch, total)}/{total}", flush=True)

    dropped = {row[0] for row, vec in zip(pending, all_vectors) if vec is None}
    stored = 0
    for i in range(0, total, batch):
        part = pending[i : i + batch]
        part_v = all_vectors[i : i + batch]
        # Skip every chunk for a dropped path so we never leave a partial note indexed.
        filtered_p: list = []
        filtered_v: list = []
        for row, vec in zip(part, part_v):
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
        print(
            f"  WARNING: {len(dropped)} file(s) left unstamped after embed drop — "
            "will retry on next index (not a permanent mtime-skip)",
            flush=True,
        )
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


def _compile_ignore(patterns: list[str]) -> list[re.Pattern[str]]:
    """Precompile ignore globs once per index walk (fnmatch per file is O(patterns))."""
    return [re.compile(fnmatch.translate(p)) for p in patterns]


def _is_ignored(rel: str, ignore_res: list[re.Pattern[str]]) -> bool:
    return any(r.fullmatch(rel) is not None for r in ignore_res)


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


def _file_hash(text: str) -> str:
    """Full-file content identity (blake2b-256 hex) stored in ``files.hash``."""
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=32).hexdigest()


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


@dataclass
class IndexStats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    chunks: int = 0
    seconds: float = 0.0


def index_vault(rebuild: bool = False, limit: int | None = None, verbose: bool = True) -> IndexStats:
    t0 = time.time()
    root = vaults.notes_root()
    if not root.exists():
        raise SystemExit(f"NOTES_ROOT does not exist: {root}")

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
        if prev:
            _delete_path(db, rel)
            # Drop the catalog row so a failed embed cannot mtime-skip on the old stamp.
            db.execute("DELETE FROM files WHERE path=?", (rel,))
            stats.changed += 1
        else:
            stats.added += 1
        from apo_engine.note_format import chunk_yaml_note, is_mmd_note, is_yaml_note, parse_yaml_document

        if is_yaml_note(rel):
            fm = parse_yaml_document(text) or {}
            id_prefix = f"yaml:{rel}"
            fm_json = json.dumps(fm, default=str)
            wikilinks: list = []
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
            wikilinks = []
            note_rows = _mmd_chunk_rows(text, rel, mtime=st.st_mtime)
        else:
            fm_json = note_catalog_json(text, rel)
            wikilinks = _extract_wikilinks(text)
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
        if wikilinks:
            db.executemany(
                "INSERT INTO backlinks(source, target_key, target_stem, line, text) VALUES (?,?,?,?,?)",
                [(rel, tk, ts, ln, tx) for ln, tk, ts, tx in wikilinks],
            )
        file_stamps.append((rel, st.st_mtime, h, fm_json, st.st_size))

    if limit is None:
        for rel in list(known):
            if rel not in on_disk:
                _delete_path(db, rel)
                db.execute("DELETE FROM files WHERE path=?", (rel,))
                stats.removed += 1

    work_done = bool(pending) or stats.removed > 0 or stats.added > 0 or stats.changed > 0
    if work_done or mtime_refreshed:
        db.commit()

    dropped: set[str] = set()
    if pending:
        stats.chunks, dropped = _embed_and_store_pending(db, pending, verbose=verbose)
    stamped = 0
    for rel, mtime, h, fm_json, file_bytes in file_stamps:
        if rel in dropped:
            # Undo added/changed counts for notes we could not finish indexing.
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
    if stamped or dropped or work_done or mtime_refreshed:
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
    file_bytes: int = 0
    section_bytes: int = 0
    content_hash: str = ""
    chunk_kind: str = "section"
    row_key: str = ""
    table_id: str = ""


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
    bounded heap (no full-list sort).
    """
    scored: list[tuple[float, int]] = []

    if prefer_ids:
        ph = ",".join("?" * len(prefer_ids))
        for rid, blob in db.execute(
            f"SELECT id, embedding FROM chunks WHERE id IN ({ph}) AND embedding IS NOT NULL",
            prefer_ids,
        ):
            scored.append((_l2_sq_blob(qvec, blob), rid))

    max_full = int(getattr(config, "SCOPED_VECTOR_FULL_SCAN_MAX", 500))
    skip_full = bool(prefer_ids) and len(prefer_ids) >= n
    if not skip_full:
        # Cheap count: if folder is huge and we already have FTS prefs, stay prefiltered.
        folder_like = _escape_like(folder_prefix) + "/%"
        if prefer_ids:
            cnt = db.execute(
                "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL AND path LIKE ? ESCAPE '\\'",
                (folder_like,),
            ).fetchone()[0]
            if cnt > max_full:
                skip_full = True

    if not skip_full:
        seen = {rid for _, rid in scored}
        rows = db.execute(
            """SELECT id, embedding FROM chunks
               WHERE embedding IS NOT NULL AND path LIKE ? ESCAPE '\\'""",
            (_escape_like(folder_prefix) + "/%",),
        )
        heap: list[tuple[float, int]] = []
        for rid, blob in rows:
            if rid in seen:
                continue
            dist = _l2_sq_blob(qvec, blob)
            if len(heap) < n:
                heapq.heappush(heap, (-dist, rid))  # max-heap via negation
            elif dist < -heap[0][0]:
                heapq.heapreplace(heap, (-dist, rid))
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
    root = vaults.notes_root()
    db = writer_connect()
    candidates: list[tuple[str, Path, float]] = []  # rel, path, mtime
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
            plan.wikilinks = []
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
            plan.wikilinks = []
            plan.pending.extend(_mmd_chunk_rows(text, rel, mtime=st_mtime))
        else:
            plan.frontmatter_json = note_catalog_json(text, rel)
            plan.wikilinks = _extract_wikilinks(text)
            plan.pending.extend(_markdown_chunk_rows(text, rel))
        plans.append(plan)

    active = plans

    # Load reusable embeddings before deletes.
    reuse: dict[str, dict[str, list[float]]] = {}
    for plan in active:
        reuse[plan.rel] = _vectors_by_content_hash(db, plan.rel)

    for rel in purge_rels:
        _delete_path_by_rel(db, rel)
    for plan in active:
        _delete_path(db, plan.rel)
        # Drop catalog row until embed succeeds — avoids permanent mtime-skip on drop.
        db.execute("DELETE FROM files WHERE path=?", (plan.rel,))
        if plan.wikilinks:
            db.executemany(
                "INSERT INTO backlinks(source, target_key, target_stem, line, text) VALUES (?,?,?,?,?)",
                [(plan.rel, tk, ts, ln, tx) for ln, tk, ts, tx in plan.wikilinks],
            )
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
        for i in range(0, len(all_pending), _EMBED_COMMIT_BATCH):
            part_p = all_pending[i : i + _EMBED_COMMIT_BATCH]
            part_v = all_vectors[i : i + _EMBED_COMMIT_BATCH]
            part_o = pending_owner[i : i + _EMBED_COMMIT_BATCH]
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
                    f"  … stored {min(i + _EMBED_COMMIT_BATCH, len(all_pending))}/{len(all_pending)} chunks",
                    flush=True,
                )
    stamped = 0
    for plan in active:
        if plan.rel in dropped:
            continue
        db.execute(
            "INSERT OR REPLACE INTO files(path, mtime, hash, frontmatter, bytes) VALUES (?,?,?,?,?)",
            (plan.rel, plan.mtime, plan.file_hash, plan.frontmatter_json, plan.file_bytes),
        )
        stamped += 1
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
    db.commit()
    return True


def _catalog_retrieval_boost(path: str, chunk_kind: str, folder_prefix: str) -> float:
    """Post-fusion score multiplier for mermaid catalog diagram recall."""
    if not folder_prefix or "mermaid-catalog" not in folder_prefix.replace("\\", "/"):
        return 1.0
    norm = path.replace("\\", "/")
    boost = 1.0
    if norm.endswith("/diagram.mmd") or norm.endswith("diagram.mmd"):
        boost *= 1.18
    if chunk_kind in ("mermaid_file", "mermaid_header", "mermaid_node", "mermaid_edge"):
        boost *= 1.10
    if "/pages/" in norm:
        boost *= 0.82
    if chunk_kind == "table_row" and "/pages/" in norm:
        boost *= 0.75
    return boost


def _compile_excludes(exclude: list[str] | None) -> tuple[list[str], list[re.Pattern[str]]]:
    """Split exclude globs into path-prefix checks vs compiled fullmatch patterns.

    Patterns like ``projects/*`` become a ``projects/`` prefix (startswith).
    """
    prefixes: list[str] = []
    globs: list[re.Pattern[str]] = []
    for raw in exclude or []:
        pat = raw.replace("\\", "/").strip()
        if not pat:
            continue
        if pat.endswith("/*") and not any(c in pat[:-2] for c in "*?["):
            prefixes.append(pat[:-1])
        else:
            globs.append(re.compile(fnmatch.translate(pat)))
    return prefixes, globs


def _path_excluded(path: str, prefixes: list[str], globs: list[re.Pattern[str]]) -> bool:
    for pref in prefixes:
        if path.startswith(pref):
            return True
    return any(g.fullmatch(path) is not None for g in globs)


# --------------------------------------------------------------------------- #
# Snippet construction — signal-density-per-character compression, not
# retrieval. Reranking (below) always scores full_texts; snippet_chars only
# shapes what a Hit shows the caller. table_row/table_header text is already
# flattened label:value text (see table_markdown.row_flatten_text /
# header_flatten_text) and never worth truncating further. "section" chunk
# text is raw markdown source, so it pays syntax overhead (table separator
# rows, heading marks, bold/list/quote syntax) that buys the caller nothing.
# --------------------------------------------------------------------------- #
_SEP_ROW_RE = re.compile(r"^[ \t]*\|?[ \t:|-]*-[ \t:|-]*\|?[ \t]*$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)
_MD_LIST_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_MD_QUOTE_RE = re.compile(r"^>[ \t]?", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WS_RE = re.compile(r"\s")

_FTS_SNIPPET_MAX_TOKENS = 24  # sqlite fts5 snippet() caps at 64; keep it modest


def _collapse_full_tables(text: str) -> str:
    """Replace each embedded GFM table with a one-line schema marker.

    A table's rows are independently indexed as table_row/table_header
    chunks (already optimally flattened) — showing raw pipe/dash syntax in a
    section snippet is pure redundancy at best, budget-eating noise at worst.
    """
    lines = text.split("\n")
    try:
        tables = table_markdown.find_tables(lines)
    except Exception:
        return text
    if not tables:
        return text
    out = list(lines)
    for t in sorted(tables, key=lambda tbl: tbl.start_line, reverse=True):
        headers = ", ".join(h.strip() for h in t.headers if h.strip())
        plural = "" if len(t.rows) == 1 else "s"
        marker = f"[table: {len(t.rows)} row{plural} — {headers}]"
        out[t.start_line : t.end_line + 1] = [marker]
    return "\n".join(out)


def _strip_markdown_decoration(text: str) -> str:
    """Drop prose-only markdown syntax that costs snippet budget for no signal.

    Never applied to table cell text — column context is load-bearing there
    (bare numerics embed/read weakly; see table_markdown.row_flatten_text).
    Tables are collapsed to markers by _collapse_full_tables before this runs.
    """
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_LIST_RE.sub(r"\1", text)
    text = _MD_QUOTE_RE.sub("", text)
    return text


def _truncate_word_boundary(text: str, limit: int) -> str:
    """Cut at the last whitespace at-or-before limit — never mid-word."""
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    last_ws = None
    for last_ws in _WS_RE.finditer(window):
        pass
    if last_ws is not None:
        return window[: last_ws.start()].rstrip()
    return window.rstrip()


def _fts_snippet_excerpt(db: sqlite3.Connection, match: str, rid: int) -> str | None:
    """Query-anchored excerpt via FTS5's built-in snippet() — no reindex needed,
    chunks_fts already stores the raw text. Only meaningful for hits that
    actually matched the keyword side of hybrid search; vector-only hits have
    no term position to anchor around."""
    try:
        row = db.execute(
            "SELECT snippet(chunks_fts, 0, '', '', ' … ', ?) "
            "FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid = ?",
            (_FTS_SNIPPET_MAX_TOKENS, match, rid),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def _index_text_for_embedding(ctext: str, chunk_kind: str) -> str:
    """Cleaned text fed to the embedder and chunks_fts — never chunks.text itself.

    chunks.text stays byte-for-byte the raw section (read_note(chunk_hash=)
    returns it verbatim, and expected_content_hash preconditions are derived
    from the same live-file bytes), so this must never touch that column.
    Only the embed/FTS inputs benefit from stripping the markdown syntax tax
    (table separator rows, bold/list/quote/link decoration) that dilutes the
    vector and pads FTS snippet() windows with punctuation. table_row/header
    text is already flattened and dense — nothing to clean there.
    """
    if chunk_kind not in ("section", "", None):
        return ctext
    cleaned = _collapse_full_tables(ctext)
    cleaned = _SEP_ROW_RE.sub("", cleaned)
    cleaned = _strip_markdown_decoration(cleaned)
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned).strip()
    return cleaned or ctext  # never hand the embedder/FTS an empty string


def _pending_chunk_kind(row: "PendingChunk | tuple") -> str:
    meta = row[10] if len(row) > 10 else None
    meta = meta if isinstance(meta, dict) else None
    return (meta or {}).get("chunk_kind") or "section"


def _pending_index_text(row: "PendingChunk | tuple") -> str:
    return _index_text_for_embedding(row[3], _pending_chunk_kind(row))


def _build_snippet(
    text: str,
    snippet_chars: int,
    chunk_kind: str,
    *,
    db: sqlite3.Connection | None = None,
    match: str | None = None,
    rid: int | None = None,
    fts_rowids: set[int] | None = None,
) -> str:
    if snippet_chars <= 0:
        return text
    if chunk_kind in ("table_row", "table_header", "mermaid_file", "mermaid_header", "mermaid_node", "mermaid_edge"):
        return text
    excerpt = None
    if db is not None and match and fts_rowids and rid in fts_rowids:
        excerpt = _fts_snippet_excerpt(db, match, rid)
    source = excerpt if excerpt else _collapse_full_tables(text)
    cleaned = _SEP_ROW_RE.sub("", source)
    cleaned = _strip_markdown_decoration(cleaned)
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned).strip()
    return _truncate_word_boundary(cleaned, snippet_chars)


def search(
    query: str,
    k: int = 8,
    exclude: list[str] | None = None,
    folder: str = "",
    hybrid: bool = True,
    snippet_chars: int = 0,
) -> list[Hit]:
    """Hybrid retrieval: dense KNN + FTS5 BM25 fused with reciprocal-rank fusion.

    Hit.score is the fused RRF strength normalized to the best candidate
    (1.0 = top hit), so scores are monotonic with ranking — comparable within
    one result set, not across queries.

    Folder scopes use path-constrained FTS + exact distance over ``chunks.embedding``
    (no global vec0 scan). Exclude-only widens the KNN pool modestly, not to corpus size.
    """
    _search_degraded.set(None)
    _search_rerank.set(None)
    db = reader_connect()
    if db.execute("SELECT value FROM meta WHERE key='dim'").fetchone() is None:
        raise SystemExit("Index is empty — run `apo-engine index` first.")

    folder_prefix = folder.replace("\\", "/").strip("/")
    excl_prefixes, excl_globs = _compile_excludes(exclude)
    n = max(k * 4, config.SEARCH_CANDIDATES)
    if exclude and not folder_prefix:
        total_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n = min(total_chunks, max(n, _EXCLUDE_CANDIDATE_FLOOR))

    fused: dict[int, float] = {}
    frows: list[tuple] = []
    match: str | None = None

    # Overlap query embed with FTS on the shared pool (no per-call executor churn).
    embed_fut = _search_pool.submit(query_embed, query)
    if hybrid:
        fts_ready = db.execute("SELECT value FROM meta WHERE key='fts_ready'").fetchone()
        if fts_ready and fts_ready[0] == "1":
            match = _fts_query(query)
            if match:
                try:
                    if folder_prefix:
                        frows = db.execute(
                            """SELECT chunks_fts.rowid
                               FROM chunks_fts
                               JOIN chunks c ON c.id = chunks_fts.rowid
                               WHERE chunks_fts MATCH ?
                                 AND c.path LIKE ? ESCAPE '\\'
                               ORDER BY rank LIMIT ?""",
                            (match, _escape_like(folder_prefix) + "/%", n),
                        ).fetchall()
                    else:
                        frows = db.execute(
                            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                            (match, n),
                        ).fetchall()
                except sqlite3.OperationalError:
                    frows = []
    qvec = embed_fut.result()
    vrows: list[tuple] = []
    if qvec is None:
        # Embed backend down (e.g. Ollama not running) — degrade to keyword-only
        # instead of silently returning nothing.
        _search_degraded.set(
            f"query embedding failed (backend={config.EMBED_BACKEND!r}, "
            f"model={config.MODEL_NAME!r})"
        )
        if not frows:
            return []
    elif folder_prefix:
        prefer = [r[0] for r in frows] if frows else None
        vrows = _scoped_vector_hits(db, qvec, folder_prefix, n, prefer_ids=prefer)
    else:
        vrows = db.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(qvec), n),
        ).fetchall()

    for rank, (rid, _) in enumerate(vrows):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (rid,) in enumerate(frows):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    fts_rowid_set = {r[0] for r in frows}

    if not fused:
        return []
    top = max(fused.values())

    ranked = sorted(fused, key=lambda i: fused[i], reverse=True)
    # Reranking rescores a wider fused pool before the cut to k.
    rerank_on = config.RERANK and k > 0
    collect_n = max(k, config.RERANK_POOL) if rerank_on else k
    # Folder already constrained retrieval. Exclude may drop hits — when exclude is set,
    # fetch all fused candidates so we don't under-fill k after filtering.
    if exclude:
        fetch_n = len(ranked)
    else:
        fetch_n = min(len(ranked), max(collect_n * 2, collect_n + 8))
    ids = ranked[:fetch_n]
    by_id: dict[int, tuple] = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        for row in db.execute(
            f"""SELECT c.id, c.path, c.heading, c.text, c.chunk_hash, c.heading_level,
                       c.start_line, c.end_line, f.mtime, COALESCE(f.bytes, 0),
                       COALESCE(c.section_bytes, LENGTH(c.text)),
                       COALESCE(c.content_hash, ''),
                       COALESCE(c.chunk_kind, 'section'),
                       COALESCE(c.row_key, ''),
                       COALESCE(c.table_id, '')
                FROM chunks c LEFT JOIN files f ON f.path = c.path
                WHERE c.id IN ({placeholders})""",
            ids,
        ):
            by_id[row[0]] = row[1:]

    hits: list[Hit] = []
    full_texts: list[str] = []  # non-snippet bodies for the reranker
    for rid in ranked:
        row = by_id.get(rid)
        if row is None:
            continue
        (
            path,
            heading,
            text,
            chunk_hash,
            hlevel,
            start_line,
            end_line,
            mtime,
            file_bytes,
            section_bytes,
            content_hash,
            chunk_kind,
            row_key,
            table_id,
        ) = row
        if folder_prefix and not path.startswith(folder_prefix + "/"):
            continue
        if _path_excluded(path, excl_prefixes, excl_globs):
            continue
        score = (fused[rid] / top) * _catalog_retrieval_boost(
            path, chunk_kind or "section", folder_prefix
        )
        out_text = _build_snippet(
            text,
            snippet_chars,
            chunk_kind or "section",
            db=db,
            match=match,
            rid=rid,
            fts_rowids=fts_rowid_set,
        )
        hits.append(
            Hit(
                path=path,
                heading=heading or "",
                text=out_text,
                score=score,
                chunk_hash=chunk_hash or "",
                heading_level=int(hlevel or 0),
                start_line=int(start_line or 1),
                end_line=int(end_line or 1),
                source=str(vaults.notes_root() / path),
                mtime=float(mtime or 0.0),
                file_bytes=int(file_bytes or 0),
                section_bytes=int(section_bytes or 0),
                content_hash=content_hash or "",
                chunk_kind=chunk_kind or "section",
                row_key=row_key or "",
                table_id=table_id or "",
            )
        )
        full_texts.append(text)
        if len(hits) >= collect_n:
            break
    if folder_prefix and "mermaid-catalog" in folder_prefix.replace("\\", "/") and hits:
        paired = sorted(zip(hits, full_texts), key=lambda p: p[0].score, reverse=True)
        hits = [p[0] for p in paired]
        full_texts = [p[1] for p in paired]
    if rerank_on and hits:
        hits, status = rerank.rerank_hits(query, hits, k, texts=full_texts)
        _search_rerank.set(status)
    return hits


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


# --------------------------------------------------------------------------- #
# Catalog queries — frontmatter filter, backlinks, recent (index-backed, no vault scan)
# --------------------------------------------------------------------------- #
def _sql_pushdown_predicates(where: dict) -> tuple[str, list[Any]] | None:
    """AND of simple frontmatter predicates as SQL, or None if any clause needs Python.

    Supported for SQL: ``{$exists: bool}`` on safe identifier keys.
    Bare equality / ``{$eq: v}`` fall back to Python — ``json_extract = ?`` misses
    list-valued fields (tags/aliases) that ``_match_condition`` treats as membership,
    and SQL lacks ``_loose_eq`` type coercion.
    ``{$in: [...]}`` and richer operators also fall back to Python.
    """
    clauses: list[str] = []
    params: list[Any] = []
    from apo_engine.fm_path import path_needs_python_match

    for key, cond in where.items():
        # Nested / selector keys and non-$exists ops stay on the Python matcher
        # (list membership, $elemMatch, loose coercion).
        if not _FM_KEY_SAFE.match(key) or path_needs_python_match(key):
            return None
        jpath = f"$.{key}"
        if not isinstance(cond, dict):
            # Bare equality — may be list-valued FM; use Python matcher.
            return None
        ops = set(cond)
        if ops == {"$eq"}:
            return None
        if ops == {"$exists"}:
            if bool(cond["$exists"]):
                clauses.append("json_extract(frontmatter, ?) IS NOT NULL")
            else:
                clauses.append("json_extract(frontmatter, ?) IS NULL")
            params.append(jpath)
        else:
            return None

    if not clauses:
        return "1", []
    return " AND ".join(clauses), params


def _sql_json_scalar(v: Any) -> Any:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def _normalize_filter_sort(sort: str = "mtime", order: str = "desc") -> tuple[str, str]:
    """Validate ``sort`` / ``order`` for ``filter_notes``. Returns (sort, order)."""
    sort_key = (sort or "mtime").strip()
    order_key = (order or "desc").strip().lower()
    if order_key not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    if sort_key != "mtime" and not _FM_KEY_SAFE.match(sort_key):
        raise ValueError(
            "sort must be 'mtime' or a safe frontmatter key "
            "(letter/underscore start, alphanumeric + underscore)"
        )
    return sort_key, order_key


def _filter_notes_cmp(
    a: tuple[float, str, dict],
    b: tuple[float, str, dict],
    *,
    sort: str,
    order: str,
) -> int:
    """Compare two filter matches. Null / missing FM sort values sort last for both orders."""
    if sort == "mtime":
        c = _loose_cmp(a[0], b[0])
        return c if order == "asc" else -c
    va, vb = a[2].get(sort), b[2].get(sort)
    if va is None and vb is None:
        return _loose_cmp(a[0], b[0])  # mtime tie-break
    if va is None:
        return 1
    if vb is None:
        return -1
    c = _loose_cmp(va, vb)
    if c == 0:
        return _loose_cmp(a[0], b[0])
    return c if order == "asc" else -c


def _filter_notes_from_table(
    db: sqlite3.Connection,
    table: str,
    where: dict,
    folder: str = "",
    limit: int = 20,
    offset: int = 0,
    *,
    sort: str = "mtime",
    order: str = "desc",
    tree_oid: str | None = None,
) -> tuple[int, list[tuple[float, str, dict]]]:
    """Shared catalog query over ``files`` or ``ref_files`` (same column contract).

    When ``table='ref_files'``, ``tree_oid`` is required and scopes the projection.
    """
    if table not in ("files", "ref_files"):
        raise ValueError(f"unsupported filter table: {table!r}")
    if table == "ref_files" and not tree_oid:
        raise ValueError("tree_oid required for ref_files filter")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit < 0:
        raise ValueError("limit must be >= 0")
    sort_key, order_key = _normalize_filter_sort(sort, order)
    folder_prefix = folder.replace("\\", "/").strip("/")
    sql_pred = _sql_pushdown_predicates(where) if where else ("1", [])
    where_parts = ["frontmatter IS NOT NULL"]
    params: list[Any] = []
    if table == "ref_files":
        where_parts.append("tree_oid = ?")
        params.append(tree_oid)
    if folder_prefix:
        where_parts.append("path LIKE ? ESCAPE '\\'")
        params.append(_escape_like(folder_prefix) + "/%")

    if sql_pred is not None:
        pred_sql, pred_params = sql_pred
        where_parts.append(f"({pred_sql})")
        params.extend(pred_params)
        where_sql = " AND ".join(where_parts)
        total = int(
            db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where_sql}", params
            ).fetchone()[0]
        )
        dir_sql = "ASC" if order_key == "asc" else "DESC"
        order_params: list[Any] = []
        if sort_key == "mtime":
            order_sql = f"mtime {dir_sql}"
        else:
            jpath = f"$.{sort_key}"
            order_sql = (
                f"(json_extract(frontmatter, ?) IS NULL) ASC, "
                f"json_extract(frontmatter, ?) {dir_sql}, "
                f"mtime DESC"
            )
            order_params.extend([jpath, jpath])
        rows = db.execute(
            f"SELECT path, mtime, frontmatter FROM {table} WHERE {where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*params, *order_params, limit, offset],
        ).fetchall()
        matches: list[tuple[float, str, dict]] = []
        for path, mtime, fm_json in rows:
            try:
                fm = json.loads(fm_json) if fm_json else {}
            except json.JSONDecodeError:
                fm = {}
            matches.append((mtime, path, fm))
        return total, matches

    scope_sql = " AND ".join(where_parts)
    rows = db.execute(
        f"SELECT path, mtime, frontmatter FROM {table} WHERE {scope_sql}",
        params,
    ).fetchall()
    matches = []
    for path, mtime, fm_json in rows:
        try:
            fm = json.loads(fm_json) if fm_json else {}
        except json.JSONDecodeError:
            fm = {}
        if all(_match_where_clause(fm, k, cond) for k, cond in where.items()):
            matches.append((mtime, path, fm))
    matches.sort(
        key=cmp_to_key(
            lambda a, b: _filter_notes_cmp(a, b, sort=sort_key, order=order_key)
        )
    )
    return len(matches), matches[offset : offset + limit]


def filter_notes(
    where: dict,
    folder: str = "",
    limit: int = 20,
    offset: int = 0,
    *,
    sort: str = "mtime",
    order: str = "desc",
) -> tuple[int, list[tuple[float, str, dict]]]:
    """Deterministic frontmatter query over the cached `files.frontmatter` column.

    Returns (total_matches, page of matches), each match (mtime, path, frontmatter).
    Default sort is mtime desc. Pass ``sort`` as ``mtime`` or a safe frontmatter key
    (e.g. ``last_activity``) and ``order`` as ``asc`` / ``desc``. Missing FM sort
    values sort last. ``offset`` skips that many matches (0-based). No filesystem
    walk — reads the index only. ``$exists`` (and empty ``where``) push into SQL via
    ``json_extract``; equality, ``$elemMatch``, dotted paths, and richer operators use
    the Python matcher (correct for list-valued fields, list-of-dicts, and loose type
    coercion).

    When ``sort=mtime`` and predicates are SQL-pushable, uses ``COUNT(*)`` plus
    ``ORDER BY mtime … LIMIT/OFFSET`` — never materializes every matching frontmatter
    blob just to page ``limit`` rows. Frontmatter ``sort`` uses SQL ``json_extract``
    ordering on the pushdown path, or Python sort after match on the complex path.
    """
    db = reader_connect()
    return _filter_notes_from_table(
        db,
        "files",
        where,
        folder,
        limit,
        offset,
        sort=sort,
        order=order,
    )


def filter_notes_at_ref(
    where: dict,
    tree_oid: str,
    folder: str = "",
    limit: int = 20,
    offset: int = 0,
    *,
    sort: str = "mtime",
    order: str = "desc",
) -> tuple[int, list[tuple[float, str, dict]]]:
    """Like ``filter_notes`` but over the ``ref_files`` catalog for ``tree_oid``."""
    db = reader_connect()
    _ensure_ref_catalog_tables(db)
    return _filter_notes_from_table(
        db,
        "ref_files",
        where,
        folder,
        limit,
        offset,
        sort=sort,
        order=order,
        tree_oid=tree_oid,
    )


def search_at_ref(
    query: str,
    tree_oid: str,
    *,
    folder: str = "",
    exclude: list[str] | None = None,
    k: int = 8,
    offset: int = 0,
    snippet_chars: int = 240,
) -> tuple[list[dict[str, Any]], int]:
    """FTS-only note-level search over ``ref_fts`` for ``tree_oid``.

    Returns ``(hits, total)``. Hits have path/content/score/mtime — no chunk_hash.
    """
    match = _fts_query(query)
    if not match:
        return [], 0
    db = reader_connect()
    _ensure_ref_catalog_tables(db)
    prefixes, globs = _compile_excludes(exclude)
    folder_prefix = folder.replace("\\", "/").strip("/")
    where = ["tree_oid = ?", "ref_fts MATCH ?"]
    params: list[Any] = [tree_oid, match]
    if folder_prefix:
        where.append("path LIKE ? ESCAPE '\\'")
        params.append(_escape_like(folder_prefix) + "/%")
    where_sql = " AND ".join(where)
    tokens = max(1, min(int(snippet_chars) // 6, 64)) if snippet_chars else 64
    rows = db.execute(
        f"SELECT path, mtime, snippet(ref_fts, 3, '', '', ' … ', ?), rank "
        f"FROM ref_fts WHERE {where_sql} ORDER BY rank",
        [tokens, *params],
    ).fetchall()
    hits: list[dict[str, Any]] = []
    for path, mtime, snip, rank in rows:
        rel = str(path or "")
        if _path_excluded(rel, prefixes, globs):
            continue
        score = 1.0 / (1.0 + abs(float(rank or 0.0)))
        text = str(snip or "")
        if snippet_chars and len(text) > snippet_chars:
            text = text[:snippet_chars].rstrip() + "…"
        hits.append(
            {
                "path": rel,
                "content": text,
                "score": score,
                "mtime": float(mtime or 0.0),
            }
        )
    total = len(hits)
    page = hits[offset : offset + k]
    return page, total


def _escape_like(s: str) -> str:
    """Escape SQLite LIKE wildcards so a literal `_`/`%` in a path segment isn't treated as one."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def recent_notes(limit: int = 10, folder: str = "") -> list[tuple[str, float]]:
    """(path, mtime) for the most recently modified notes, index-backed — no per-file stat()."""
    db = reader_connect()
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


def recent_notes_preview(
    limit: int = 10,
    folder: str = "",
    *,
    since: float | None = None,
    until: float | None = None,
    preview: str = "first",
    heading: str | None = None,
    exclude: list[str] | None = None,
    preview_chars: int = 120,
) -> list[dict[str, Any]]:
    """Newest indexed notes by mtime with chunk preview — no vault file reads.

    ``preview`` is ``first`` (lowest matching ord; default ord=0) or ``last``
    (highest matching ord). Optional ``heading`` scopes the chunk pick to that
    governing heading (case-insensitive exact). ``exclude`` uses the same glob
    rules as search. Each row includes ``chunk_hash`` for expand/append.
    """
    limit = max(0, int(limit))
    if limit == 0:
        return []
    mode = (preview or "first").strip().lower()
    if mode not in ("first", "last"):
        raise ValueError("preview must be 'first' or 'last'")
    chars = max(1, min(int(preview_chars), 2000))
    heading_key = (heading or "").strip() or None

    db = reader_connect()
    folder_prefix = folder.replace("\\", "/").strip("/")
    where: list[str] = []
    params: list[Any] = []
    if folder_prefix:
        where.append("f.path LIKE ? ESCAPE '\\'")
        params.append(_escape_like(folder_prefix) + "/%")
    if since is not None:
        where.append("f.mtime >= ?")
        params.append(float(since))
    if until is not None:
        where.append("f.mtime <= ?")
        params.append(float(until))
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # Chunk pick: default first = ord 0; with heading / last use MIN/MAX subquery.
    # Heading match is breadcrumb-aware (exact, leaf after " › ", or segment).
    _heading_match = (
        "("
        "lower(c2.heading) = lower(?) OR "
        "lower(c2.heading) LIKE '% › ' || lower(?) OR "
        "lower(c2.heading) LIKE lower(?) || ' › %' OR "
        "lower(c2.heading) LIKE '% › ' || lower(?) || ' › %'"
        ")"
    )
    if heading_key is None and mode == "first":
        join_sql = "LEFT JOIN chunks c ON c.path = f.path AND c.ord = 0"
        join_params: list[Any] = []
    else:
        agg = "MIN" if mode == "first" else "MAX"
        if heading_key is None:
            join_sql = (
                "LEFT JOIN chunks c ON c.path = f.path AND c.ord = ("
                f"SELECT {agg}(c2.ord) FROM chunks c2 WHERE c2.path = f.path)"
            )
            join_params = []
        else:
            join_sql = (
                "LEFT JOIN chunks c ON c.path = f.path AND c.ord = ("
                f"SELECT {agg}(c2.ord) FROM chunks c2 WHERE c2.path = f.path "
                f"AND {_heading_match})"
            )
            join_params = [heading_key, heading_key, heading_key, heading_key]

    excl_prefixes, excl_globs = _compile_excludes(exclude)
    # Over-fetch when excluding so LIMIT still yields enough survivors.
    fetch_n = limit
    if excl_prefixes or excl_globs:
        fetch_n = min(max(limit * 5, limit + 20), 500)

    # first → leading chars; last → trailing chars (session-log digests).
    if mode == "last":
        text_expr = (
            "COALESCE("
            "CASE WHEN length(c.text) > ? "
            "THEN substr(c.text, length(c.text) - ? + 1) "
            "ELSE c.text END, '')"
        )
        text_params: list[Any] = [chars, chars]
    else:
        text_expr = "COALESCE(substr(c.text, 1, ?), '')"
        text_params = [chars]

    sql = f"""
        SELECT f.path, f.mtime,
               {text_expr},
               COALESCE(c.chunk_hash, ''),
               COALESCE(c.heading, ''),
               f.frontmatter
        FROM files f
        {join_sql}
        {where_sql}
        ORDER BY f.mtime DESC
        LIMIT ?
    """
    rows = db.execute(sql, [*text_params, *join_params, *params, fetch_n]).fetchall()

    out: list[dict[str, Any]] = []
    for path, mtime, text_preview, chunk_hash, chunk_heading, fm_raw in rows:
        if _path_excluded(path, excl_prefixes, excl_globs):
            continue
        fm: dict[str, Any] | None = None
        if fm_raw:
            try:
                parsed = json.loads(fm_raw)
                if isinstance(parsed, dict):
                    fm = parsed
            except json.JSONDecodeError:
                fm = None
        out.append(
            {
                "path": path,
                "mtime": float(mtime),
                "first_line": (text_preview or "").replace("\n", " ").strip(),
                "chunk_hash": chunk_hash or "",
                "heading": chunk_heading or "",
                "frontmatter": fm,
            }
        )
        if len(out) >= limit:
            break
    return out


def frontmatter_field(rel_path: str, field: str) -> Any:
    """Read one cached frontmatter field for a vault-relative path (index only)."""
    from apo_engine.note_format import ensure_indexed_path

    rel = ensure_indexed_path(rel_path)
    db = reader_connect()
    row = db.execute("SELECT frontmatter FROM files WHERE path=?", (rel,)).fetchone()
    if not row or row[0] is None:
        return None
    try:
        fm = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    return fm.get(field) if isinstance(fm, dict) else None


def list_backlinks(
    target_keys: set[str], exclude_source: str = "", limit: int = 100
) -> list[tuple[str, int, str]]:
    """(source path, line, line text) for notes linking to any of target_keys (stem or full path)."""
    if not target_keys:
        return []
    db = reader_connect()
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

    coll = collection or vaults.collection()
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
