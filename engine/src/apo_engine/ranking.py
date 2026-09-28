"""Ranking — hybrid-search boost/demotion heuristics applied on top of RRF fusion.

Split out of ``core.py`` for readability: :func:`apo_engine.core.search` fuses
dense (vector) + FTS5 candidates via reciprocal-rank fusion, then hands the pool
here for post-fusion multipliers — filename/slug/ticket-id matches, title/
frontmatter overlap, backlink density, 1-hop link-neighbor promotion, mermaid
catalog recall, and an eval-table demotion — before the final sort. Same logic,
same call signatures as before the split; this module does not change scoring
behavior.

References :mod:`apo_engine.core` (``reader_connect``, ``Hit``, ``list_backlinks``,
``list_outlinks``, ``_escape_like``) via ``import core`` rather than
``from .core import ...`` — core.py imports this module too (to call the boosts
from ``search()``), and only the whole-module form survives that circular
import; every ``core.<name>`` reference below is resolved at call time, once
both modules have finished initializing, and stays mock-patchable via
``mock.patch.object(core, "reader_connect", ...)`` in tests.

Architecture/system vocabulary — vault-wide mermaid boost only when the query
looks like a DFD/CDE/payment-flow question — used to be a single hardcoded
regex baked into this engine (real internal system/vendor names from one
vault's employer). It now comes from that vault's own
``search-contract.schema.yaml`` (``boost_vocab:``) via
:mod:`apo_engine.search_contract`. A vault with no ``boost_vocab`` gets no
architecture-query boost at all — see :func:`_is_architecture_query`.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import core
from . import search_contract
from . import vaults

# --------------------------------------------------------------------------- #
# Architecture/system vocabulary (per-vault, via search-contract boost_vocab)
# --------------------------------------------------------------------------- #

_arch_vocab_cache: dict[str, tuple[float, "re.Pattern[str] | None"]] = {}
_arch_vocab_lock = threading.Lock()
_ARCH_VOCAB_CACHE_TTL = 30.0


def _compile_vocab_regex(terms: list[str]) -> "re.Pattern[str] | None":
    """Build a case-insensitive word-boundary regex from a vault's boost_vocab list.

    Multi-word phrases match with flexible whitespace/hyphen separators (e.g.
    ``"data flow"`` also matches ``"data-flow"``), and a literal ``.`` is
    optional (e.g. ``"authorize.net"`` also matches ``"authorizenet"``) — this
    mirrors the matching the old hardcoded ``_ARCH_QUERY_RE`` did for its own
    phrases, generalized to any vault's term list.
    """
    parts: list[str] = []
    for term in terms:
        cleaned = (term or "").strip()
        if not cleaned:
            continue
        escaped = re.escape(cleaned).replace(r"\ ", r"[\s-]*").replace(r"\.", r"\.?")
        parts.append(escaped)
    if not parts:
        return None
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.I)


def _architecture_query_regex(vault_root: Path) -> "re.Pattern[str] | None":
    """Cached per-vault compiled boost_vocab regex (``None`` = no boost for this vault)."""
    key = str(vault_root)
    now = time.monotonic()
    with _arch_vocab_lock:
        cached = _arch_vocab_cache.get(key)
        if cached is not None and (now - cached[0]) < _ARCH_VOCAB_CACHE_TTL:
            return cached[1]
    terms = search_contract.load_boost_vocab(vault_root)
    pattern = _compile_vocab_regex(terms)
    with _arch_vocab_lock:
        _arch_vocab_cache[key] = (now, pattern)
    return pattern


def _is_architecture_query(query: str) -> bool:
    pattern = _architecture_query_regex(vaults.notes_root())
    if pattern is None:
        return False
    return bool(pattern.search(query or ""))


def clear_architecture_vocab_cache() -> None:
    """Test/reload hook — drop the cached per-vault boost_vocab regex."""
    with _arch_vocab_lock:
        _arch_vocab_cache.clear()


_TICKET_PREFIXES = frozenset(
    {"itops", "plat", "dv", "qa", "harmon", "core-api", "ngi", "devops"}
)
_TICKET_ID_RE = re.compile(r"\b([a-z][a-z0-9-]*?)[-\s]+(\d{2,6})\b", re.I)
_EVAL_ARTIFACT_STEMS = frozenset({"apo-qmd-retrieval-pilot", "search-eval"})
_STEM_PHRASE_STOP = frozenset(
    {"core", "api", "thread", "plat", "the", "and", "for", "gradguard", "integration", "feasibility"}
)


def _phrase_stem_boost(path: str, query: str) -> float:
    """Boost when consecutive query words appear hyphenated in the filename stem."""
    stem = Path(path).stem.lower()
    words = re.findall(r"[a-z0-9]+", (query or "").lower())
    if len(words) < 2:
        return 1.0
    best = 1.0
    for n in (4, 3, 2):
        for i in range(len(words) - n + 1):
            phrase_words = words[i : i + n]
            if sum(1 for w in phrase_words if w not in _STEM_PHRASE_STOP) < 1:
                continue
            hyphen = "-".join(phrase_words)
            if hyphen in stem:
                best = max(best, 1.0 + 0.14 * n)
    return best


_frontmatter_boost_cache: dict[str, tuple[float, dict[str, str]]] = {}
_frontmatter_boost_lock = threading.Lock()


def _frontmatter_boost_fields(path: str) -> dict[str, str]:
    """Cached title/permalink/description from index frontmatter for rank boosts."""
    key = path.replace("\\", "/")
    now = time.monotonic()
    with _frontmatter_boost_lock:
        cached = _frontmatter_boost_cache.get(key)
        if cached is not None and (now - cached[0]) < 30.0:
            return cached[1]
    db = core.reader_connect()
    row = db.execute("SELECT frontmatter FROM files WHERE path=?", (key,)).fetchone()
    fields: dict[str, str] = {}
    if row and row[0]:
        try:
            fm = json.loads(row[0])
            if isinstance(fm, dict):
                for k in ("title", "permalink", "description"):
                    v = fm.get(k)
                    if isinstance(v, str) and v.strip():
                        fields[k] = v.strip()
        except (json.JSONDecodeError, TypeError):
            pass
    with _frontmatter_boost_lock:
        _frontmatter_boost_cache[key] = (now, fields)
    return fields


def _title_frontmatter_boost(path: str, query: str) -> float:
    """Boost when cached title/permalink tokens align with the query (beyond filename)."""
    q_lower = (query or "").lower()
    q_tokens = {t for t in re.findall(r"[a-z0-9]+", q_lower) if len(t) > 2}
    if len(q_tokens) < 2:
        return 1.0
    stem_tokens = set(re.split(r"[-_]", Path(path).stem.lower()))
    stem_overlap = len(q_tokens & stem_tokens) / len(q_tokens)
    if stem_overlap < 0.30:
        return 1.0
    fm = _frontmatter_boost_fields(path)
    best = 1.0
    q_words = re.findall(r"[a-z0-9]+", q_lower)
    for key in ("title", "permalink", "description"):
        text = (fm.get(key) or "").lower()
        if not text:
            continue
        t_tokens = {t for t in re.findall(r"[a-z0-9]+", text) if len(t) > 2}
        if t_tokens:
            overlap = len(q_tokens & t_tokens) / len(q_tokens)
            if overlap >= 0.45:
                best = max(best, 1.0 + 0.16 * overlap)
        for n in (3, 2):
            for i in range(len(q_words) - n + 1):
                if "-".join(q_words[i : i + n]) in text.replace("_", "-"):
                    best = max(best, 1.0 + 0.10 * n)
    return best


def _slug_ticket_boost(path: str, query: str) -> float:
    """Boost when query ticket/slug tokens match the note filename stem."""
    stem = Path(path).stem.lower()
    q = (query or "").lower()
    best = 1.0
    for m in _TICKET_ID_RE.finditer(q):
        prefix = m.group(1).lower().rstrip("-")
        num = m.group(2)
        if prefix not in _TICKET_PREFIXES and not any(prefix.startswith(p) for p in _TICKET_PREFIXES):
            continue
        needle = f"{prefix}-{num}"
        if needle in stem or stem.startswith(needle + "-") or stem == needle:
            best = max(best, 1.38)
    if best > 1.0:
        return best
    q_tokens = {t for t in re.findall(r"[a-z0-9]+", q) if len(t) > 1}
    stem_tokens = set(re.split(r"[-_]", stem))
    if len(q_tokens) >= 2:
        overlap = len(q_tokens & stem_tokens) / len(q_tokens)
        if overlap >= 0.6:
            best = max(best, 1.0 + 0.22 * overlap)
    return best


def _eval_table_demotion(path: str, chunk_kind: str, query: str) -> float:
    """Demote table_row hits from eval/meta notes when filename doesn't match ticket ids."""
    if chunk_kind != "table_row":
        return 1.0
    stem = Path(path).stem.lower()
    if _slug_ticket_boost(path, query) > 1.0:
        return 1.0
    if stem in _EVAL_ARTIFACT_STEMS or stem.endswith("-eval"):
        return 0.62
    return 1.0


