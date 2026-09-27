"""Watcher logging setup — timestamps, rotation bound, and log-line shape.

Covers "Problem A" in docs/watcher-scheduler-separation.md: `watch.py` used
to log via bare `print(..., flush=True)`, appended forever by launchd/
`watch.sh` with no timestamp and no size bound (988MB / 174MB observed). This
replaces that with a module-level `logging` setup: a `RotatingFileHandler` on
the `"apo"` logger (bounded), a timestamped formatter, and — since `core.py`'s
`logging.getLogger("apo.index")` warnings are a *child* of `"apo"` — the same
handler picks those up via the logging hierarchy with no separate wiring.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apo_engine import watch
from apo_engine.core import QueueStats


def _reset_apo_logger() -> None:
    """Tests each get a clean `"apo"` logger — `_configure_logging` is
    otherwise idempotent-by-design (attach the handler once per process)."""
    root = logging.getLogger("apo")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(logging.WARNING)


class LogFilePathTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: __import__("os").environ.get(k)
            for k in ("APO_WATCH_LOG_FILE", "WATCH_PID_DIR")
        }

    def tearDown(self):
        import os

        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_explicit_override_wins(self):
        import os

        os.environ["APO_WATCH_LOG_FILE"] = "/tmp/custom-watch.log"
        os.environ.pop("WATCH_PID_DIR", None)
        self.assertEqual(watch.log_file_path(), Path("/tmp/custom-watch.log"))

    def test_watch_pid_dir_used_when_no_override(self):
        import os

        os.environ.pop("APO_WATCH_LOG_FILE", None)
        os.environ["WATCH_PID_DIR"] = "/tmp/apo-pid-dir"
        self.assertEqual(watch.log_file_path(), Path("/tmp/apo-pid-dir/watch.log"))

    def test_default_is_home_dot_apo(self):
        import os

        os.environ.pop("APO_WATCH_LOG_FILE", None)
        os.environ.pop("WATCH_PID_DIR", None)
        self.assertEqual(watch.log_file_path(), Path.home() / ".apo" / "watch.log")


class ConfigureLoggingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-watchlog-")).resolve()
        self.log_path = self.tmp / "watch.log"
        import os

        self._saved_override = os.environ.get("APO_WATCH_LOG_FILE")
        os.environ["APO_WATCH_LOG_FILE"] = str(self.log_path)
        _reset_apo_logger()

    def tearDown(self):
        import os

        if self._saved_override is None:
            os.environ.pop("APO_WATCH_LOG_FILE", None)
        else:
            os.environ["APO_WATCH_LOG_FILE"] = self._saved_override
        _reset_apo_logger()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_rotating_handler_on_apo_logger(self):
        watch._configure_logging(verbose=False)
        root = logging.getLogger("apo")
        self.assertEqual(len(root.handlers), 1)
        self.assertIsInstance(root.handlers[0], RotatingFileHandler)
        self.assertEqual(root.handlers[0].maxBytes, watch._LOG_MAX_BYTES)
        self.assertEqual(root.handlers[0].backupCount, watch._LOG_BACKUP_COUNT)
        self.assertTrue(self.log_path.is_file())

    def test_idempotent_does_not_duplicate_handler(self):
        watch._configure_logging(verbose=False)
        watch._configure_logging(verbose=True)
        root = logging.getLogger("apo")
        self.assertEqual(len(root.handlers), 1)

    def test_verbose_sets_debug_level(self):
        watch._configure_logging(verbose=True)
        self.assertEqual(logging.getLogger("apo").level, logging.DEBUG)

    def test_non_verbose_sets_info_level(self):
        watch._configure_logging(verbose=False)
        self.assertEqual(logging.getLogger("apo").level, logging.INFO)

    def test_watch_logger_message_is_timestamped(self):
        watch._configure_logging(verbose=False)
        watch.logger.info("hello from watch")
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("hello from watch", text)
        self.assertIn("[apo.watch]", text)
        # ISO-ish date prefix, e.g. "2026-09-27T..."
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_apo_index_logger_inherits_same_handler(self):
        """core.py's `logging.getLogger("apo.index")` warnings must land in
        the same rotated, timestamped file — via the logging hierarchy, no
        separate wiring in core.py."""
        watch._configure_logging(verbose=False)
        logging.getLogger("apo.index").warning(
            "section %s is %d bytes", "note.md#Body", 99999
        )
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("section note.md#Body is 99999 bytes", text)
        self.assertIn("[apo.index]", text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_unrelated_logger_not_captured(self):
        """Sanity check the hierarchy claim: a logger outside the "apo" tree
        must not land in the watcher's log."""
        watch._configure_logging(verbose=False)
        logging.getLogger("something_else").warning("should not appear")
        text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("should not appear", text)


class FormatIndexedSummaryTest(unittest.TestCase):
    """Pure-function unit tests for the batch-summary log line."""

    def test_small_batch_includes_paths(self):
        root = Path("/vault")
        ready = [root / "a.md", root / "b.md"]
        stats = QueueStats(indexed=2, purged=0, vault_stats=None)
        summary = watch._format_indexed_summary(stats, ready, root)
        self.assertEqual(summary, "2 file(s) [a.md, b.md]")

    def test_large_batch_is_count_only(self):
        root = Path("/vault")
        ready = [root / f"n{i}.md" for i in range(6)]
        stats = QueueStats(indexed=6, purged=0, vault_stats=None)
        summary = watch._format_indexed_summary(stats, ready, root)
        self.assertEqual(summary, "6 file(s)")

    def test_exactly_five_still_shows_paths(self):
        root = Path("/vault")
        ready = [root / f"n{i}.md" for i in range(5)]
        stats = QueueStats(indexed=5, purged=0, vault_stats=None)
        summary = watch._format_indexed_summary(stats, ready, root)
        self.assertIn("[n0.md, n1.md, n2.md, n3.md, n4.md]", summary)

    def test_indexed_without_ready_paths_falls_back_to_count(self):
        """`stats.indexed` can originate from queue consumption rather than
        the debounced `ready` batch (e.g. a rebuild) — no per-path info is
        available there, so this must degrade to a count, not crash."""
        root = Path("/vault")
        stats = QueueStats(indexed=3, purged=0, vault_stats=None)
        summary = watch._format_indexed_summary(stats, [], root)
        self.assertEqual(summary, "3 file(s)")

    def test_purge_only_reports_purged(self):
        root = Path("/vault")
        stats = QueueStats(indexed=0, purged=2, vault_stats=None)
        summary = watch._format_indexed_summary(stats, [], root)
        self.assertEqual(summary, "2 purged")

    def test_nothing_changed_returns_none(self):
        root = Path("/vault")
        stats = QueueStats(indexed=0, purged=0, vault_stats=None)
        self.assertIsNone(watch._format_indexed_summary(stats, [], root))


if __name__ == "__main__":
    unittest.main()
