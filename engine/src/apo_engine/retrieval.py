"""Search, filtering, retrieval, snippets, and ranking integration."""

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

def fts_query(query: str) -> str | None:
    terms = [term for term in re.findall(r"\w+", query.lower()) if len(term) > 1][
        :24
    ]
    if not terms:
        return None
    separator = " AND " if len(terms) <= 2 else " OR "
    return separator.join(f'"{term}"' for term in terms)

def escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )

# >1 so concurrent MCP searches (each already on a worker thread) can embed in parallel.
_search_pool = ThreadPoolExecutor(max_workers=4)
def _hybrid_candidate_pools(
    k: int,
    *,
    exclude: bool,
    folder_prefix: str,
    total_chunks: int,
) -> tuple[int, int]:
    base = max(k * 4, config.SEARCH_CANDIDATES)
    if exclude and not folder_prefix:
        return (
            min(total_chunks, max(base, config.EXCLUDE_CANDIDATE_FLOOR)),
            min(total_chunks, config.EXCLUDE_VEC_K),
        )
    if not folder_prefix:
        vector_cap = int(getattr(config, "UNSCOPED_VEC_K", 48))
        return base, min(total_chunks, base, vector_cap)
    return base, base
_search_degraded: ContextVar[str | None] = ContextVar("apo_search_degraded", default=None)
_search_rerank: ContextVar[dict | None] = ContextVar("apo_search_rerank", default=None)


def last_search_degraded() -> str | None:
    """Reason the most recent search() in this context ran degraded, or None."""
    return _search_degraded.get()


def last_search_rerank() -> dict | None:
    """Rerank status for the most recent search() in this context, or None."""
    return _search_rerank.get()
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
# Ranking/boost heuristics (filename/slug/ticket, title/frontmatter, backlink,
# link-neighbor, mermaid-catalog, eval-table demotion, architecture vocabulary)
# live in ranking.py — see that module's docstring for why, and for the
# per-vault boost_vocab mechanism that replaced the old hardcoded arch regex.


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


def _sql_path_prefix_excludes(prefixes: list[str]) -> tuple[str, list[Any]]:
    """SQL ``AND c.path NOT LIKE …`` fragments for prefix-style exclude globs."""
    parts: list[str] = []
    params: list[Any] = []
    for pref in prefixes:
        parts.append("c.path NOT LIKE ? ESCAPE '\\'")
        params.append(_escape_like(pref) + "%")
    if not parts:
        return "", []
    return " AND " + " AND ".join(parts), params


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
    if chunk_kind not in ("section", "section_part", "", None):
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
    """Embedder input. ``meta["embed_text"]`` (oversize sections) overrides the cleaned text.

    The override feeds the vector only — ``chunks.text`` and the FTS row keep the full
    section, so keyword search still covers all of it.
    """
    meta = row[10] if len(row) > 10 else None
    if isinstance(meta, dict) and meta.get("embed_text"):
        return meta["embed_text"]
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


def _hits_for_ids(
    db: sqlite3.Connection,
    ids: list[int],
    folder_prefix: str,
    *,
    match: str | None,
    fts_rowid_set: set[int],
    snippet_chars: int,
) -> list[Hit]:
    """Materialize ``Hit`` rows for a caller-ranked chunk id list, preserving order.

    No RRF, no boosts, no rerank — building block for a caller doing its own fusion
    across several ranked lists (:func:`search_expanded`); :func:`search` does its
    fusion/boosts inline instead of going through this.
    """
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    by_id: dict[int, tuple] = {}
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
    root = vaults.notes_root()
    hits: list[Hit] = []
    for rid in ids:
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
                score=0.0,  # caller (RRF fusion) overwrites this
                chunk_hash=chunk_hash or "",
                heading_level=int(hlevel or 0),
                start_line=int(start_line or 1),
                end_line=int(end_line or 1),
                source=str(root / path),
                mtime=float(mtime or 0.0),
                file_bytes=int(file_bytes or 0),
                section_bytes=int(section_bytes or 0),
                content_hash=content_hash or "",
                chunk_kind=chunk_kind or "section",
                row_key=row_key or "",
                table_id=table_id or "",
            )
        )
    return hits


