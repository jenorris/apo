"""0.39.3 index/watch reliability regressions.

Embed drop keeps old chunks, failed watch batches requeue, chunk-id allocation
under the write lock, vec/fts delete errors, per-commit checkpoint removal,
phrase-stem text cap, C frontmatter loader parity.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import config, content, core, index_db, indexing, ranking, watch

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from test_core import VaultTestCase, _fake_embed  # noqa: E402


def _drop_all(texts, **kwargs):
    return [None] * len(texts)


class EmbedDropKeepsPreviousChunksTest(VaultTestCase):
    def _rows(self, sql: str, *args) -> list[tuple]:
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def test_failed_embed_keeps_old_chunks_until_a_retry_succeeds(self):
        saved_backoff = config.EMBED_FAIL_BACKOFF
        config.EMBED_FAIL_BACKOFF = 0.0
        self.addCleanup(setattr, config, "EMBED_FAIL_BACKOFF", saved_backoff)
        self.write("other.md", "# Other\n\nplain\n")
        note = self.write("a.md", "# A\n\noriginal wombat text [[other]]\n")
        self.assertEqual(core.index_files([note]), 1)
        old_ids = self._rows("SELECT id FROM chunks WHERE path='a.md'")
        self.assertTrue(old_ids)
        links_before = self._rows("SELECT COUNT(*) FROM backlinks WHERE source='a.md'")

        note.write_text("# A\n\nedited numbat text [[other]]\n", encoding="utf-8")
        os.utime(note, (note.stat().st_atime, note.stat().st_mtime + 5))
        core.embed = _drop_all
        self.assertEqual(core.index_files([note]), 0)

        # Previous chunks, vectors, FTS rows and backlinks survive the failed embed.
        self.assertEqual(self._rows("SELECT id FROM chunks WHERE path='a.md'"), old_ids)
        texts = " ".join(r[0] for r in self._rows("SELECT text FROM chunks WHERE path='a.md'"))
        self.assertIn("original wombat", texts)
        self.assertEqual(
            self._rows("SELECT COUNT(*) FROM backlinks WHERE source='a.md'"), links_before
        )
        core.embed = _fake_embed
        core.reader_close()
        hits = core.search("wombat", k=5)
        self.assertIn("a.md", [h.path for h in hits])
        # The files row is the unstamped retry sentinel, so the next pass retries.
        row = self._rows("SELECT mtime, hash, embed_fails FROM files WHERE path='a.md'")[0]
        self.assertEqual((float(row[0]), row[1], int(row[2])), (0.0, "", 1))

        self.assertEqual(core.index_files([note]), 1)
        texts = " ".join(r[0] for r in self._rows("SELECT text FROM chunks WHERE path='a.md'"))
        self.assertIn("edited numbat", texts)
        self.assertNotIn("original wombat", texts)
        n_chunks = self._rows("SELECT COUNT(*) FROM chunks WHERE path='a.md'")[0][0]
        n_vec = self._rows(
            "SELECT COUNT(*) FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE path='a.md')"
        )[0][0]
        self.assertEqual(n_chunks, n_vec)
        self.assertEqual(
            self._rows("SELECT COUNT(*) FROM backlinks WHERE source='a.md'"), links_before
        )
        row = self._rows("SELECT hash, embed_fails FROM files WHERE path='a.md'")[0]
        self.assertTrue(row[0])
        self.assertEqual(int(row[1] or 0), 0)


class _FailingIndex:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(self, items, verbose=False):
        self.calls += 1
        raise self.exc


class WatchBatchRetryTest(unittest.TestCase):
    def setUp(self):
        self.debouncer = watch.PathDebouncer(0.0)
        self.retry = watch.IndexRetry(self.debouncer, label="t")
        self.paths = [Path("/vault/a.md"), Path("/vault/b.md")]

    def _cycle(self) -> list[Path]:
        ready = self.debouncer.ready()
        if ready:
            try:
                watch._index_paths(ready, verbose=False, retry=self.retry)
            except sqlite3.Error:
                pass
        return ready

    def test_raised_error_requeues_the_batch(self):
        self.debouncer.touch(self.paths)
        failing = _FailingIndex(sqlite3.OperationalError("database is locked"))
        with mock.patch.object(core, "index_files", failing):
            ready = self.debouncer.ready()
            with self.assertRaises(sqlite3.OperationalError):
                watch._index_paths(ready, verbose=False, retry=self.retry)
        self.assertEqual(self.debouncer.ready(), sorted(self.paths))

    def test_swallowed_oserror_requeues_the_batch(self):
        self.debouncer.touch(self.paths)
        with mock.patch.object(core, "index_files", _FailingIndex(OSError("io"))):
            self.assertEqual(
                watch._index_paths(self.debouncer.ready(), verbose=False, retry=self.retry), 0
            )
        self.assertEqual(self.debouncer.waiting(), 2)

    def test_permanent_failure_stops_after_the_limit_and_logs_once(self):
        self.debouncer.touch(self.paths)
        failing = _FailingIndex(sqlite3.OperationalError("disk I/O error"))
        with (
            mock.patch.object(core, "index_files", failing),
            self.assertLogs(watch.logger, level="WARNING") as logs,
        ):
            for _ in range(watch.INDEX_RETRY_LIMIT + 3):
                self._cycle()
            # A later fs event for the same path is tried, but not requeued again.
            self.debouncer.touch(self.paths)
            self._cycle()
        self.assertEqual(failing.calls, watch.INDEX_RETRY_LIMIT + 1)
        self.assertEqual(self.debouncer.waiting(), 0)
        self.assertEqual(len(logs.records), 1)

    def test_success_resets_the_count(self):
        self.debouncer.touch(self.paths)
        with mock.patch.object(core, "index_files", _FailingIndex(OSError("io"))):
            for _ in range(watch.INDEX_RETRY_LIMIT - 1):
                self._cycle()
        self.assertEqual(self.debouncer.waiting(), 2)
        with mock.patch.object(core, "index_files", lambda items, verbose=False: len(items)):
            self._cycle()
        self.assertEqual(self.debouncer.waiting(), 0)
        self.debouncer.touch(self.paths)
        with mock.patch.object(core, "index_files", _FailingIndex(OSError("io"))):
            self._cycle()
        self.assertEqual(self.debouncer.waiting(), 2)


class _HookedDb:
    """Connection proxy that runs ``hook`` right after the last MAX(...) read."""

    def __init__(self, db: sqlite3.Connection, hook) -> None:
        self._db = db
        self._hook = hook

    @property
    def in_transaction(self) -> bool:
        return self._db.in_transaction

    def execute(self, sql, *args):
        cur = self._db.execute(sql, *args)
        if "MAX(rowid), 0) FROM chunks_fts" in sql:
            hook, self._hook = self._hook, None
            if hook is not None:
                hook()
        return cur

    def executemany(self, sql, rows):
        return self._db.executemany(sql, rows)


def _pending(rel: str, text: str) -> tuple:
    return (rel, 0, "H", text, 1, 2, 1, f"hash-{rel}")


class ChunkIdAllocationTest(VaultTestCase):
    def test_second_writer_cannot_allocate_the_same_ids(self):
        vec = [[1.0] + [0.0] * 15]
        a = index_db.connect(config.INDEX_PATH)
        b = index_db.connect(config.INDEX_PATH)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.execute("PRAGMA busy_timeout=0")
        index_db._insert_pending_chunks(a, [_pending("seed.md", "seed")], vec)
        a.commit()

        blocked: list[bool] = []

        def other_writer() -> None:
            try:
                index_db._insert_pending_chunks(a, [_pending("a.md", "from a")], vec)
                a.commit()
                blocked.append(False)
            except sqlite3.OperationalError:
                a.rollback()
                blocked.append(True)

        self.assertFalse(b.in_transaction)
        index_db._insert_pending_chunks(_HookedDb(b, other_writer), [_pending("b.md", "from b")], vec)
        b.commit()
        # The second writer had to wait for the lock instead of reading the same MAX.
        self.assertEqual(blocked, [True])
        index_db._insert_pending_chunks(a, [_pending("a.md", "from a")], vec)
        a.commit()
        ids = [r[0] for r in a.execute("SELECT id FROM chunks ORDER BY id")]
        self.assertEqual(ids, [1, 2, 3])

    def test_caller_transaction_still_commits_and_rolls_back(self):
        vec = [[1.0] + [0.0] * 15]
        db = core.writer_connect()
        index_db._insert_pending_chunks(db, [_pending("a.md", "kept")], vec)
        self.assertTrue(db.in_transaction)
        db.commit()
        index_db._insert_pending_chunks(db, [_pending("b.md", "undone")], vec)
        db.rollback()
        self.assertFalse(db.in_transaction)
        self.assertEqual([r[0] for r in db.execute("SELECT path FROM chunks")], ["a.md"])


class _VecDeleteFails:
    def __init__(self, db: sqlite3.Connection, message: str) -> None:
        self._db = db
        self._message = message

    def execute(self, sql, *args):
        if sql.startswith("DELETE FROM vec_chunks"):
            raise sqlite3.OperationalError(self._message)
        return self._db.execute(sql, *args)


class DeletePathErrorTest(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.md", "# A\n\nalpha\n")
        core.index_vault(verbose=False)
        self.db = core.writer_connect()

    def _chunks(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM chunks WHERE path='a.md'").fetchone()[0]

    def test_locked_error_is_raised_and_chunks_are_kept(self):
        with self.assertRaises(sqlite3.OperationalError):
            indexing._delete_path(_VecDeleteFails(self.db, "database is locked"), "a.md")
        self.assertGreater(self._chunks(), 0)

    def test_missing_table_is_still_tolerated(self):
        indexing._delete_path(_VecDeleteFails(self.db, "no such table: vec_chunks"), "a.md")
        self.assertEqual(self._chunks(), 0)


class FinalizeCheckpointTest(VaultTestCase):
    def test_finalize_does_not_checkpoint_after_every_commit(self):
        note = self.write("a.md", "# A\n\nalpha\n")
        db = core.writer_connect()
        seen: list[str] = []
        db.set_trace_callback(seen.append)
        try:
            core.index_files([note])
        finally:
            db.set_trace_callback(None)
        self.assertTrue(any(s.startswith("COMMIT") for s in seen))
        self.assertEqual([s for s in seen if "wal_checkpoint" in s], [])
        # SQLite's own auto-checkpoint stays on (the engine never overrides it).
        self.assertGreater(db.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)


class PhraseStemTextCapTest(VaultTestCase):
    def test_injected_hit_text_is_capped_like_other_hydrated_hits(self):
        self.write(
            "catalog/widget-catalog-diagram.md",
            "# Widget catalog diagram\n\n" + "torque calibration " * 8 + "\n",
        )
        core.index_vault(verbose=False)
        full = core.reader_connect().execute("SELECT MAX(LENGTH(text)) FROM chunks").fetchone()[0]
        self.assertGreater(full, 40)
        with mock.patch.object(config, "SEARCH_HYDRATE_CHARS", 40):
            hits, _ = ranking._inject_phrase_stem_hits([], "widget catalog diagram", "catalog")
        self.assertEqual(len(hits), 1)
        self.assertLessEqual(len(hits[0].text), 40)


_REPRESENTATIVE_FM = """\
title: "Quarterly review: Q3"
okf_type: Thread
description: >
  Folded text
  over two lines
