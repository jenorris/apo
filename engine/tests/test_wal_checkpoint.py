"""WAL truncate must not run on every watcher tick."""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from apo_engine import index_db


class WalCheckpointTests(unittest.TestCase):
    def setUp(self):
        self._prev = dict(index_db._last_wal_truncate)
        index_db._last_wal_truncate.clear()

    def tearDown(self):
        index_db._last_wal_truncate.clear()
        index_db._last_wal_truncate.update(self._prev)

    def _db(self, row):
        db = mock.Mock()
        db.execute.return_value.fetchone.return_value = row
        return db

    def test_passive_only_while_a_reader_is_busy(self):
        db = self._db((1, 10, 4))
        with (
            mock.patch.object(index_db, "is_wal_over_limit", return_value=True),
            mock.patch.object(index_db, "writer_connect", return_value=db),
        ):
            self.assertFalse(index_db.checkpoint_wal_if_over_limit())
        sql = [c.args[0] for c in db.execute.call_args_list]
        self.assertEqual(sql, ["PRAGMA wal_checkpoint(PASSIVE)"])

    def test_truncate_runs_once_per_interval_when_idle(self):
        db = self._db((0, 10, 10))
        with (
            mock.patch.object(index_db, "is_wal_over_limit", return_value=True),
            mock.patch.object(index_db, "writer_connect", return_value=db),
            mock.patch.object(index_db, "wal_bytes", return_value=0),
        ):
            self.assertTrue(index_db.checkpoint_wal_if_over_limit())
            self.assertFalse(index_db.checkpoint_wal_if_over_limit())
        sql = [c.args[0] for c in db.execute.call_args_list]
        # The mock answers every fetchone with (0, 10, 10), so the "previous"
        # busy_timeout restored after TRUNCATE is 0.
        self.assertEqual(
            sql,
            [
                "PRAGMA wal_checkpoint(PASSIVE)",
                "PRAGMA busy_timeout",
                "PRAGMA busy_timeout=1000",
                "PRAGMA wal_checkpoint(TRUNCATE)",
                "PRAGMA busy_timeout=0",
                "PRAGMA wal_checkpoint(PASSIVE)",
            ],
        )

    def test_one_vault_truncate_does_not_suppress_another(self):
        db = self._db((0, 10, 10))
        with (
            mock.patch.object(index_db, "is_wal_over_limit", return_value=True),
            mock.patch.object(index_db, "writer_connect", return_value=db),
            mock.patch.object(index_db, "wal_bytes", return_value=0),
            mock.patch.object(index_db, "_index_key", side_effect=["/a.db", "/b.db", "/a.db"]),
        ):
            self.assertTrue(index_db.checkpoint_wal_if_over_limit())
            self.assertTrue(index_db.checkpoint_wal_if_over_limit())
            self.assertFalse(index_db.checkpoint_wal_if_over_limit())

    def test_busy_timeout_is_restored_when_truncate_fails(self):
        db = mock.Mock()

        def execute(sql):
            if "TRUNCATE" in sql:
                raise sqlite3.OperationalError("database is locked")
            cur = mock.Mock()
            cur.fetchone.return_value = (30000,) if sql == "PRAGMA busy_timeout" else (0, 10, 10)
            return cur

        db.execute.side_effect = execute
        with (
            mock.patch.object(index_db, "is_wal_over_limit", return_value=True),
            mock.patch.object(index_db, "writer_connect", return_value=db),
            mock.patch.object(index_db, "writer_reset"),
        ):
            self.assertFalse(index_db.checkpoint_wal_if_over_limit())
        sql = [c.args[0] for c in db.execute.call_args_list]
        self.assertEqual(sql[-2:], ["PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA busy_timeout=30000"])

    def test_real_connection_keeps_its_busy_timeout(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(str(Path(tmp) / "i.db"))
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=4321")
                with (
                    mock.patch.object(index_db, "is_wal_over_limit", return_value=True),
                    mock.patch.object(index_db, "writer_connect", return_value=db),
                    mock.patch.object(index_db, "wal_bytes", return_value=0),
                ):
                    self.assertTrue(index_db.checkpoint_wal_if_over_limit())
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 4321)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
