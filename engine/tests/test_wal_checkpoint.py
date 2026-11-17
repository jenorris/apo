"""WAL truncate must not run on every watcher tick."""

from __future__ import annotations

import unittest
from unittest import mock

from apo_engine import index_db


class WalCheckpointTests(unittest.TestCase):
    def setUp(self):
        self._prev = index_db._last_wal_truncate
        index_db._last_wal_truncate = 0.0

    def tearDown(self):
        index_db._last_wal_truncate = self._prev

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
        self.assertEqual(
            sql,
            [
                "PRAGMA wal_checkpoint(PASSIVE)",
                "PRAGMA wal_checkpoint(TRUNCATE)",
                "PRAGMA wal_checkpoint(PASSIVE)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