timestamp: 2026-08-05T00:05:04Z
created: 2026-08-05
effective_date: 2017-00-00
tags: [alpha, beta-gamma, "with space"]
aliases:
  - First alias
  - 'second: alias'
priority: 2
ratio: 0.75
done: false
legacy: yes
owner: null
empty:
nested:
  key: value
  list:
    - {a: 1, b: [x, y]}
  note: |
    literal
    block
unicode: "naïve — café"
"""


class FrontmatterLoaderTest(unittest.TestCase):
    def test_uses_the_c_loader_when_available(self):
        base = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        self.assertTrue(issubclass(content._FrontmatterLoader, base))

    def test_invalid_timestamp_stays_a_string(self):
        fm = content._parse_frontmatter(
            "---\neffective_date: 2017-00-00\nwhen: 2026-13-45T00:00:00Z\n"
            "ok: 2026-08-05\ntitle: X\n---\n\n# X\n"
        )
        self.assertEqual(fm["effective_date"], "2017-00-00")
        self.assertEqual(fm["when"], "2026-13-45T00:00:00Z")
        self.assertEqual(fm["ok"], datetime.date(2026, 8, 5))
        self.assertEqual(fm["title"], "X")

    def test_matches_pure_python_safe_loader(self):
        class _PureLoader(yaml.SafeLoader):
            pass

        _PureLoader.add_constructor(
            "tag:yaml.org,2002:timestamp", content._construct_yaml_timestamp
        )
        expected = yaml.load(_REPRESENTATIVE_FM, Loader=_PureLoader)
        got = content._parse_frontmatter(f"---\n{_REPRESENTATIVE_FM}---\n\nbody\n")
        self.assertEqual(got, expected)
        self.assertEqual(
            {k: type(v) for k, v in got.items()}, {k: type(v) for k, v in expected.items()}
        )

    def test_malformed_yaml_is_an_empty_mapping_on_both_loaders(self):
        for bad in ("a: [1, 2\n", "a: b: c\n", "\ta: 1\n", "a: 'unterminated\n", "- just\n- a list\n"):
            self.assertEqual(content._parse_frontmatter(f"---\n{bad}---\n"), {}, bad)


if __name__ == "__main__":
    unittest.main()