def search_lex_only(
    query: str,
    k: int = 8,
    folder: str = "",
    snippet_chars: int = 0,
    exclude: list[str] | None = None,
) -> list[Hit]:
    """Pure BM25/FTS5 ranked hits — no vector fusion.

    Building block for typed query-expansion routing (:func:`search_expanded`): a
    ``lex`` sub-query should hit FTS only, not contaminate the vector pool.
    """
    db = reader_connect()
    folder_prefix = folder.replace("\\", "/").strip("/")
    excl_prefixes, excl_globs = _compile_excludes(exclude)
    match = _fts_query(query)
    if not match:
        return []
    fts_ready = db.execute("SELECT value FROM meta WHERE key='fts_ready'").fetchone()
    if not (fts_ready and fts_ready[0] == "1"):
        return []
    total_chunks = int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 0)
    fetch_n, _ = _hybrid_candidate_pools(
        k,
        exclude=bool(exclude),
        folder_prefix=folder_prefix,
        total_chunks=total_chunks,
    )
    try:
        if folder_prefix:
            frows = db.execute(
                """SELECT chunks_fts.rowid
                   FROM chunks_fts
                   JOIN chunks c ON c.id = chunks_fts.rowid
                   WHERE chunks_fts MATCH ?
                     AND c.path LIKE ? ESCAPE '\\'
                   ORDER BY rank LIMIT ?""",
                (match, _escape_like(folder_prefix) + "/%", fetch_n),
            ).fetchall()
        else:
            frows = db.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, fetch_n),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    ids = [r[0] for r in frows]
    hits = _hits_for_ids(
        db, ids, folder_prefix, match=match, fts_rowid_set=set(ids), snippet_chars=snippet_chars
    )
    if not exclude:
        return hits[:k]
    out: list[Hit] = []
    for h in hits:
        if _path_excluded(h.path, excl_prefixes, excl_globs):
            continue
        out.append(h)
        if len(out) >= k:
            break
    return out


def search_vector_only(
    qvec: list[float],
    k: int = 8,
    folder: str = "",
    snippet_chars: int = 0,
    exclude: list[str] | None = None,
) -> list[Hit]:
    """Pure dense-KNN ranked hits — no FTS fusion.

    Building block for typed query-expansion routing (:func:`search_expanded`): a
    ``vec``/``hyde`` sub-query should hit the vector index only.
    """
    db = reader_connect()
    folder_prefix = folder.replace("\\", "/").strip("/")
    excl_prefixes, excl_globs = _compile_excludes(exclude)
    total_chunks = int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 0)
    _, fetch_n = _hybrid_candidate_pools(
        k,
        exclude=bool(exclude),
        folder_prefix=folder_prefix,
        total_chunks=total_chunks,
    )
    if folder_prefix:
        vrows = _scoped_vector_hits(db, qvec, folder_prefix, fetch_n, prefer_ids=None)
    else:
        vrows = db.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(qvec), fetch_n),
        ).fetchall()
    vrows = _collapse_part_hits(db, vrows)
    ids = [r[0] for r in vrows]
    hits = _hits_for_ids(
        db, ids, folder_prefix, match=None, fts_rowid_set=set(), snippet_chars=snippet_chars
    )
    if not exclude:
        return hits[:k]
    out: list[Hit] = []
    for h in hits:
        if _path_excluded(h.path, excl_prefixes, excl_globs):
            continue
        out.append(h)
        if len(out) >= k:
            break
    return out


