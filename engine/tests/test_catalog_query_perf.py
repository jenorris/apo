"""Query-path perf fixes: the fast paths must return exactly what the old code returned.

Each class keeps a verbatim copy of the pre-optimization behavior as an oracle and
compares it with the live implementation: ``filter_notes`` frontmatter matching,
folder-scoped search helpers, the bounded ranking caches, ``search_expanded`` embed
concurrency, and ``search_at_ref`` paging.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import random
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from functools import cmp_to_key
from pathlib import Path
from unittest import mock

from apo_engine import config, core, link_index, ranking, ref_catalog, retrieval, vaults

_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


# --------------------------------------------------------------------------- #
# filter_notes: SQL prefilter + Python matcher == Python matcher alone
# --------------------------------------------------------------------------- #
_VALUES: list = [
    "active", "Active", " active ", "done", "5", "5.0", "1e3", "true", "True", "none", "None",
    "", "{'a': 1}", "2026-01-05", "2026-02-10", "2025-12-31", "abc", "é",
    5, 7, 5.0, 1000.0, 0, True, False, None,
    ["active"], ["Active", "x"], ["done", "y"], ["x"], [], [1, "active"], [None], [5],
    [{"name": "a", "v": 3}, {"name": "b", "v": 9}], [{"name": "c"}], ["x", {"name": "a"}],
    {"a": 1}, {"owner": "jeremy"},
]
_KEYS = ["status", "tags", "count", "date", "items"]


def _legacy_filter(db, where, folder="", limit=20, offset=0, sort="mtime", order="desc"):
    """Pre-optimization Python path: fetchall, match, full sort, slice."""
    sort_key, order_key = retrieval._normalize_filter_sort(sort, order)
    parts, params = ["frontmatter IS NOT NULL"], []
    folder_prefix = folder.replace("\\", "/").strip("/")
    if folder_prefix:
        parts.append("path LIKE ? ESCAPE '\\'")
        params.append(retrieval.escape_like(folder_prefix) + "/%")
    rows = db.execute(
        f"SELECT path, mtime, frontmatter FROM files WHERE {' AND '.join(parts)}", params
    ).fetchall()
    matches = []
    for path, mtime, fm_json in rows:
        try:
            fm = json.loads(fm_json) if fm_json else {}
        except json.JSONDecodeError:
            fm = {}
        if all(retrieval._match_where_clause(fm, k, c) for k, c in where.items()):
            matches.append((mtime, path, fm))
    matches.sort(
        key=cmp_to_key(
            lambda a, b: retrieval._filter_notes_cmp(a, b, sort=sort_key, order=order_key)
        )
    )
    return len(matches), matches[offset : offset + limit]


def _filter_fixture(n: int = 400) -> sqlite3.Connection:
    rng = random.Random(7)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, mtime REAL, frontmatter TEXT)")
    for i in range(n):
        fm = {k: rng.choice(_VALUES) for k in _KEYS if rng.random() < 0.8}
        if rng.random() < 0.3:
            fm["meta"] = {"owner": rng.choice(["jeremy", "Jeremy", "fotini"])}
        folder = rng.choice(["areas", "areas/threads", "projects", "Projects"])
        # Deliberate mtime ties: ordering among equal mtimes must follow scan order.
        db.execute(
            "INSERT INTO files VALUES (?, ?, ?)",
            (f"{folder}/n{i:04d}.md", float(rng.randint(1, 60)), json.dumps(fm)),
        )
    odd = [
        ("odd/none.md", 1.0, None),
        ("odd/empty-obj.md", 3.0, "{}"),
        ("odd/broken.md", 4.0, "{not json"),
        ("odd/nan.md", 5.0, '{"count": NaN, "status": "active"}'),
    ]
    db.executemany("INSERT INTO files VALUES (?, ?, ?)", odd)
    return db


def _where_cases() -> list[dict]:
    cases: list[dict] = []
    scalars = [v for v in _VALUES if not isinstance(v, (list, dict))]
    for key in ("status", "tags", "count", "date"):
        for v in scalars:
            cases.append({key: v})
            cases.append({key: {"$eq": v}})
            cases.append({key: {"$ne": v}})
    cases += [{"tags": ["active"]}, {"items": [{"name": "a", "v": 3}]}]
    for key in ("status", "tags", "count"):
        for rhs in (
            ["active", "done"], ["Active"], ["5"], ["x", "y"], ["{'a': 1}"], [""],
            ["true"], ["active", 5], [5, 7], [], "active", None,
        ):
            cases.append({key: {"$in": rhs}})
    for op in ("$lt", "$lte", "$gt", "$gte"):
        for rhs in ("2026-01-05", "2026-02", "5", "5.0", "abc", "", 5, 5.5, "nan", None):
            cases.append({"date": {op: rhs}})
            cases.append({"count": {op: rhs}})
    cases += [{"date": {"$gte": "2026-01-01", "$lt": "2026-02-01"}}]
    for rhs in ("x", "act", "Active", "5", 5, None):
        cases.append({"tags": {"$contains": rhs}})
        cases.append({"status": {"$contains": rhs}})
    cases += [
        {"items": {"$elemMatch": {"name": "a"}}},
        {"items": {"$elemMatch": {"v": {"$gt": 4}}}},
        {"items": {"$elemMatch": {}}},
        {"tags": {"$elemMatch": {"name": "a"}}},
        {"meta.owner": "jeremy"},
        {"meta.owner": {"$in": ["jeremy", "fotini"]}},
        {"items[name=a].v": {"$gt": 1}},
        {"status": {"$exists": True}, "tags": "x"},
        {"status": {"$exists": False}, "count": {"$gt": 1}},
        {"status": {"$exists": True, "$ne": "done"}},
        {"status": {"$exists": False, "$ne": "done"}},
        {"status": {}},
        {"status": {"$bogus": 1}},
        {"status": "active", "tags": {"$in": ["x", "done"]}, "count": {"$gte": 0}},
        {"status": "active", "meta.owner": "jeremy"},
        {"status": "Active", "date": {"$lt": "2026-02-01"}},
    ]
    return cases


class FilterNotesPrefilterParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = _filter_fixture()

    def _assert_same(self, where, **kw):
        want = _legacy_filter(self.db, where, **kw)
        got = retrieval._filter_notes_from_table(self.db, "files", where, **kw)
        self.assertEqual(got, want, msg=f"where={where!r} kw={kw!r}")

    def test_every_case_matches_legacy_full_page(self):
        for where in _where_cases():
            if retrieval._sql_pushdown_predicates(where) is not None:
                continue  # $exists-only clauses never took the Python path
            self._assert_same(where, limit=10_000)

    def test_paging_sorting_and_folder_scopes_match_legacy(self):
        wheres = [
            {"status": "active"},
            {"tags": {"$in": ["x", "done"]}},
            {"status": {"$ne": "done"}},
            {"date": {"$gte": "2026-01-01"}},
            {"items": {"$elemMatch": {"name": "a"}}},
            {"meta.owner": "jeremy"},
        ]
        combos = itertools.product(
            wheres,
            [("mtime", "desc"), ("mtime", "asc"), ("date", "asc"), ("count", "desc")],
            [(3, 0), (5, 4), (0, 0), (1000, 2)],
            ["", "areas", "Projects/"],
        )
        for where, (sort, order), (limit, offset), folder in combos:
            self._assert_same(
                where, sort=sort, order=order, limit=limit, offset=offset, folder=folder
            )

    def test_prefilter_actually_narrows_parsing(self):
        total = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        # Ranges admit every non-text value (str() forms differ), so they narrow far less.
        for where, bound in (
            ({"status": "active"}, 0.7),
            ({"tags": {"$in": ["x", "done"]}}, 0.7),
            ({"date": {"$gt": "2026-01-01"}}, 1.0),
        ):
            with mock.patch.object(
                retrieval, "_loads_frontmatter", wraps=retrieval._loads_frontmatter
            ) as loads:
                retrieval._filter_notes_from_table(self.db, "files", where)
            self.assertLess(loads.call_count, total * bound, where)

    def test_unsafe_keys_and_dotted_paths_are_not_prefiltered(self):
        clauses, _ = retrieval._fm_prefilter_predicates(
            {"meta.owner": "jeremy", "items[name=a].v": 1, "bad key": "x", "ok": {"$exists": True}}
        )
        self.assertEqual(clauses, [])

    def test_ref_files_table_uses_same_matcher(self):
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE ref_files (tree_oid TEXT, path TEXT, mtime REAL, frontmatter TEXT,"
            " PRIMARY KEY (tree_oid, path))"
        )
        rows = [("t1", f"n{i}.md", float(i % 4), json.dumps({"status": s})) for i, s in enumerate(
            ["active", "Active", "done", "active", "x"]
        )] + [("t2", "other.md", 1.0, json.dumps({"status": "active"}))]
        db.executemany("INSERT INTO ref_files VALUES (?, ?, ?, ?)", rows)
        total, page = retrieval._filter_notes_from_table(
            db, "ref_files", {"status": "active"}, tree_oid="t1"
        )
        self.assertEqual(total, 2)
        self.assertEqual({p[1] for p in page}, {"n0.md", "n3.md"})


# --------------------------------------------------------------------------- #
# Index-backed fixtures (folder search, caches, search_expanded)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _indexed_vault(root: Path, files: dict[str, str], name: str):
    vault = root / name
    for rel, body in files.items():
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    patches = [
        mock.patch.object(config, "NOTES_ROOT", vault),
        mock.patch.object(config, "INDEX_PATH", root / f"{name}.db"),
        mock.patch.object(config, "COLLECTION", f"perf_{name}"),
        mock.patch.object(core, "embed", _fake_embed),
    ]
    for p in patches:
        p.start()
    try:
        vaults._usage_layout_cache.clear()
        core.index_vault(rebuild=True, verbose=False)
        yield vault
    finally:
        for p in reversed(patches):
            p.stop()
        vaults._usage_layout_cache.clear()


_THREAD_FILES = {
    "areas/threads/alpha-rippling-scim.md": "---\ntitle: Alpha Rippling SCIM\n---\n\n# Alpha\n\n"
    "scim rippling provisioning notes. Links [[beta-widget]] and [[ghost-note]].\n",
    "areas/threads/beta-widget.md": "---\ntitle: Beta Widget\n---\n\n# Beta\n\n"
    "widget keyword content about scim provisioning for the rippling vendor, "
    + "filler " * 80
    + "\n\n## Second\n\nmore rippling scim detail here\n",
    "areas/threads/gamma-note.md": "---\ntitle: Gamma\n---\n\n# Gamma\n\n"
    "points at [[alpha-rippling-scim]] about rippling scim\n",
    "areas/threads/delta.v2.md": "# Delta\n\nrippling scim delta widget\n",
    "areas/other/epsilon.md": "# Epsilon\n\nrippling widget outside the folder\n",
    "root-note.md": "# Root\n\nlinks to [[alpha-rippling-scim]] and [[gamma-note]]\n",
}


class _IndexedTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._stack.callback(core.clear_query_embed_cache)
        for cache in (
            ranking._frontmatter_boost_cache,
            ranking._backlink_count_cache,
            ranking._folder_stems_cache,
        ):
            cache.clear()

    def index(self, files=None, name="v"):
        return self._stack.enter_context(_indexed_vault(self.tmp, files or _THREAD_FILES, name))


class FolderSearchOverheadTest(_IndexedTestCase):
    def test_stem_lower_matches_pathlib(self):
        for p in ("a/b/c.md", ".md", "x/.md", "a.b.md", "dir.x/name.md", "noext", "a/b.", "A/Mixed-Case.MD"):
            self.assertEqual(ranking._stem_lower(p), Path(p).stem.lower(), p)

    def test_phrase_stem_paths_match_legacy_and_cache_the_folder(self):
        self.index()

        def legacy(query, folder, limit=12):
            db = ranking.index_db.reader_connect()
            rows = db.execute(
                "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' AND path LIKE '%.md'",
                (retrieval.escape_like(folder) + "/%",),
            ).fetchall()
            scored = []
            for (path,) in rows:
                stem = Path(path).stem.lower()
                if not any(len(w) > 2 and w in stem for w in re.findall(r"[a-z0-9]+", query.lower())):
                    continue
                boost = ranking._phrase_stem_boost(path, query)
                if boost > 1.05:
                    scored.append((boost, path))
            scored.sort(key=lambda t: t[0], reverse=True)
            return [p for _, p in scored[:limit]]

        for query, folder in [
            ("alpha rippling scim", "areas/threads"),
            ("beta widget", "areas/threads/"),
            ("delta v2", "areas/threads"),
            ("rippling scim", "areas"),
            ("single", "areas"),
            ("an of", "areas"),
        ]:
            want = legacy(query, folder.strip("/"))
            self.assertEqual(ranking._phrase_stem_paths_in_folder(query, folder), want, query)
        self.assertTrue(ranking._phrase_stem_paths_in_folder("alpha rippling scim", "areas/threads"))

        with mock.patch.object(
            ranking.index_db, "reader_connect", side_effect=AssertionError("SQL on cached folder")
        ):
            self.assertTrue(ranking._phrase_stem_paths_in_folder("beta widget", "areas/threads"))

    def test_folder_stem_cache_is_scoped_per_index(self):
        self.index({"areas/one-two.md": "# a\n\nbody\n"}, name="va")
        self.assertEqual(
            ranking._phrase_stem_paths_in_folder("one two", "areas"), ["areas/one-two.md"]
        )
        with self.subTest("other vault, same folder"):
            self._stack.close()
            self._stack = contextlib.ExitStack()
            self.index({"areas/three-four.md": "# b\n\nbody\n"}, name="vb")
            self.assertEqual(ranking._phrase_stem_paths_in_folder("one two", "areas"), [])
            self.assertEqual(
                ranking._phrase_stem_paths_in_folder("three four", "areas"), ["areas/three-four.md"]
            )

    def test_neighbor_paths_match_per_path_queries(self):
        self.index()

        def legacy(top_paths, limit=80):
            neighbors = set()
            for raw in top_paths[:5]:
                rel = raw.replace("\\", "/")
                if not rel.endswith(".md"):
                    rel = f"{rel}.md"
                keys = {rel.removesuffix(".md").lower(), Path(rel).stem.lower()}
                for src, _, _ in link_index.list_backlinks(keys, limit=limit, target_path=rel):
                    neighbors.add(src.replace("\\", "/"))
                for row in link_index.list_links(source=rel, limit=limit):
                    if row["target_path"]:
                        neighbors.add(row["target_path"])
                    elif row["status"] == "unresolved":
                        t = row["target_key"].replace("\\", "/")
                        neighbors.add(t if t.endswith(".md") else f"{t}.md")
            return neighbors

        groups = [
            ["areas/threads/alpha-rippling-scim.md"],
            ["areas/threads/alpha-rippling-scim.md", "areas/threads/beta-widget.md", "root-note"],
            ["areas/threads/gamma-note.md", "areas/threads/not-indexed.md", "areas/other/epsilon.md"],
            [f"areas/threads/n{i}.md" for i in range(8)],
            [],
        ]
        for top in groups:
            for limit in (80, 1):
                self.assertEqual(
                    ranking._neighbor_paths_for(top, limit=limit), legacy(top, limit), (top, limit)
                )
        self.assertTrue(ranking._neighbor_paths_for(groups[0]))

    def test_list_links_many_equals_list_links(self):
        self.index()
        queries = [
            {"source": "areas/threads/alpha-rippling-scim.md", "limit": 10},
            {"target_path": "areas/threads/alpha-rippling-scim.md", "target_keys": {"alpha-rippling-scim"}, "limit": 10},
            {"target_keys": set(), "limit": 5},
            {"target_keys": {"ghost-note"}, "limit": 5},
        ]
        self.assertEqual(
            link_index.list_links_many(queries), [link_index.list_links(**q) for q in queries]
        )

    def test_snippet_excerpts_are_batched_and_identical(self):
        self.index()
        kinds = {}
        for hybrid in (True,):
            for folder in ("", "areas/threads"):
                with mock.patch.object(
                    core, "_fts_snippet_excerpt", wraps=retrieval._fts_snippet_excerpt
                ) as per_hit:
                    with mock.patch.object(core, "_batched_excerpts", return_value=None):
                        want = core.search("rippling scim widget", k=6, folder=folder, snippet_chars=90, hybrid=hybrid)
                    legacy_calls = per_hit.call_count
                with mock.patch.object(
                    core, "_fts_snippet_excerpt", side_effect=AssertionError("per-hit FTS query")
                ):
                    got = core.search("rippling scim widget", k=6, folder=folder, snippet_chars=90, hybrid=hybrid)
                self.assertGreater(legacy_calls, 1)
                self.assertEqual([(h.chunk_hash, h.text, h.score) for h in got],
                                 [(h.chunk_hash, h.text, h.score) for h in want])
                kinds[folder] = len(got)
        self.assertTrue(all(kinds.values()))

    def test_hits_for_ids_snippets_are_batched_and_identical(self):
        self.index()
        want = core.search_lex_only("rippling scim", k=6, snippet_chars=80)
        with mock.patch.object(
            core, "_fts_snippet_excerpt", side_effect=AssertionError("per-hit FTS query")
        ):
            got = core.search_lex_only("rippling scim", k=6, snippet_chars=80)
        self.assertEqual([h.text for h in got], [h.text for h in want])
        with mock.patch.object(core, "_batched_excerpts", return_value=None):
            legacy = core.search_lex_only("rippling scim", k=6, snippet_chars=80)
        self.assertEqual([h.text for h in got], [h.text for h in legacy])


class BoostCacheTest(_IndexedTestCase):
    def test_caches_are_lru_bounded(self):
        with mock.patch.object(ranking, "_BOOST_CACHE_MAX", 4):
            cache = ranking._backlink_count_cache
            with ranking._backlink_count_lock:
                for i in range(4):
                    ranking._boost_cache_put_locked(cache, ("ix", f"k{i}"), (time.monotonic(), i))
            self.assertIsNotNone(
                ranking._boost_cache_get(cache, ranking._backlink_count_lock, ("ix", "k0"), time.monotonic())
            )  # touching k0 makes k1 the oldest
            with ranking._backlink_count_lock:
                ranking._boost_cache_put_locked(cache, ("ix", "k4"), (time.monotonic(), 4))
            self.assertEqual(len(cache), 4)
            self.assertNotIn(("ix", "k1"), cache)
            self.assertIn(("ix", "k0"), cache)

    def test_real_lookups_stay_bounded(self):
        self.index()
        paths = [p for (p,) in ranking.index_db.reader_connect().execute("SELECT path FROM files")]
        self.assertGreater(len(paths), 4)
        with mock.patch.object(ranking, "_BOOST_CACHE_MAX", 3):
            for p in paths:
                ranking._frontmatter_boost_fields(p)
                ranking._backlink_count(p)
            ranking._prefetch_path_boost_data(paths)
        self.assertLessEqual(len(ranking._frontmatter_boost_cache), 3)
        self.assertLessEqual(len(ranking._backlink_count_cache), 3)

    def test_same_relative_path_in_two_vaults_does_not_collide(self):
        files_a = {"areas/same.md": "---\ntitle: Title From A\n---\n\n# A\n\nbody a\n",
                   "areas/linker.md": "# L\n\nsee [[same]]\n"}
        files_b = {"areas/same.md": "---\ntitle: Title From B\n---\n\n# B\n\nbody b\n"}
        path = "areas/same.md"
        with _indexed_vault(self.tmp, files_a, "va"):
            self.assertEqual(ranking._frontmatter_boost_fields(path)["title"], "Title From A")
            self.assertEqual(ranking._backlink_count(path), 1)
        with _indexed_vault(self.tmp, files_b, "vb"):
            self.assertEqual(ranking._frontmatter_boost_fields(path)["title"], "Title From B")
            self.assertEqual(ranking._backlink_count(path), 0)
        with _indexed_vault(self.tmp, files_a, "va"):
            self.assertEqual(ranking._frontmatter_boost_fields(path)["title"], "Title From A")


class _InlinePool:
    """Runs submitted work immediately on the caller thread (the pre-change serial order)."""

    def submit(self, fn, *args):
        fut: Future = Future()
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - mirror Executor.submit
            fut.set_exception(exc)
        return fut


class SearchExpandedEmbedConcurrencyTest(_IndexedTestCase):
    SUBQUERIES = [
        {"type": "lex", "query": "rippling scim", "weight": 2.0},
        {"type": "vec", "query": "rippling scim provisioning", "weight": 2.0},
        {"type": "hyde", "query": "a note about widget keyword content", "weight": 1.0},
        {"type": "vec", "query": "rippling scim provisioning", "weight": 1.0},  # repeated text
        {"type": "vec", "query": "gamma points at alpha", "weight": 1.0},
        {"type": "lex", "query": "widget", "weight": 1.0},
        {"type": "vec", "query": "   ", "weight": 1.0},  # blank: skipped
    ]
    LATENCY = 0.05

    def _slow_embed(self, text):
        time.sleep(self.LATENCY)
        return _fake_embed([text])[0]

    def _run(self, pool):
        calls = []
        lock = threading.Lock()

        def embed(text):
            with lock:
                calls.append(text)
            return self._slow_embed(text)

        with (
            mock.patch.object(core, "query_embed", embed),
            mock.patch.object(core, "_search_pool", pool),
            mock.patch.object(config, "RERANK", False),
        ):
            start = time.perf_counter()
            hits = core.search_expanded(queries=self.SUBQUERIES, k=8, explain=True)
            elapsed = time.perf_counter() - start
        return hits, elapsed, calls

    def test_same_fused_output_and_embeds_overlap(self):
        self.index()
        serial_hits, serial_s, serial_calls = self._run(_InlinePool())
        pool_hits, pool_s, pool_calls = self._run(retrieval.ThreadPoolExecutor(max_workers=4))

        def key(hits):
            return [(h.chunk_hash, h.path, h.score, h.text, h.explain) for h in hits]

        self.assertTrue(serial_hits)
        self.assertEqual(key(pool_hits), key(serial_hits))
        self.assertEqual(sorted(set(pool_calls)), sorted(set(serial_calls)))
        self.assertEqual(len(pool_calls), 3)  # repeated sub-query text embeds once
        self.assertLess(pool_s, serial_s * 0.75, (pool_s, serial_s))

    def test_embed_failure_propagates(self):
        self.index()
        with (
            mock.patch.object(core, "query_embed", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            core.search_expanded(queries=[{"type": "vec", "query": "x y", "weight": 1.0}], k=3)

    def test_unavailable_embedding_skips_subquery(self):
        self.index()
        with mock.patch.object(core, "query_embed", return_value=None):
            out = core.search_expanded(
                queries=[
                    {"type": "vec", "query": "rippling", "weight": 1.0},
                    {"type": "lex", "query": "rippling scim", "weight": 1.0},
                ],
                k=3,
            )
        self.assertTrue(out)


# --------------------------------------------------------------------------- #
# search_at_ref: SQL excludes + LIMIT/OFFSET == fetchall + Python excludes + slice
# --------------------------------------------------------------------------- #
def _legacy_search_at_ref(db, query, tree_oid, *, folder="", exclude=None, k=8, offset=0, snippet_chars=240):
    match = retrieval.fts_query(query)
    if not match:
        return [], 0
    prefixes, globs = retrieval._compile_excludes(exclude)
    folder_prefix = folder.replace("\\", "/").strip("/")
    where = ["tree_oid = ?", "ref_fts MATCH ?"]
    params = [tree_oid, match]
    if folder_prefix:
        where.append("path LIKE ? ESCAPE '\\'")
        params.append(retrieval.escape_like(folder_prefix) + "/%")
    tokens = max(1, min(int(snippet_chars) // 6, 64)) if snippet_chars else 64
    rows = db.execute(
        f"SELECT path, mtime, snippet(ref_fts, 3, '', '', ' … ', ?), rank "
        f"FROM ref_fts WHERE {' AND '.join(where)} ORDER BY rank",
        [tokens, *params],
    ).fetchall()
    hits = []
    for path, mtime, snip, rank in rows:
        rel = str(path or "")
        if retrieval._path_excluded(rel, prefixes, globs):
            continue
        text = str(snip or "")
        if snippet_chars and len(text) > snippet_chars:
            text = text[:snippet_chars].rstrip() + "…"
        hits.append(
            {"path": rel, "content": text, "score": 1.0 / (1.0 + abs(float(rank or 0.0))),
             "mtime": float(mtime or 0.0)}
        )
    return hits[offset : offset + k], len(hits)


class SearchAtRefPagingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = ref_catalog.connect(self.tmp / "refs.db")
        self.addCleanup(self.db.close)
        folders = ["projects", "Projects", "archive", "areas/threads", "areas/people", "top"]
        for i in range(60):
            # Distinct term counts + lengths -> distinct bm25 ranks (no tie-order ambiguity).
            body = " ".join(["widget"] * (1 + i % 7) + [f"filler{j}" for j in range(i)])
            name = f"{folders[i % len(folders)]}/{'draft-' if i % 5 == 0 else ''}note{i}{'.tmp' if i % 9 == 0 else ''}.md"
            self.db.execute("INSERT INTO ref_fts VALUES (?, ?, ?, ?)", ("t1", name, float(i), body))
            self.db.execute("INSERT INTO ref_fts VALUES (?, ?, ?, ?)", ("t2", name, 1.0, "widget"))
        self.db.commit()
        patcher = mock.patch.object(ref_catalog, "reader_connect", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_matches_legacy_for_prefix_glob_folder_and_paging(self):
        excludes = [
            None,
            [],
            ["projects/*"],
            ["projects/*", "archive/*"],
            ["*/draft-*"],
            ["*.tmp.md"],
            ["projects/*", "*/draft-*", "areas/people/*"],
            ["areas/*"],  # prefix form
            ["areas/th?eads/*"],  # glob form
        ]
        for exclude, folder, (k, offset), snippet_chars in itertools.product(
            excludes, ["", "areas", "areas/threads/"], [(8, 0), (5, 5), (100, 0), (3, 40), (0, 0), (4, 500)], [240, 0, 30]
        ):
            want = _legacy_search_at_ref(
                self.db, "widget", "t1", folder=folder, exclude=exclude, k=k, offset=offset,
                snippet_chars=snippet_chars,
            )
            got = retrieval.search_at_ref(
                "widget", "t1", folder=folder, exclude=exclude, k=k, offset=offset,
                snippet_chars=snippet_chars,
            )
            self.assertEqual(got, want, (exclude, folder, k, offset, snippet_chars))

    def test_prefix_excludes_stay_case_sensitive(self):
        _, total_all = retrieval.search_at_ref("widget", "t1", k=1)
        _, total_lower = retrieval.search_at_ref("widget", "t1", k=1, exclude=["projects/*"])
        self.assertLess(total_lower, total_all)
        page, _ = retrieval.search_at_ref("widget", "t1", k=100, exclude=["projects/*"])
        self.assertTrue(any(h["path"].startswith("Projects/") for h in page))

    def test_empty_query_and_unknown_tree(self):
        self.assertEqual(retrieval.search_at_ref("", "t1"), ([], 0))
        self.assertEqual(retrieval.search_at_ref("widget", "nope"), ([], 0))


if __name__ == "__main__":
    unittest.main()
