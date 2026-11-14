"""Phase 0: the watcher must not index its own heartbeat, and one chunk is one hit."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

from apo_engine import config, core, watch, watch_health

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from test_core import VaultTestCase  # noqa: E402


class IgnoreEngineStateTest(VaultTestCase):
    def test_hook_file_is_not_indexed_or_watched(self):
        hook = self.write(".apo/watch-hooks.json", '{"git_sync": {"ok": true}}\n')
        note = self.write("note.md", "# Note\n\nhello from the note\n")
        core.index_files([hook, note], verbose=False)
        self.assertNotIn(".apo/watch-hooks.json", self.chunk_paths())
        self.assertIn("note.md", self.chunk_paths())
        ignore = core._compile_ignore(core._load_ignore())
        self.assertTrue(watch._event_path_noise(str(hook), self.vault, ignore))
        self.assertFalse(watch._event_path_noise(str(note), self.vault, ignore))

    def test_indexing_twice_keeps_one_row_per_ord(self):
        note = self.write("note.md", "# Note\n\nhello from the note\n")
        core.index_files([note, note], verbose=False)
        core.index_files([note], verbose=False)
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            rows = db.execute(
                "SELECT COUNT(*) FROM chunks WHERE path = ?", ("note.md",)
            ).fetchone()[0]
            distinct = db.execute(
                "SELECT COUNT(DISTINCT ord) FROM chunks WHERE path = ?", ("note.md",)
            ).fetchone()[0]
        finally:
            db.close()
        self.assertGreater(rows, 0)
        self.assertEqual(rows, distinct)
        health = core.index_health()
        self.assertEqual(health["duplicate_chunks"], 0)
        self.assertNotIn("duplicate_chunks", health["flags"])

    def test_search_collapses_duplicate_chunk_hash(self):
        note = self.write("note.md", "# Note\n\nzebra sleeps in the shade\n")
        core.index_files([note], verbose=False)
        db = core.writer_connect()
        row = db.execute(
            """
            SELECT path, heading, text, start_line, end_line, heading_level,
                   chunk_hash, content_hash
            FROM chunks WHERE path = ? LIMIT 1
            """,
            ("note.md",),
        ).fetchone()
        self.assertIsNotNone(row)
        chunk_hash = row[6]
        db.execute(
            """
            INSERT INTO chunks (
                path, ord, heading, text, start_line, end_line, heading_level,
                chunk_hash, content_hash
            ) VALUES (?, 99, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]),
        )
        db.commit()
        hits = core.search("zebra shade", k=8, hybrid=False)
        matched = [h for h in hits if h.chunk_hash == chunk_hash and h.path == "note.md"]
        self.assertEqual(len(matched), 1)


class WatchLockTest(VaultTestCase):
    def test_second_watch_exits_while_first_holds_the_lock(self):
        env = os.environ.copy()
        env["WATCH_PID_DIR"] = str(self.tmp / "watch-pid")
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """
                    from apo_engine.watch import _acquire_watch_lock
                    import time
                    assert _acquire_watch_lock()
                    print("held", flush=True)
                    time.sleep(30)
                    """
                ),
            ],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = holder.stdout.readline() if holder.stdout is not None else ""
            self.assertIn("held", line)
            done = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        from apo_engine.watch import run_watch
                        run_watch(interval=60, verbose=False, use_events=False)
                        """
                    ),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIsNone(holder.poll())
        finally:
            holder.kill()
            holder.wait(timeout=5)


class RecordStartedThrottleTest(unittest.TestCase):
    def test_record_started_does_not_rewrite_inside_the_gap(self):
        import shutil
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="apo-started-")).resolve()
        try:
            tracker = watch_health.HookHealthTracker(tmp)
            tracker.record_started("git_sync")
            path = tmp / ".apo" / "watch-hooks.json"
            first = path.stat().st_mtime_ns
            time.sleep(0.01)
            tracker.record_started("git_sync")
            self.assertEqual(path.stat().st_mtime_ns, first)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