_EXPAND_LINE_RE = re.compile(r"^\s*(lex|vec|hyde)\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _parse_qmd_expansion(text: str, original_query: str) -> list[dict[str, Any]]:
    """Parse qmd-query-expansion-1.7B's native ``lex:``/``vec:``/``hyde:`` line output.

    The model may emit multiple lines of the same type (unlike a generic JSON-object
    prompt, which can only carry one of each) and sometimes emits a stray/unclosed
    ``<think>`` line despite being trained on ``/no_think`` — scan every line for a
    typed prefix and ignore everything else rather than trying to strip a
    ``<think>...</think>`` block, which isn't reliably well-formed in practice.
    """
    out: list[dict[str, Any]] = []
    lowered_query = original_query.strip().lower()
    for line in text.splitlines():
        m = _EXPAND_LINE_RE.match(line)
        if not m:
            continue
        qtext = m.group(2).strip()
        if qtext and qtext.lower() != lowered_query:
            out.append({"type": m.group(1).lower(), "query": qtext, "weight": 1.0})
    return out


def expand_query(query: str, *, intent: str = "") -> list[dict[str, Any]]:
    """Typed sub-query expansion (``lex``/``vec``/``hyde``) via a local Ollama model.

    Uses qmd (github.com/tobi/qmd)'s own fine-tuned expansion model
    (``tobil/qmd-query-expansion-1.7B``, trained specifically for this task — not a
    general chat model prompted for JSON) — see ``docs/models/qmd-query-expansion.md``
    for the one-time ``ollama pull`` + ``ollama create`` setup. Its prompt format is
    fixed by its own SFT training (``/no_think Expand this search query: {query}``,
    baked into the Ollama Modelfile's TEMPLATE) — ``intent`` has no dedicated slot,
    so it's appended inline as parenthetical context.

    The original query is always searched on both backends first (mirrors qmd's own
    README: "The original query is sent to both backends"); the model's typed
    sub-queries (if generation is enabled and succeeds) are added on top, each meant
    for exactly one backend once :func:`search_expanded` routes them. Disabled by
    default (``APO_QUERY_EXPAND``); on any failure — model down, timeout — falls back
    to just the two base entries. Never raises.
    """
    base: list[dict[str, Any]] = [
        {"type": "lex", "query": query, "weight": 2.0},
        {"type": "vec", "query": query, "weight": 2.0},
    ]
    if not config.QUERY_EXPAND:
        return base
    try:
        prompt_query = f"{query} (context: {intent})" if intent else query
        payload = json.dumps(
            {
                "model": config.QUERY_EXPAND_MODEL,
                "prompt": prompt_query,
                "stream": False,
                "think": False,  # belt-and-suspenders alongside the model's own
                # /no_think training — a stray <think> line was still observed once
                "keep_alive": config.QUERY_EXPAND_KEEP_ALIVE,
            }
        ).encode()
        req = urllib.request.Request(
            f"{config.OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=config.QUERY_EXPAND_TIMEOUT) as resp:
            data = json.load(resp)
        extra = _parse_qmd_expansion(data.get("response", "") or "", query)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        OSError,
    ):
        return base
    return base + extra


def search_expanded(
    query: str = "",
    *,
    queries: list[dict[str, Any]] | None = None,
    intent: str = "",
    k: int = 8,
    folder: str = "",
    snippet_chars: int = 0,
    exclude: list[str] | None = None,
    explain: bool = False,
) -> list[Hit]:
    """RRF-fuse typed sub-queries, each routed to exactly one backend.

    ``lex`` -> :func:`search_lex_only`; ``vec``/``hyde`` -> embed + :func:`search_vector_only`
    (any other/missing type is treated as ``lex``). Pass ``queries`` for caller-supplied
    typed sub-queries (pre-expanded, e.g. from an MCP client — same shape qmd's ``query``
    tool takes); otherwise ``query`` is auto-expanded via :func:`expand_query`.

    Distinct from :func:`search`'s single-query internal FTS+vector fusion — this fuses
    *across several independently-ranked sub-query result lists*, keyed by ``chunk_hash``
    (RRF only needs each input's rank order, not how it was produced). One refinement
    on top of plain RRF, adapted from qmd's own fusion (its architecture doc, not just
    its README — a deeper pass than the ``folder_context``/transport features):

    - **Top-rank bonus**: a chunk that ranks #1 in *any* sub-query's own list gets a
      flat bonus on its fused score (+0.05 of the pool's top score; #2-3 get +0.02).
      Protects an exact match for the original query from being diluted when the
      expanded variants disagree with it.

    Position-aware rerank blending — reranking weighted by retrieval rank tier
    instead of fully overriding fused order — is shared with :func:`search`'s
    reranking now (both call :func:`rerank.rerank_scores`), not exclusive to this
    function. The **top-rank bonus stayed exclusive to this function**: measured with
    ``just search-eval`` against a 24-query labeled set (``docs/search-quality.md``),
    it *hurt* :func:`search`'s plain 2-list (FTS + vector) single-query fusion
    (MRR@5 0.861 → 0.854) — "#1 in either list" is a much weaker agreement signal for
    one query than "#1 in several independently-generated sub-query variants," which
    is what the bonus is actually validated for here.
    """
    if queries is None:
        queries = expand_query(query, intent=intent)
    folder_prefix = folder.replace("\\", "/").strip("/")
    excl_prefixes, excl_globs = _compile_excludes(exclude)
    db = reader_connect()
    total_chunks = int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 0)
    pool_n, _ = _hybrid_candidate_pools(
        k,
        exclude=bool(exclude),
        folder_prefix=folder_prefix,
        total_chunks=total_chunks,
    )
    pool_n = max(pool_n, k * 3, config.SEARCH_CANDIDATES)
    fused: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    hit_by_hash: dict[str, Hit] = {}
    full_texts: dict[str, str] = {}
    for sq in queries[:10]:
        qtype = str(sq.get("type") or "lex").strip().lower()
        qtext = str(sq.get("query") or "").strip()
        if not qtext:
            continue
        weight = float(sq.get("weight", 1.0))
        if qtype == "lex":
            sub_hits = search_lex_only(
                qtext,
                k=pool_n,
                folder=folder,
                snippet_chars=0,
                exclude=exclude,
            )
        else:
            qvec = query_embed(qtext)
            if qvec is None:
                continue
            sub_hits = search_vector_only(
                qvec,
                k=pool_n,
                folder=folder,
                snippet_chars=0,
                exclude=exclude,
            )
        for rank, h in enumerate(sub_hits):
            fused[h.chunk_hash] = fused.get(h.chunk_hash, 0.0) + weight / (RRF_K + rank)
            if rank < best_rank.get(h.chunk_hash, rank + 1):
                best_rank[h.chunk_hash] = rank
            hit_by_hash.setdefault(h.chunk_hash, h)
            full_texts.setdefault(h.chunk_hash, h.text)
    if not fused:
        return []

    top = max(fused.values())
    for chash, rank in best_rank.items():
        if rank == 0:
            fused[chash] += 0.05 * top
        elif rank <= 2:
            fused[chash] += 0.02 * top

    ranked = sorted(fused, key=lambda ch: fused[ch], reverse=True)
    rerank_on = config.RERANK and k > 0
    collect_n = max(k, config.RERANK_POOL) if rerank_on else k
    scan = ranked if exclude else (ranked[:collect_n] if collect_n > 0 else ranked)
    top = max(fused.values())  # re-max after the bonus so 1.0 still means "pool best"
    primary_query = (query or "").strip() or str((queries[0] or {}).get("query") or "")
    out: list[Hit] = []
    for chash in scan:
        h = hit_by_hash[chash]
        h.score = round(fused[chash] / top, 4)
        if exclude and _path_excluded(h.path, excl_prefixes, excl_globs):
            continue
        if snippet_chars > 0:
            h.text = _truncate_word_boundary(h.text, snippet_chars)
        if explain:
            h.explain = {
                "fused": round(fused[chash] / top, 4),
                "subquery_best_rank": best_rank.get(chash),
            }
        out.append(h)
        if len(out) >= collect_n:
            break

    out = ranking._apply_path_boosts_to_hits(out, primary_query, explain=explain, folder=folder)

    if rerank_on and out:
        rerank_query = str((queries[0] or {}).get("query") or query)
        texts = [full_texts.get(h.chunk_hash, h.text) for h in out]
        scores, status = rerank.rerank_scores(rerank_query, texts)
        if scores is not None:
            lo, hi = min(scores), max(scores)
            span = hi - lo
            for rrf_rank, h in enumerate(out):
                rr_norm = (scores[rrf_rank] - lo) / span if span > 0 else 1.0
                w = 0.75 if rrf_rank < 3 else 0.60 if rrf_rank < 10 else 0.40
                h.score = round(w * h.score + (1 - w) * rr_norm, 4)
            out.sort(key=lambda h: h.score, reverse=True)
        _search_rerank.set(status)
        out = out[:k] if k > 0 else out
    return out