# Generic 2-column key/value shapes carry zero note-specific schema information —
# core._collapse_full_tables already embeds the real column list (with note context)
# into the section chunk's own "[table: N rows — cols]" marker, so a table_header
# chunk whose columns are just one of these pairs is a pure "super-matcher" that
# reads as a plausible hit for nearly any query mentioning "field"/"key"/"value"/etc,
# with nothing differentiating it from thousands of other tables in the vault.
_GENERIC_TABLE_HEADER_COL_SETS = frozenset(
    {
        frozenset({"field", "value"}),
        frozenset({"key", "value"}),
        frozenset({"property", "value"}),
        frozenset({"name", "value"}),
    }
)
_TABLE_HEADER_COLS_RE = re.compile(r"Columns:\s*(.+)$")


def _is_generic_table_header_text(text: str) -> bool:
    """True when a table_header chunk's column list is a generic key/value pair.

    Matches ``header_flatten_text``'s ``"{breadcrumb} — Columns: {cols}"`` /
    ``"Columns: {cols}"`` shape (see ``table_markdown.header_flatten_text``) —
    only the trailing column list matters, so this doesn't care whether a
    breadcrumb precedes it.
    """
    m = _TABLE_HEADER_COLS_RE.search(text or "")
    if not m:
        return False
    cols = frozenset(c.strip().lower() for c in m.group(1).split(",") if c.strip())
    return bool(cols) and cols in _GENERIC_TABLE_HEADER_COL_SETS


