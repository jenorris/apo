"""Tests for deferred queues and single-writer process_queues."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, deferred


class DeferredQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-deferred-"))
        self._saved_dir = deferred.DEFERRED_DIR
        deferred.DEFERRED_DIR = self.tmp
        self.collection = "test_coll"

    def tearDown(self):
        deferred.DEFERRED_DIR = self._saved_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_enqueue_and_consume_index(self):
        q = deferred.enqueue_index(self.collection, "/tmp/note.md")
        self.assertIn(str(Path("/tmp/note.md").resolve()), q)
        paths = deferred.consume_index_queue(self.collection)
        self.assertEqual(paths, [str(Path("/tmp/note.md").resolve())])
        self.assertEqual(deferred.consume_index_queue(self.collection), [])

    def test_enqueue_many_single_wake(self):
        touches = {"n": 0}
        orig = deferred.touch_wake

        def counting(coll):
            touches["n"] += 1
            orig(coll)

        deferred.touch_wake = counting  # type: ignore[method-assign]
        try:
            q = deferred.enqueue_many(
                self.collection, ["/tmp/a.md", "/tmp/b.md", "/tmp/a.md"], wake=True
            )
            self.assertEqual(touches["n"], 1)
            self.assertEqual(len(q), 2)
        finally:
            deferred.touch_wake = orig  # type: ignore[method-assign]

    def test_enqueue_purge(self):
        deferred.enqueue_purge(self.collection, "/tmp/gone.md")
        paths = deferred.consume_purge_queue(self.collection)
        self.assertEqual(len(paths), 1)
        self.assertEqual(deferred.consume_purge_queue(self.collection), [])

    def test_rebuild_signal(self):
        deferred.signal_rebuild(self.collection, force=True)
        payload = deferred.consume_rebuild(self.collection)
        self.assertEqual(payload, {"force": True})
        self.assertIsNone(deferred.consume_rebuild(self.collection))


class ProcessQueuesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-pq-")).resolve()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._saved = {
            k: getattr(config, k)
            for k in ("NOTES_ROOT", "INDEX_PATH", "COLLECTION", "MAX_CHARS", "OVERLAP", "IGNORE_FILE")
        }
        config.NOTES_ROOT = self.vault
        config.INDEX_PATH = self.tmp / "index.db"
        config.COLLECTION = "pq_test"
        config.MAX_CHARS = 200
        config.OVERLAP = 20
        config.IGNORE_FILE = self.tmp / "missing-ignore"
        self._saved_dir = deferred.DEFERRED_DIR
        deferred.DEFERRED_DIR = self.tmp / "apo"
        self._saved_embed = core.embed
        core.embed = lambda texts: [[1.0, 0.0] * 8 for _ in texts]  # dim 16

    def tearDown(self):
        for k, val in self._saved.items():
            setattr(config, k, val)
        deferred.DEFERRED_DIR = self._saved_dir
        core.embed = self._saved_embed
        core.writer_close()
        core._schema_ready.discard(str(config.INDEX_PATH.resolve()))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_process_index_queue(self):
        note = self.vault / "a.md"
        note.write_text("# A\n\nalpha\n", encoding="utf-8")
        deferred.enqueue_index(config.COLLECTION, str(note))
        stats = core.process_queues(config.COLLECTION, scan_vault=False, verbose=False)
        self.assertEqual(stats.indexed, 1)
        hits = core.search("alpha", k=1, hybrid=False)
        self.assertEqual(hits[0].path, "a.md")

    def test_process_purge_queue(self):
        note = self.vault / "gone.md"
        note.write_text("# Gone\n\nsecret zebra\n", encoding="utf-8")
        core.index_file(note, verbose=False)
        note.unlink()
        deferred.enqueue_purge(config.COLLECTION, str(note))
        stats = core.process_queues(config.COLLECTION, scan_vault=False, verbose=False)
        self.assertEqual(stats.purged, 1)
        self.assertEqual(core.search("zebra", k=3, hybrid=False), [])


if __name__ == "__main__":
    unittest.main()