def _search_lex_boosted(
    query: str,
    k: int,
    *,
    exclude: list[str] | None = None,
    folder: str = "",
    snippet_chars: int = 0,
    explain: bool = False,
) -> list[Hit]:
    """Keyword-only search with retrieval boosts (no query embed, no vector scan)."""
    folder_prefix = folder.replace("\\", "/").strip("/")
    rerank_on = config.RERANK and k > 0
    collect_n = max(k, config.RERANK_POOL) if rerank_on else k
    hits = search_lex_only(
        query,
        k=max(collect_n * 2, config.SEARCH_CANDIDATES),
        folder=folder_prefix,
        snippet_chars=snippet_chars,
        exclude=exclude,
    )
    full_texts = [h.text for h in hits]
    hits = ranking._apply_path_boosts_to_hits(
        hits, query, explain=explain, neighbor=True, folder=folder_prefix
    )
    full_texts = [h.text for h in hits]
    if rerank_on and hits:
        scores, status = rerank.rerank_scores(query, full_texts)
        if scores is not None:
            lo, hi = min(scores), max(scores)
            span = hi - lo
            for rrf_rank, h in enumerate(hits):
                rr_norm = (scores[rrf_rank] - lo) / span if span > 0 else 1.0
                w = 0.75 if rrf_rank < 3 else 0.60 if rrf_rank < 10 else 0.40
                h.score = round(w * h.score + (1 - w) * rr_norm, 4)
            hits.sort(key=lambda h: h.score, reverse=True)
        _search_rerank.set(status)
        hits = hits[:k] if k > 0 else hits
    elif k > 0:
        hits = hits[:k]
    return hits


