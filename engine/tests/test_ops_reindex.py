"""ops.reindex — Problem B: watcher-aware index/reindex, unified for
``apo-engine index`` and the MCP ``apo_admin`` ``reindex`` capability.

A live watcher is simulated by mocking ``ops.watcher_status`` (the same
liveness probe both ``apo-engine`` and the MCP admin handler already use to
decide inline-vs-signal) rather than spawning a real watcher process. The
watcher's actual "picked up the rebuild / finished it" signal is the pair of
marker files in ``deferred`` (``rebuild-pending`` / ``rebuild-busy``); a
background thread stands in for the watcher draining those markers so
``wait=True`` has something real to poll.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, deferred, ops, vaults


class OpsReindexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-reindex-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# Note\n\nbody\n", encoding="utf-8")
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            mock.patch.object(config, "COLLECTION", "ops_reindex_test"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        _default, bindings = vaults.load_bindings(force=True)
        self.binding = bindings["default"]

    def test_bad_mode(self):
        out = ops.reindex(mode="nope")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_flush_no_watcher_warns(self):
        with mock.patch.object(ops, "watcher_status", return_value={"running": False}):
            out = ops.reindex(mode="flush")
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["watcher_running"])
        self.assertIn("warning", out)

    def test_rebuild_no_watcher_runs_inline(self):
        fake_stats = core.IndexStats(added=1, changed=0, removed=0, chunks=2, seconds=0.05)
        with mock.patch.object(ops, "watcher_status", return_value={"running": False}), mock.patch.object(
            core, "index_vault", return_value=fake_stats
        ) as m:
            out = ops.reindex(mode="rebuild", force=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["inline"])
        self.assertFalse(out["watcher_running"])
        self.assertEqual(out["added"], 1)
        self.assertEqual(out["chunks"], 2)
        m.assert_called_once_with(rebuild=True)

    def test_rebuild_watcher_live_no_wait_just_signals(self):
        with mock.patch.object(ops, "watcher_status", return_value={"running": True}):
            out = ops.reindex(mode="rebuild", force=False, wait=False)
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["inline"])
        self.assertTrue(out["rebuild_signaled"])
        self.assertFalse(out["waited"])
        self.assertTrue(deferred.rebuild_pending(self.binding.collection))

    def test_rebuild_watcher_live_wait_completes(self):
        collection = self.binding.collection

        def fake_watcher():
            # Wait for the signal, then mimic core.process_queues: consume the
            # pending marker, hold the busy marker for a beat, then clear it.
            deadline = time.monotonic() + 5
            while not deferred.rebuild_pending(collection) and time.monotonic() < deadline:
                time.sleep(0.01)
            deferred.consume_rebuild(collection)
            deferred.mark_rebuild_running(collection)
            time.sleep(0.2)
            deferred.clear_rebuild_running(collection)

        t = threading.Thread(target=fake_watcher, daemon=True)
        t.start()
        try:
            with mock.patch.object(ops, "watcher_status", return_value={"running": True}):
                out = ops.reindex(mode="rebuild", wait=True, timeout=5.0)
        finally:
            t.join(timeout=5)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["waited"])
        self.assertTrue(out["completed"], out)
        self.assertFalse(deferred.rebuild_pending(collection))
        self.assertFalse(deferred.rebuild_running(collection))

    def test_rebuild_watcher_live_wait_times_out(self):
        # No fake watcher thread: the markers never clear, so a short timeout
        # must return promptly with completed=False rather than hang.
        with mock.patch.object(ops, "watcher_status", return_value={"running": True}):
            t0 = time.monotonic()
            out = ops.reindex(mode="rebuild", wait=True, timeout=0.3)
            elapsed = time.monotonic() - t0
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["waited"])
        self.assertFalse(out["completed"])
        self.assertIn("warning", out)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
