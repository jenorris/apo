"""Tests for PathDebouncer and search/index perf helpers."""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from apo_engine import config, core
from apo_engine.watch import PathDebouncer


class PathDebouncerTest(unittest.TestCase):
    def test_coalesces_until_quiet(self):
        d = PathDebouncer(0.15)
        a = Path("/tmp/a.md")
        b = Path("/tmp/b.md")
        t0 = time.monotonic()
        d.touch(a, now=t0)
        d.touch(b, now=t0)
        d.touch(a, now=t0 + 0.05)  # reset a
        self.assertEqual(d.ready(now=t0 + 0.10), [])
        self.assertEqual(d.waiting(), 2)
        due = d.ready(now=t0 + 0.16)
        self.assertEqual(due, [b])  # only b quiet long enough
        self.assertEqual(d.waiting(), 1)
        due2 = d.ready(now=t0 + 0.30)
        self.assertEqual(due2, [a])
        self.assertEqual(d.waiting(), 0)

    def test_next_due_in(self):
        d = PathDebouncer(1.0)
        self.assertIsNone(d.next_due_in(now=100.0))
        d.touch(Path("/tmp/x.md"), now=100.0)
        self.assertAlmostEqual(d.next_due_in(now=100.4) or -1, 0.6, places=2)


class IndexFileUnchangedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-unchanged-")).resolve()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._saved = {
            k: getattr(config, k)
            for k in ("NOTES_ROOT", "INDEX_PATH", "MAX_CHARS", "OVERLAP", "IGNORE_FILE")
        }
        config.NOTES_ROOT = self.vault
        config.INDEX_PATH = self.tmp / "index.db"
        config.MAX_CHARS = 200
        config.OVERLAP = 20
        config.IGNORE_FILE = self.tmp / "missing"
        self._embed = core.embed
        self.calls = 0

        def counting_embed(texts):
            self.calls += 1
            return [[1.0] + [0.0] * 15 for _ in texts]

        core.embed = counting_embed

    def tearDown(self):
        for k, val in self._saved.items():
            setattr(config, k, val)
        core.embed = self._embed
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skips_embed_when_hash_unchanged(self):
        note = self.vault / "n.md"
        note.write_text("# N\n\nhello world\n", encoding="utf-8")
        self.assertGreater(core.index_file(note), 0)
        self.assertEqual(self.calls, 1)
        self.assertEqual(core.index_file(note), 0)
        self.assertEqual(self.calls, 1)


class QueryEmbedCacheTest(unittest.TestCase):
    def setUp(self):
        self._embed = core.embed
        self.calls = 0
        core.clear_query_embed_cache()
        self._ttl = config.QUERY_EMBED_TTL
        config.QUERY_EMBED_TTL = 60.0

        def counting(texts):
            self.calls += 1
            return [[0.5] * 8 for _ in texts]

        core.embed = counting

    def tearDown(self):
        core.embed = self._embed
        config.QUERY_EMBED_TTL = self._ttl
        core.clear_query_embed_cache()

    def test_cache_hit(self):
        a = core.query_embed("same query")
        b = core.query_embed("same query")
        self.assertEqual(a, b)
        self.assertEqual(self.calls, 1)
        core.query_embed("  same   query  ")
        self.assertEqual(self.calls, 1)


class ProcessQueuesConsumeIndexFlag(unittest.TestCase):
    def setUp(self):
        from apo_engine import deferred

        self.deferred = deferred
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-pq2-")).resolve()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._saved = {
            k: getattr(config, k)
            for k in ("NOTES_ROOT", "INDEX_PATH", "COLLECTION", "MAX_CHARS", "OVERLAP", "IGNORE_FILE")
        }
        config.NOTES_ROOT = self.vault
        config.INDEX_PATH = self.tmp / "index.db"
        config.COLLECTION = "pq2"
        config.MAX_CHARS = 200
        config.OVERLAP = 20
        config.IGNORE_FILE = self.tmp / "missing"
        self._dir = deferred.DEFERRED_DIR
        deferred.DEFERRED_DIR = self.tmp / "apo"
        self._embed = core.embed
        core.embed = lambda texts: [[1.0, 0.0] * 8 for _ in texts]

    def tearDown(self):
        for k, val in self._saved.items():
            setattr(config, k, val)
        self.deferred.DEFERRED_DIR = self._dir
        core.embed = self._embed
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_consume_index_false_leaves_queue(self):
        note = self.vault / "a.md"
        note.write_text("# A\n\nalpha\n", encoding="utf-8")
        self.deferred.enqueue_index(config.COLLECTION, str(note))
        stats = core.process_queues(config.COLLECTION, scan_vault=False, consume_index=False)
        self.assertEqual(stats.indexed, 0)
        left = self.deferred.consume_index_queue(config.COLLECTION)
        self.assertEqual(len(left), 1)


if __name__ == "__main__":
    unittest.main()