def search(
    query: str,
    k: int = 8,
    exclude: list[str] | None = None,
    folder: str = "",
    hybrid: bool = True,
    snippet_chars: int = 0,
    *,
    explain: bool = False,
) -> list[Hit]:
    """Hybrid retrieval: dense KNN + FTS5 BM25 fused with reciprocal-rank fusion.

    Hit.score is the fused RRF strength normalized to the best candidate
    (1.0 = top hit), so scores are monotonic with ranking — comparable within
    one result set, not across queries. Catalog-scoped or architecture-vocabulary
    queries apply :func:`ranking._catalog_retrieval_boost` on top of that normalization,
    which can push a boosted hit's score *above* 1.0 — "1.0 = top hit" is exact
    only when no boost is in play. With ``config.RERANK`` on, this is the
    *pre-rerank* score the reranker score is then position-aware blended
    against (see :func:`rerank.rerank_scores`) — the top hit's final score can
    land just under 1.0 when the reranker didn't fully agree it belonged
    there, or above 1.0 when it was already boosted.

    Folder scopes use path-constrained FTS + exact distance over ``chunks.embedding``
    (no global vec0 scan). Unscoped exclude widens the FTS pool only; dense KNN stays
    under ``EXCLUDE_VEC_K`` (not the FTS exclude floor).
    """
    _search_degraded.set(None)
    _search_rerank.set(None)
    if not hybrid:
        return _search_lex_boosted(
            query,
            k,
            exclude=exclude,
            folder=folder,
            snippet_chars=snippet_chars,
            explain=explain,
        )
    db = reader_connect()
    if db.execute("SELECT value FROM meta WHERE key='dim'").fetchone() is None:
        raise SystemExit("Index is empty — run `apo-engine index` first.")

    folder_prefix = folder.replace("\\", "/").strip("/")
    excl_prefixes, excl_globs = _compile_excludes(exclude)
    total_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    fts_n, vec_n = _hybrid_candidate_pools(
        k,
        exclude=bool(exclude),
        folder_prefix=folder_prefix,
        total_chunks=int(total_chunks or 0),
    )

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
                excl_sql, excl_params = _sql_path_prefix_excludes(excl_prefixes)
                try:
                    if folder_prefix:
                        frows = db.execute(
                            f"""SELECT chunks_fts.rowid
                               FROM chunks_fts
                               JOIN chunks c ON c.id = chunks_fts.rowid
                               WHERE chunks_fts MATCH ?
                                 AND c.path LIKE ? ESCAPE '\\'{excl_sql}
                               ORDER BY rank LIMIT ?""",
                            (match, _escape_like(folder_prefix) + "/%", *excl_params, fts_n),
                        ).fetchall()
                    else:
                        frows = db.execute(
                            f"""SELECT chunks_fts.rowid
                               FROM chunks_fts
                               JOIN chunks c ON c.id = chunks_fts.rowid
                               WHERE chunks_fts MATCH ?{excl_sql}
                               ORDER BY rank LIMIT ?""",
                            (match, *excl_params, fts_n),
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
        vrows = _scoped_vector_hits(db, qvec, folder_prefix, vec_n, prefer_ids=prefer)
    else:
        vrows = db.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(qvec), vec_n),
        ).fetchall()
    vrows = _collapse_part_hits(db, vrows)

    for rank, (rid, _) in enumerate(vrows):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    fts_rank_by_id: dict[int, int] = {}
    for rank, (rid,) in enumerate(frows):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
        fts_rank_by_id[rid] = rank
    vec_rank_by_id: dict[int, int] = {rid: rank for rank, (rid, _) in enumerate(vrows)}
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
    vault_wide_arch = (not folder_prefix) and ranking._is_architecture_query(query)
    if exclude:
        scan_cap = int(getattr(config, "EXCLUDE_HIT_SCAN_MAX", 96))
        fetch_n = min(len(ranked), max(collect_n * 4, scan_cap))
    else:
        fetch_n = min(len(ranked), max(collect_n * 2, collect_n + 8))
        # Wider pool so vault-wide mermaid boosts can promote diagrams that
        # sat just outside the default top-N fused cut.
        if vault_wide_arch:
            fetch_n = min(len(ranked), max(fetch_n, 48))
    # Collect enough candidates for post-fusion boost reordering (then cut to k).
    if vault_wide_arch and not rerank_on and k > 0:
        promote = int(getattr(config, "ARCH_PROMOTE_POOL", 48))
        collect_n = min(fetch_n, promote)
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

    ranking._prefetch_path_boost_data(row[0] for row in by_id.values())

    hits: list[Hit] = []
    full_texts: list[str] = []  # non-snippet bodies for the reranker
    # Post-fusion result-diversity cap (config.RESULT_DIVERSITY_CAP): a same-source
    # group (same table_id for table rows/headers, else same note path — see
    # ranking.diversity_group_key) fills at most that many of the primary slots;
    # anything over cap is held in overflow and only used to backfill remaining
    # slots if the capped pool couldn't fill collect_n on its own. Soft cap — a
    # sufficiently table-row-heavy fused pool can still under-diversify if there
    # aren't enough other-source candidates in the fetched window.
    overflow_hits: list[Hit] = []
    overflow_texts: list[str] = []
    group_counts: dict[str, int] = {}
    diversity_cap = int(getattr(config, "RESULT_DIVERSITY_CAP", 2))
    for rid in ids:
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
        fused_norm = fused[rid] / top
        catalog_mult = ranking._catalog_retrieval_boost(
            path, chunk_kind or "section", folder_prefix, query=query
        )
        path_mult, path_detail = ranking._path_retrieval_boost(
            path, chunk_kind or "section", query, text=text
        )
        score = fused_norm * catalog_mult * path_mult
        hit_explain: dict[str, Any] = {}
        if explain:
            hit_explain = {
                "fts_rank": fts_rank_by_id.get(rid),
                "vec_rank": vec_rank_by_id.get(rid),
                "fused": round(fused_norm, 4),
                "catalog_boost": catalog_mult,
                **path_detail,
                "final_score": round(score, 4),
            }
        out_text = _build_snippet(
            text,
            snippet_chars,
            chunk_kind or "section",
            db=db,
            match=match,
            rid=rid,
            fts_rowids=fts_rowid_set,
        )
        hit = Hit(
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
            explain=hit_explain,
        )
        group_key = ranking.diversity_group_key(path, chunk_kind or "section", table_id or "")
        if group_counts.get(group_key, 0) < diversity_cap:
            group_counts[group_key] = group_counts.get(group_key, 0) + 1
            hits.append(hit)
            full_texts.append(text)
        else:
            overflow_hits.append(hit)
            overflow_texts.append(text)
        if len(hits) >= collect_n:
            break
    if len(hits) < collect_n and overflow_hits:
        need = collect_n - len(hits)
        hits.extend(overflow_hits[:need])
        full_texts.extend(overflow_texts[:need])
    hits, full_texts = ranking._inject_phrase_stem_hits(
        hits, query, folder_prefix, explain=explain, full_texts=full_texts
    )
    if hybrid and not hits and folder_prefix:
        lex_fallback = search_lex_only(
            query,
            k=max(collect_n * 2, config.SEARCH_CANDIDATES),
            folder=folder_prefix,
            exclude=exclude,
        )
        if lex_fallback:
            hits = lex_fallback[:collect_n]
            full_texts = [h.text for h in hits]
            for h in hits:
                mult, detail = ranking._path_retrieval_boost(
                    h.path, h.chunk_kind or "section", query, text=h.text
                )
                h.score = round(h.score * mult, 4)
                if explain:
                    h.explain = {**(h.explain or {}), **detail, "lex_fallback": True}
    # Re-sort after path/chunk boosts (catalog, slug/ticket, backlink, eval demotion).
    if hits:
        paired = sorted(zip(hits, full_texts), key=lambda p: p[0].score, reverse=True)
        hits = [p[0] for p in paired]
        full_texts = [p[1] for p in paired]
        if vault_wide_arch and not rerank_on and k > 0:
            hits = hits[:k]
            full_texts = full_texts[:k]
    hits = ranking._neighbor_rank_boost(hits, query, explain=explain)
    if rerank_on and hits:
        # Position-aware blend, not a full override — see docs/search-quality.md for
        # the eval (0.795 -> 0.826 MRR@5 vs. the old rerank.rerank_hits() reorder).
        scores, status = rerank.rerank_scores(query, full_texts)
        if scores is not None:
            lo, hi = min(scores), max(scores)
            span = hi - lo
            for rrf_rank, h in enumerate(hits):
                rr_norm = (scores[rrf_rank] - lo) / span if span > 0 else 1.0
                w = 0.75 if rrf_rank < 3 else 0.60 if rrf_rank < 10 else 0.40
                h.score = round(w * h.score + (1 - w) * rr_norm, 4)
            hits.sort(key=lambda h: h.score, reverse=True)
        _search_rerank.set(status)
        hits = hits[:k] if k > 0 else hits
    return hits
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
    db = ref_catalog.reader_connect()
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
    db = ref_catalog.reader_connect()
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

