"""Concurrent embed pipeline, query-embed sidecar cache, and grouped vault flush."""

from __future__ import annotations

import random
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from test_core import VaultTestCase, _fake_embed  # noqa: E402


class _Recorder:
    """Fake ``_server_embed_request``: random latency, tracks peak in-flight requests."""

    def __init__(self, poison: str | None = None):
        self.poison = poison
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0

    def __call__(self, texts, **kwargs):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(random.uniform(0.01, 0.03))
            return [[float("nan")] if t == self.poison else [float(t)] for t in texts]
        finally:
            with self.lock:
                self.inflight -= 1


class EmbedConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(config, "EMBED_BACKEND", "ollama"),
            mock.patch.object(config, "EMBED_COMMIT_BATCH", 3),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _run(self, concurrency: int, texts: list[str], poison: str | None = None):
        rec = _Recorder(poison)
        with mock.patch.object(config, "EMBED_CONCURRENCY", concurrency), \
             mock.patch.object(core, "_server_embed_request", rec):
            out = core._embed_ollama(texts)
        return out, rec

    def test_order_preserved_under_concurrency(self):
        texts = [str(i) for i in range(30)]
        out, rec = self._run(4, texts)
        self.assertEqual(out, [[float(i)] for i in range(30)])
        self.assertGreater(rec.peak, 1)
        self.assertLessEqual(rec.peak, 4)

    def test_concurrency_one_is_serial_and_identical(self):
        texts = [str(i) for i in range(30)]
        serial, rec = self._run(1, texts)
        parallel, _ = self._run(4, texts)
        self.assertEqual(rec.peak, 1)
        self.assertEqual(serial, parallel)

    def test_nan_bisection_isolates_poisoned_input(self):
        texts = [str(i) for i in range(20)]
        out, _ = self._run(2, texts, poison="13")
        self.assertIsNone(out[13])
        self.assertEqual(
            [v for i, v in enumerate(out) if i != 13],
            [[float(i)] for i in range(20) if i != 13],
        )

    def test_fastembed_stays_serial(self):
        with mock.patch.object(config, "EMBED_BACKEND", "fastembed"), \
             mock.patch.object(config, "EMBED_CONCURRENCY", 8):
            self.assertEqual(core.embed_concurrency(), 1)

    def test_worker_error_propagates(self):
        def boom(part):
            if part == [2]:
                raise RuntimeError("backend down")
            return part

        with mock.patch.object(config, "EMBED_CONCURRENCY", 2):
            with self.assertRaises(RuntimeError):
                list(core.iter_embedded_in_order(boom, [[0], [1], [2], [3], [4]]))


class PipelinedStoreTest(VaultTestCase):
    def setUp(self):
        super().setUp()
        self._cfg = {k: getattr(config, k) for k in ("EMBED_BACKEND", "EMBED_COMMIT_BATCH", "EMBED_CONCURRENCY")}
        config.EMBED_BACKEND = "ollama"
        config.EMBED_COMMIT_BATCH = 2
        for i in range(12):
            self.write(f"n{i:02d}.md", f"# Note {i}\n\nalpha {i} body\n\n## Two\n\nbeta {i} tail\n")

    def tearDown(self):
        for k, v in self._cfg.items():
            setattr(config, k, v)
        super().tearDown()

    def _rows(self):
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            return db.execute("SELECT path, ord, text, embedding FROM chunks ORDER BY id").fetchall()
        finally:
            db.close()

    def test_concurrent_matches_serial_and_writes_on_caller_thread(self):
        def slow_embed(texts, **kwargs):
            time.sleep(random.uniform(0.0, 0.02))
            return _fake_embed(texts)

        main = threading.get_ident()
        insert_threads: set[int] = set()
        real_insert = core._insert_pending_chunks

        def tracking_insert(db, pending, vectors):
            insert_threads.add(threading.get_ident())
            return real_insert(db, pending, vectors)

        core.embed = slow_embed
        results = {}
        with mock.patch.object(core, "_insert_pending_chunks", tracking_insert):
            for conc in (1, 4):
                config.EMBED_CONCURRENCY = conc
                stats = core.index_vault(rebuild=True, verbose=False)
                results[conc] = (stats.chunks, self._rows())
        self.assertEqual(results[1], results[4])
        self.assertGreater(results[4][0], 2 * config.EMBED_COMMIT_BATCH)
        self.assertEqual(insert_threads, {main})


class QueryEmbedSidecarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-qcache-")).resolve()
        self._patches = [
            mock.patch.object(config, "INDEX_PATH", self.tmp / "index-x.db"),
            mock.patch.object(config, "QUERY_EMBED_DISK_CACHE", True),
            mock.patch.object(config, "QUERY_EMBED_DISK_TTL", 3600.0),
            mock.patch.object(config, "QUERY_EMBED_WARM_DISK_LIMIT", 64),
            mock.patch.object(config, "EMBED_BACKEND", "ollama"),
        ]
        for p in self._patches:
            p.start()
        core._qcache_last_prune.clear()
        with core._query_embed_lock:
            core._query_embed_cache.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with core._query_embed_lock:
            core._query_embed_cache.clear()
        core._qcache_last_prune.clear()
        core.writer_close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sidecar(self) -> sqlite3.Connection:
        return sqlite3.connect(core.query_embed_cache_path())

    def test_sidecar_path_next_to_index(self):
        self.assertEqual(core.query_embed_cache_path(), self.tmp / "index-x.qcache.db")

    def test_round_trip(self):
        core._save_query_embed_disk("hello", [0.5, -0.25, 1.0])
        self.assertEqual(core._load_query_embed_disk("hello"), [0.5, -0.25, 1.0])
        self.assertIsNone(core._load_query_embed_disk("other"))

    def test_ttl_expiry_and_model_mismatch(self):
        core._save_query_embed_disk("old", [1.0])
        core._save_query_embed_disk("other model", [1.0])
        db = self._sidecar()
        db.execute("UPDATE qemb SET at = at - 7200 WHERE q='old'")
        db.execute("UPDATE qemb SET model = 'not-the-model' WHERE q='other model'")
        db.commit()
        db.close()
        self.assertIsNone(core._load_query_embed_disk("old"))
        self.assertIsNone(core._load_query_embed_disk("other model"))
        self.assertEqual(core._hydrate_query_embed_from_disk(), 0)

    def test_hydrate_takes_most_recent_up_to_cap(self):
        for i in range(5):
            core._save_query_embed_disk(f"q{i}", [float(i)])
        db = self._sidecar()
        for i in range(5):
            db.execute("UPDATE qemb SET at = ? WHERE q = ?", (1000.0 + i + time.time(), f"q{i}"))
        db.commit()
        db.close()
        self.assertEqual(core._hydrate_query_embed_from_disk(limit=2), 2)
        with core._query_embed_lock:
            self.assertEqual(set(core._query_embed_cache), {"q3", "q4"})

    def test_save_prunes_expired_rows(self):
        core._save_query_embed_disk("stale", [1.0])
        db = self._sidecar()
        db.execute("UPDATE qemb SET at = at - 7200")
        db.commit()
        core._qcache_last_prune.clear()
        core._save_query_embed_disk("fresh", [2.0])
        rows = [r[0] for r in db.execute("SELECT q FROM qemb")]
        db.close()
        self.assertEqual(rows, ["fresh"])

    def test_never_raises_on_unwritable_path(self):
        with mock.patch.object(config, "INDEX_PATH", self.tmp / "missing" / "dir" / "index.db"):
            core._save_query_embed_disk("q", [1.0])
            self.assertIsNone(core._load_query_embed_disk("q"))
            self.assertEqual(core._hydrate_query_embed_from_disk(), 0)
        core.query_embed_cache_path().mkdir()
        core._save_query_embed_disk("q", [1.0])
        self.assertIsNone(core._load_query_embed_disk("q"))
        self.assertEqual(core._hydrate_query_embed_from_disk(), 0)

    def test_query_embed_never_writes_index_db(self):
        with mock.patch.object(core, "writer_connect", side_effect=AssertionError("index.db write")), \
             mock.patch.object(core, "_ollama_embed_request", return_value=[[0.1, 0.2]]):
            vec = core.query_embed("novel query never seen")
        self.assertEqual(len(vec), 2)
        self.assertFalse((self.tmp / "index-x.db").exists())
        self.assertIsNotNone(core._load_query_embed_disk("novel query never seen"))


class GroupedVaultFlushTest(VaultTestCase):
    def _snapshot(self):
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            chunks = db.execute("SELECT path, ord, text FROM chunks ORDER BY path, ord").fetchall()
            files = db.execute("SELECT path, hash != '' FROM files ORDER BY path").fetchall()
            return chunks, files
        finally:
            db.close()

    def test_vault_larger_than_one_group_indexes_identically(self):
        for i in range(15):
            self.write(f"n{i:02d}.md", f"# Note {i}\n\nalpha {i}\n\n## More\n\nbeta {i}\n")
        self.write("n07-bad.md", "# Bad\n\npoisoned wombat\n")

        def drop_poisoned(texts, **kwargs):
            vecs = _fake_embed(texts)
            return [None if "poisoned" in t else v for t, v in zip(texts, vecs)]

        core.embed = drop_poisoned
        saved_backoff = config.EMBED_FAIL_BACKOFF
        config.EMBED_FAIL_BACKOFF = 0.0
        out = {}
        flushes = {}
        real_store = core._embed_and_store_pending
        try:
            for group in (100_000, 3):
                calls = []

                def counting_store(db, pending, **kwargs):
                    calls.append(len(pending))
                    return real_store(db, pending, **kwargs)

                with mock.patch.object(core, "_VAULT_FLUSH_CHUNKS", group), \
                     mock.patch.object(core, "_embed_and_store_pending", counting_store):
                    stats = core.index_vault(rebuild=True, verbose=False)
                out[group] = ((stats.added, stats.changed, stats.removed, stats.chunks), self._snapshot())
                flushes[group] = calls
        finally:
            config.EMBED_FAIL_BACKOFF = saved_backoff
        self.assertEqual(len(flushes[100_000]), 1)
        self.assertGreater(len(flushes[3]), 5)
        self.assertEqual(out[100_000], out[3])
        (added, _changed, _removed, chunks), (chunk_rows, files) = out[3]
        self.assertEqual(added, 15)
        self.assertEqual(chunks, len(chunk_rows))
        stamped = {p for p, ok in files if ok}
        self.assertEqual(stamped, {f"n{i:02d}.md" for i in range(15)})
        self.assertIn(("n07-bad.md", 0), files)
        self.assertNotIn("n07-bad.md", {r[0] for r in chunk_rows})


if __name__ == "__main__":
    unittest.main()