def _generic_table_header_demotion(chunk_kind: str, text: str) -> float:
    """Demote table_header hits whose columns carry no differentiating content."""
    if chunk_kind != "table_header":
        return 1.0
    return 0.55 if _is_generic_table_header_text(text) else 1.0


_backlink_count_cache: dict[str, tuple[float, int]] = {}
_backlink_count_lock = threading.Lock()


def _backlink_count(path: str) -> int:
    """Distinct inbound wiki-link sources for a path (cached briefly per search)."""
    rel = path.replace("\\", "/").removesuffix(".md")
    key = rel.lower()
    now = time.monotonic()
    with _backlink_count_lock:
        cached = _backlink_count_cache.get(key)
        if cached is not None and (now - cached[0]) < 30.0:
            return cached[1]
    stem = Path(rel).name.lower()
    targets = {key, stem}
    rows = core.list_backlinks(targets, limit=500)
    count = len({r[0] for r in rows})
    with _backlink_count_lock:
        _backlink_count_cache[key] = (now, count)
    return count


def _backlink_search_boost(path: str) -> float:
    """Modest boost for well-linked notes (GBrain-style backlink signal, index-only)."""
    n = _backlink_count(path)
    if n <= 0:
        return 1.0
    return 1.0 + min(0.18, 0.04 * math.log1p(n))