__owned__ = ['fts_query', 'escape_like', '_search_pool', '_hybrid_candidate_pools', '_search_degraded', '_search_rerank', 'last_search_degraded', 'last_search_rerank', '_loose_eq', '_loose_cmp', '_match_condition', '_match_where_clause', '_compile_excludes', '_path_excluded', '_sql_path_prefix_excludes', '_SEP_ROW_RE', '_MD_LINK_RE', '_MD_BOLD_RE', '_MD_ITALIC_RE', '_MD_HEADING_RE', '_MD_LIST_RE', '_MD_QUOTE_RE', '_BLANK_RUN_RE', '_WS_RE', '_FTS_SNIPPET_MAX_TOKENS', '_collapse_full_tables', '_strip_markdown_decoration', '_truncate_word_boundary', '_fts_snippet_excerpt', '_index_text_for_embedding', '_pending_chunk_kind', '_pending_index_text', '_build_snippet', '_hits_for_ids', 'search_lex_only', 'search_vector_only', '_EXPAND_LINE_RE', '_parse_qmd_expansion', 'expand_query', 'search_expanded', '_search_lex_boosted', 'search', '_sql_pushdown_predicates', '_sql_json_scalar', '_normalize_filter_sort', '_filter_notes_cmp', '_filter_notes_from_table', 'filter_notes', 'filter_notes_at_ref', 'search_at_ref', 'recent_notes', 'recent_notes_preview', 'frontmatter_field']
