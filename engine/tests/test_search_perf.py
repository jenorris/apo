"""Search performance regressions (hybrid=False routing, embed cache)."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from apo_engine import config, core, embeddings, ops, ranking, vaults


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


class SearchPerfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        thread = self.vault / "areas" / "threads"
        thread.mkdir(parents=True)
        (thread / "alpha-note.md").write_text(
            "# Alpha\n\nalpha widget keyword content\n", encoding="utf-8"
        )
        (thread / "beta-note.md").write_text(
            "# Beta\n\nbeta unrelated content\n", encoding="utf-8"
        )
        (self.vault / "system" / "contracts").mkdir(parents=True)
        (self.vault / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "layout:\n  areas: standing work areas\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "search_perf_test"),
            mock.patch.object(config, "QUERY_EMBED_DISK_CACHE", True),
            mock.patch.object(core, "embed", _fake_embed),
        ]
        for patch in self._patches:
            patch.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        vaults._usage_layout_cache.clear()
        core.clear_query_embed_cache()
        ops._lint_sweep_cache.clear()
        ranking._frontmatter_boost_cache.clear()
        ranking._backlink_count_cache.clear()
        note_lint = __import__("apo_engine.note_lint", fromlist=["note_lint"])
        note_lint._wiki_index_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hybrid_false_skips_query_embed(self):
        with mock.patch.object(core, "query_embed", side_effect=AssertionError("embed called")):
            out = ops.search("alpha widget", folder="areas/threads", hybrid=False, limit=3)
        self.assertTrue(out["ok"])
        self.assertTrue(out["results"])
        self.assertEqual(out["results"][0]["source"], "areas/threads/alpha-note.md")

    def test_query_embed_disk_cache_roundtrip(self):
        core.clear_query_embed_cache()
        with mock.patch.object(core, "embed", wraps=_fake_embed) as emb:
            first = core.query_embed("cache me roundtrip")
            with core._query_embed_lock:
                core._query_embed_cache.clear()
            second = core.query_embed("cache me roundtrip")
        self.assertEqual(len(first), len(second))
        for a, b in zip(first, second):
            self.assertAlmostEqual(a, b, places=6)  # sidecar stores float32
        self.assertEqual(emb.call_count, 1)

    def test_concurrent_query_embed_misses_share_one_backend_call(self):
        core.clear_query_embed_cache()
        workers = 8
        barrier = threading.Barrier(workers)
        backend_started = threading.Event()
        release_backend = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def blocked_embed(texts, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            backend_started.set()
            self.assertTrue(release_backend.wait(1.0))
            return _fake_embed(texts, **kwargs)

        def query():
            barrier.wait()
            return core.query_embed("one concurrent query")

        with mock.patch.object(core, "embed", blocked_embed):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(query) for _ in range(workers)]
                self.assertTrue(backend_started.wait(1.0))
                release_backend.set()
                results = [future.result() for future in futures]

        self.assertEqual(calls, 1)
        self.assertTrue(all(result == results[0] for result in results[1:]))

    def test_failed_query_embed_flight_reaches_waiters_and_is_retryable(self):
        core.clear_query_embed_cache()
        workers = 8
        barrier = threading.Barrier(workers)
        release_backend = threading.Event()
        all_flight_lookups = threading.Event()
        lookup_lock = threading.Lock()

        class CountingFlights(dict):
            lookups = 0

            def get(self, key, default=None):
                with lookup_lock:
                    self.lookups += 1
                    if self.lookups == workers:
                        all_flight_lookups.set()
                return super().get(key, default)

        def failing_embed(*args, **kwargs):
            self.assertTrue(release_backend.wait(1.0))
            raise RuntimeError("backend down")

        def query():
            barrier.wait()
            return core.query_embed("retry after failure")

        with (
            mock.patch.object(embeddings, "_query_embed_inflight", CountingFlights()),
            mock.patch.object(core, "embed", failing_embed),
            ThreadPoolExecutor(max_workers=workers) as pool,
        ):
            futures = [pool.submit(query) for _ in range(workers)]
            self.assertTrue(all_flight_lookups.wait(1.0))
            release_backend.set()
            for future in futures:
                with self.assertRaisesRegex(RuntimeError, "backend down"):
                    future.result()

        with mock.patch.object(core, "embed", wraps=_fake_embed) as embed:
            self.assertTrue(core.query_embed("retry after failure"))
        self.assertEqual(embed.call_count, 1)

    def test_prefetch_path_boost_data_warms_caches(self):
        path = "areas/threads/alpha-note.md"
        ranking._frontmatter_boost_cache.clear()
        ranking._backlink_count_cache.clear()
        ranking._prefetch_path_boost_data([path])
        with mock.patch.object(core, "reader_connect") as rc:
            ranking._frontmatter_boost_fields(path)
            ranking._backlink_count(path)
            rc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