def _prefetch_path_boost_data(paths: Iterable[str]) -> None:
    """Warm frontmatter + backlink caches for a search candidate pool (one SQL batch each)."""
    uniq: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        p = str(raw).replace("\\", "/")
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    if not uniq:
        return
    db = core.reader_connect()
    now = time.monotonic()
    fm_warm: dict[str, dict[str, str]] = {}
    batch_size = 400
    for i in range(0, len(uniq), batch_size):
        chunk = uniq[i : i + batch_size]
        ph = ",".join("?" * len(chunk))
        for path, raw_fm in db.execute(
            f"SELECT path, frontmatter FROM files WHERE path IN ({ph})",
            chunk,
        ):
            fields: dict[str, str] = {}
            if raw_fm:
                try:
                    fm = json.loads(raw_fm)
                    if isinstance(fm, dict):
                        for k in ("title", "permalink", "description"):
                            v = fm.get(k)
                            if isinstance(v, str) and v.strip():
                                fields[k] = v.strip()
                except (json.JSONDecodeError, TypeError):
                    pass
            fm_warm[str(path).replace("\\", "/")] = fields
    with _frontmatter_boost_lock:
        for path, fields in fm_warm.items():
            _frontmatter_boost_cache[path] = (now, fields)

    lookup: dict[str, set[str]] = {}
    boost_keys: set[str] = set()
    for p in uniq:
        rel = p.replace("\\", "/").removesuffix(".md")
        bk = rel.lower()
        boost_keys.add(bk)
        stem = Path(rel).name.lower()
        for lk in (bk, stem):
            lookup.setdefault(lk, set()).add(bk)
    sources: dict[str, set[str]] = {bk: set() for bk in boost_keys}
    keys = list(lookup.keys())
    for i in range(0, len(keys), batch_size):
        chunk = keys[i : i + batch_size]
        ph = ",".join("?" * len(chunk))
        for source, target_key, target_stem in db.execute(
            f"""SELECT source, target_key, target_stem FROM backlinks
                WHERE target_key IN ({ph}) OR target_stem IN ({ph})""",
            chunk + chunk,
        ):
            src = str(source)
            for lk in (str(target_key or "").lower(), str(target_stem or "").lower()):
                for bk in lookup.get(lk, ()):
                    sources[bk].add(src)
    with _backlink_count_lock:
        for bk, srcs in sources.items():
            _backlink_count_cache[bk] = (now, len(srcs))


def _phrase_stem_paths_in_folder(query: str, folder_prefix: str, *, limit: int = 12) -> list[str]:
    """Paths under folder whose stem contains a hyphenated phrase from the query."""
    folder_clean = folder_prefix.replace("\\", "/").strip("/")
    if not folder_clean:
        return []
    words = re.findall(r"[a-z0-9]+", (query or "").lower())
    if len(words) < 2:
        return []
    db = core.reader_connect()
    prefix = core._escape_like(folder_clean) + "/%"
    rows = db.execute(
        "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' AND path LIKE '%.md'",
        (prefix,),
    ).fetchall()
    scored: list[tuple[float, str]] = []
    for (path,) in rows:
        stem = Path(path).stem.lower()
        # Cheap prefilter before hyphenated phrase scan (O(folder) but skips regex work).
        if not any(len(w) > 2 and w in stem for w in words):
            continue
        boost = _phrase_stem_boost(path, query)
        if boost > 1.05:
            scored.append((boost, path))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in scored[:limit]]


