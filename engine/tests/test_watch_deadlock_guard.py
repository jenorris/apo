"""Watcher deadlock hardening — writer reset, WAL bounds, embed quarantine, MCP read path."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from apo_engine import config, core, git_sync, ops

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from test_core import VaultTestCase, _fake_embed  # noqa: E402


class WriterResetTest(VaultTestCase):
    def test_writer_reset_clears_uncommitted_transaction(self):
        db = core.writer_connect()
        db.execute("DELETE FROM files")
        self.assertTrue(db.in_transaction)
        core.writer_reset()
        db2 = core.writer_connect()
        self.assertFalse(db2.in_transaction)


class WalBoundTest(VaultTestCase):
    def test_journal_size_limit_set_on_writer_connect(self):
        db = core.writer_connect()
        limit = db.execute("PRAGMA journal_size_limit").fetchone()[0]
        self.assertEqual(int(limit), config.WAL_LIMIT_BYTES)


class EmbedQuarantineTest(VaultTestCase):
    def test_quarantine_after_n_drops_same_hash(self):
        self.write("bad.md", "# Bad\n\npoisoned wombat\n")
        real_embed = core.embed
        saved_backoff = config.EMBED_FAIL_BACKOFF
        config.EMBED_FAIL_BACKOFF = 0.0
        n = config.EMBED_FAIL_QUARANTINE

        def always_drop(texts, **kwargs):
            return [None] * len(texts)

        core.embed = always_drop
        try:
            for _ in range(n):
                core.index_vault(verbose=False)
        finally:
            core.embed = real_embed
            config.EMBED_FAIL_BACKOFF = saved_backoff

        db = sqlite3.connect(config.INDEX_PATH)
        try:
            row = db.execute(
                "SELECT embed_quarantined, hash FROM files WHERE path='bad.md'"
            ).fetchone()
        finally:
            db.close()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 1)
        self.assertTrue(row[1])

        # Hash change clears quarantine path and allows re-index.
        note = self.vault / "bad.md"
        note.write_text("# Bad\n\npoisoned wombat updated\n", encoding="utf-8")
        core.embed = _fake_embed
        core.index_vault(verbose=False)
        self.assertIn("bad.md", self.chunk_paths())

    def test_first_embed_drop_still_unstamped(self):
        """Regression: first drop must not mtime-skip (existing contract)."""
        self.write("bad.md", "# Bad\n\npoisoned wombat\n")

        def drop_once(texts, **kwargs):
            return [None] * len(texts)

        core.embed = drop_once
        try:
            core.index_vault(verbose=False)
        finally:
            core.embed = _fake_embed

        db = sqlite3.connect(config.INDEX_PATH)
        try:
            row = db.execute(
                "SELECT path, mtime, hash, embed_quarantined FROM files WHERE path='bad.md'"
            ).fetchone()
        finally:
            db.close()
        self.assertIsNotNone(row)
        self.assertEqual(float(row[1]), 0.0)
        self.assertEqual(row[2], "")
        self.assertEqual(int(row[3]), 0)
        self.assertNotIn("bad.md", self.chunk_paths())


class ReaderConnectTest(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.md", "# A\n\nalpha\n")
        core.index_vault(verbose=False)

    def test_reader_uses_read_only_uri(self):
        with unittest.mock.patch.object(core, "_open_sqlite", wraps=core._open_sqlite) as spy:
            core.reader_connect()
        self.assertTrue(any(c.kwargs.get("read_only") is True for c in spy.call_args_list))

    def test_reader_ping_fail_closes_before_reopen(self):
        saved_ping = config.READER_PING_INTERVAL
        config.READER_PING_INTERVAL = 0.0
        try:
            db = core.reader_connect()
            key = core._index_key()
            conns = core._tls_map(core._reader_local, "conns")

            class _Broken:
                def execute(self, *_a, **_k):
                    raise sqlite3.OperationalError("locked")

                def close(self) -> None:
                    pass

            conns[key] = _Broken()  # type: ignore[assignment]
            db2 = core.reader_connect()
            self.assertIsNot(db, db2)
            self.assertIsInstance(db2, sqlite3.Connection)
        finally:
            config.READER_PING_INTERVAL = saved_ping


class LockBackoffTest(unittest.TestCase):
    def test_consecutive_lock_errors_increase_backoff(self):
        idx = Path(tempfile.mktemp(prefix="apo-idx-"))
        core.clear_index_lock_health(index=idx)
        d1 = core.note_index_lock_error(index=idx)
        d2 = core.note_index_lock_error(index=idx)
        self.assertGreater(d2, d1)
        core.clear_index_lock_health(index=idx)
        self.assertFalse(core.index_lock_backoff_active(index=idx))


class GitSyncSkipTest(unittest.TestCase):
    def test_tick_skips_pull_when_lock_backoff_active(self):
        root = Path(tempfile.mkdtemp(prefix="apo-vault-"))
        ctl = git_sync.VaultSyncController(root, verbose=False)
        ctl._last_pull_at = time.monotonic() - 120.0
        with unittest.mock.patch.object(git_sync, "sync_enabled", return_value=True):
            with unittest.mock.patch.object(core, "index_lock_backoff_active", return_value=True):
                with unittest.mock.patch.object(git_sync, "pull_ff_only") as pull:
                    ctl.tick(index_busy=False)
        pull.assert_not_called()

    def test_tick_skips_pull_when_wal_over_limit(self):
        root = Path(tempfile.mkdtemp(prefix="apo-vault-"))
        ctl = git_sync.VaultSyncController(root, verbose=False)
        ctl._last_pull_at = time.monotonic() - 120.0
        with unittest.mock.patch.object(git_sync, "sync_enabled", return_value=True):
            with unittest.mock.patch.object(core, "index_lock_backoff_active", return_value=False):
                with unittest.mock.patch.object(core, "is_wal_over_limit", return_value=True):
                    with unittest.mock.patch.object(git_sync, "pull_ff_only") as pull:
                        ctl.tick(index_busy=False)
        pull.assert_not_called()


class OpsIndexBusyTest(unittest.TestCase):
    def test_sqlite_lock_maps_to_index_busy(self):
        err = ops._sqlite_index_err(sqlite3.OperationalError("database is locked"))
        assert err is not None
        self.assertFalse(err["ok"])
        self.assertEqual(err["error"], "index_busy")


class WatchBackoffHelperTest(unittest.TestCase):
    def test_note_index_lock_error_resets_after_clear(self):
        idx = Path("/tmp/apo-test-index.db")
        core.note_index_lock_error(index=idx)
        self.assertTrue(core.index_lock_backoff_active(index=idx))
        core.clear_index_lock_health(index=idx)
        self.assertFalse(core.index_lock_backoff_active(index=idx))