def _inject_phrase_stem_hits(
    hits: "list[core.Hit]",
    query: str,
    folder_prefix: str,
    *,
    explain: bool = False,
    full_texts: list[str] | None = None,
) -> "tuple[list[core.Hit], list[str] | None]":
    """Ensure phrase-stem filename matches enter the pool when hybrid search missed them."""
    existing = {h.path for h in hits}
    paths = _phrase_stem_paths_in_folder(query, folder_prefix)
    if not paths:
        return hits, full_texts
    db = core.reader_connect()
    top_score = hits[0].score if hits else 0.55
    added_texts: list[str] = []
    for path in paths:
        if path in existing:
            continue
        row = db.execute(
            """SELECT c.path, c.heading, c.text, c.chunk_hash, c.heading_level,
                      c.start_line, c.end_line, f.mtime, COALESCE(f.bytes, 0),
                      COALESCE(c.section_bytes, LENGTH(c.text)),
                      COALESCE(c.content_hash, ''),
                      COALESCE(c.chunk_kind, 'section'),
                      COALESCE(c.row_key, ''), COALESCE(c.table_id, '')
               FROM chunks c LEFT JOIN files f ON f.path = c.path
               WHERE c.path = ?
               ORDER BY (COALESCE(c.chunk_kind, 'section') != 'section'),
                        c.heading_level ASC, c.start_line ASC
               LIMIT 1""",
            (path,),
        ).fetchone()
        if not row:
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
        path_mult, path_detail = _path_retrieval_boost(path, chunk_kind or "section", query, text=text)
        score = round(top_score * 0.92 * path_mult, 4)
        hit_explain: dict[str, Any] = {}
        if explain:
            hit_explain = {"phrase_stem_inject": True, **path_detail, "final_score": score}
        hits.append(
            core.Hit(
                path=path,
                heading=heading or "",
                text=text,
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
        )
        added_texts.append(text)
        existing.add(path)
    if added_texts and full_texts is not None:
        full_texts = full_texts + added_texts
    if added_texts:
        hits.sort(key=lambda h: h.score, reverse=True)
    return hits, full_texts


def _path_retrieval_boost(
    path: str,
    chunk_kind: str,
    query: str,
    *,
    text: str = "",
) -> tuple[float, dict[str, float]]:
    """Post-fusion multipliers for slug/ticket recall and backlink rank.

    ``text`` is optional (defaults to no-op for the generic-header check) — pass
    the chunk's own text when available so ``table_header`` demotion can inspect
    its column list.
    """
    slug = _slug_ticket_boost(path, query)
    phrase = _phrase_stem_boost(path, query)
    title_fm = _title_frontmatter_boost(path, query)
    demote = _eval_table_demotion(path, chunk_kind, query)
    generic_header = _generic_table_header_demotion(chunk_kind, text)
    bl = _backlink_search_boost(path)
    combined = slug * phrase * title_fm * demote * generic_header * bl
    detail = {
        "slug_boost": slug,
        "phrase_stem_boost": phrase,
        "title_frontmatter_boost": title_fm,
        "table_demotion": demote,
        "generic_table_header_demotion": generic_header,
        "backlink_boost": bl,
    }
    return combined, detail


def _neighbor_paths_for(top_paths: list[str], *, limit: int = 80) -> set[str]:
    """1-hop wiki-link neighbors of top-ranked note paths (index-only)."""
    neighbors: set[str] = set()
    for raw in top_paths[:5]:
        rel = raw.replace("\\", "/")
        if not rel.endswith(".md"):
            rel = f"{rel}.md"
        stem = Path(rel).stem.lower()
        keys = {rel.removesuffix(".md").lower(), stem}
        for src, _, _ in core.list_backlinks(keys, limit=limit):
            neighbors.add(src.replace("\\", "/"))
        for tgt, _, _ in core.list_outlinks(rel, limit=limit):
            t = tgt.replace("\\", "/")
            neighbors.add(t if t.endswith(".md") else f"{t}.md")
    return neighbors


def _neighbor_rank_boost(
    hits: "list[core.Hit]",
    query: str,
    *,
    explain: bool = False,
) -> "list[core.Hit]":
    """Promote 1-hop link neighbors of top hits (GBrain-style graph signal, lite)."""
    if len(hits) < 2:
        return hits
    top_paths = list(dict.fromkeys(h.path for h in hits[:5]))
    neighbor_paths = _neighbor_paths_for(top_paths)
    if not neighbor_paths:
        return hits
    top_set = set(top_paths)
    boosted = False
    for h in hits:
        if h.path in top_set or h.path not in neighbor_paths:
            continue
        overlap = _slug_ticket_boost(h.path, query)
        mult = 1.14 if overlap > 1.0 else 1.08
        h.score = round(h.score * mult, 4)
        if explain:
            h.explain = h.explain or {}
            h.explain["neighbor_boost"] = mult
        boosted = True
    if boosted:
        hits.sort(key=lambda x: x.score, reverse=True)
    return hits


def _apply_path_boosts_to_hits(
    hits: "list[core.Hit]",
    query: str,
    *,
    explain: bool = False,
    neighbor: bool = True,
    folder: str = "",
) -> "list[core.Hit]":
    if not hits:
        return hits
    _prefetch_path_boost_data([h.path for h in hits])
    folder_prefix = folder.replace("\\", "/").strip("/")
    if folder_prefix:
        hits, _ = _inject_phrase_stem_hits(hits, query, folder_prefix, explain=explain)
    for h in hits:
        mult, detail = _path_retrieval_boost(h.path, h.chunk_kind or "section", query, text=h.text)
        h.score = round(h.score * mult, 4)
        if explain:
            h.explain = h.explain or {}
            h.explain.update(detail)
            h.explain["final_score"] = h.score
    hits.sort(key=lambda x: x.score, reverse=True)
    if neighbor:
        hits = _neighbor_rank_boost(hits, query, explain=explain)
    return hits


def diversity_group_key(path: str, chunk_kind: str, table_id: str) -> str:
    """Grouping key for the post-fusion result-diversity cap (see ``core.search``).

    Groups by note path — every ``table_row``/``table_header`` chunk already
    belongs to exactly one path (a table never spans notes), so path is a
    superset-safe grouping: it caps a crowded table *and* caps a note that
    combines a matching prose section with that same table, whereas grouping
    by ``table_id`` alone would let those add up separately and still crowd
    one note's chunks into most of the result list. ``table_id`` is accepted
    (not currently used to subdivide the key) so a finer per-table cap can be
    layered in later without changing every call site.
    """
    del chunk_kind, table_id  # not currently used to subdivide the key — see above
    return f"path:{path}"


def _catalog_retrieval_boost(
    path: str,
    chunk_kind: str,
    folder_prefix: str,
    query: str = "",
) -> float:
    """Post-fusion score multiplier for mermaid catalog diagram recall.

    Catalog-scoped (``folder`` contains ``mermaid-catalog``): always apply
    path/chunk_kind boosts and pages demotion.

    Vault-wide (empty ``folder``): same shape, stronger multipliers, but only
    when ``query`` matches this vault's ``boost_vocab`` architecture terms —
    so policy prose does not silently lose every CDE/payment question to
    diagrams on unrelated searches, and a vault with no ``boost_vocab`` never
    triggers this branch at all.
    """
    norm = path.replace("\\", "/")
    in_catalog = "mermaid-catalog" in norm
    folder_scoped = bool(
        folder_prefix and "mermaid-catalog" in folder_prefix.replace("\\", "/")
    )
    vault_wide = (not folder_prefix) and in_catalog and _is_architecture_query(query)
    if not folder_scoped and not vault_wide:
        return 1.0

    # Vault-wide needs a larger nudge — competitors are the whole vault.
    d_mmd = 1.35 if vault_wide else 1.18
    d_kind = 1.22 if vault_wide else 1.10
    d_pages = 0.70 if vault_wide else 0.82
    d_pages_row = 0.55 if vault_wide else 0.75

    boost = 1.0
    if norm.endswith("/diagram.mmd") or norm.endswith("diagram.mmd"):
        boost *= d_mmd
    if chunk_kind in ("mermaid_file", "mermaid_header", "mermaid_node", "mermaid_edge"):
        boost *= d_kind
    if "/pages/" in norm:
        boost *= d_pages
    if chunk_kind == "table_row" and "/pages/" in norm:
        boost *= d_pages_row
    return boost
