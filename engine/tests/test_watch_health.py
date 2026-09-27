"""Watch-loop hook health — persisted heartbeat + index_health() wiring.

Covers the "Problem B" fix in docs/watcher-scheduler-separation.md: each of
the watch loop's peripheral hooks (git-sync, optima-merge, desk-reproject) is
wrapped in try/except Exception, which only guards a call that *raises* — a
hang was previously invisible. `watch_health.HookHealthTracker` records
last-tick timestamp/duration/outcome per hook; `core.index_health()` surfaces
it so it reaches `apo-engine doctor` / MCP `index_health` without a new
capability.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apo_engine import watch_health

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from test_core import VaultTestCase  # noqa: E402


class HookHealthTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-hookhealth-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_ok_persists_fields(self):
        tracker = watch_health.HookHealthTracker(self.tmp)
        tracker.record(watch_health.GIT_SYNC, seconds=0.02, ok=True)
        hooks = watch_health.read_hook_health(self.tmp)
        row = hooks[watch_health.GIT_SYNC]
        self.assertTrue(row["ok"])
        self.assertIsNone(row["error"])
        self.assertAlmostEqual(row["last_tick_seconds"], 0.02, places=3)
        self.assertIn("last_tick_at", row)
        # ISO-8601, parseable
        datetime.fromisoformat(row["last_tick_at"])

    def test_record_failure_writes_immediately_bypassing_throttle(self):
        tracker = watch_health.HookHealthTracker(self.tmp)
        # First write establishes the throttle window.
        tracker.record(watch_health.OPTIMA_MERGE, seconds=0.01, ok=True)
        # A failure right after must not be swallowed by the 5s write-gap
        # throttle — a stall/error needs to be visible without delay.
        tracker.record(
            watch_health.OPTIMA_MERGE, seconds=1.5, ok=False, error="boom"
        )
        hooks = watch_health.read_hook_health(self.tmp)
        row = hooks[watch_health.OPTIMA_MERGE]
        self.assertFalse(row["ok"])
        self.assertEqual(row["error"], "boom")

    def test_ok_writes_are_throttled(self):
        tracker = watch_health.HookHealthTracker(self.tmp)
        tracker.record(watch_health.DESK_REPROJECT, seconds=0.01, ok=True)
        path = self.tmp / ".apo" / "watch-hooks.json"
        first_mtime = path.stat().st_mtime_ns
        # Immediate second "ok" tick — within the throttle window, should not
        # rewrite the file (bursty event-driven loops shouldn't hammer disk).
        tracker.record(watch_health.DESK_REPROJECT, seconds=0.02, ok=True)
        self.assertEqual(path.stat().st_mtime_ns, first_mtime)

    def test_read_missing_file_returns_empty_dict(self):
        self.assertEqual(watch_health.read_hook_health(self.tmp), {})

    def test_read_corrupt_file_returns_empty_dict(self):
        path = self.tmp / ".apo" / "watch-hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(watch_health.read_hook_health(self.tmp), {})


class HookHealthFlagsTest(unittest.TestCase):
    def test_errored_hook_flagged(self):
        hooks = {"git_sync": {"ok": False, "last_tick_at": _now_iso()}}
        flags = watch_health.hook_health_flags(hooks)
        self.assertIn("hook_error:git_sync", flags)

    def test_fresh_ok_hook_not_flagged(self):
        hooks = {"git_sync": {"ok": True, "last_tick_at": _now_iso()}}
        self.assertEqual(watch_health.hook_health_flags(hooks), [])

    def test_stale_ok_hook_flagged(self):
        stale_at = (
            datetime.now(timezone.utc) - timedelta(seconds=watch_health.STALE_AFTER_S + 5)
        ).isoformat(timespec="seconds")
        hooks = {"optima_merge": {"ok": True, "last_tick_at": stale_at}}
        flags = watch_health.hook_health_flags(hooks)
        self.assertIn("hook_stale:optima_merge", flags)

    def test_error_takes_precedence_over_staleness_check(self):
        stale_at = (
            datetime.now(timezone.utc) - timedelta(seconds=watch_health.STALE_AFTER_S + 5)
        ).isoformat(timespec="seconds")
        hooks = {"desk_reproject": {"ok": False, "last_tick_at": stale_at}}
        flags = watch_health.hook_health_flags(hooks)
        self.assertEqual(flags, ["hook_error:desk_reproject"])

    def test_empty_hooks_no_flags(self):
        self.assertEqual(watch_health.hook_health_flags({}), [])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IndexHealthHooksIntegrationTest(VaultTestCase):
    """`core.index_health()` surfaces the persisted hook snapshot per vault."""

    def test_hooks_key_present_and_populated(self):
        from apo_engine import core

        tracker = watch_health.HookHealthTracker(self.vault)
        tracker.record(watch_health.GIT_SYNC, seconds=0.05, ok=True)
        health = core.index_health()
        self.assertIn("hooks", health)
        self.assertIn(watch_health.GIT_SYNC, health["hooks"])
        self.assertTrue(health["hooks"][watch_health.GIT_SYNC]["ok"])

    def test_hook_error_surfaces_in_flags(self):
        from apo_engine import core

        tracker = watch_health.HookHealthTracker(self.vault)
        tracker.record(watch_health.OPTIMA_MERGE, seconds=0.1, ok=False, error="oops")
        health = core.index_health()
        self.assertIn("hook_error:optima_merge", health["flags"])

    def test_no_hooks_file_yet_is_empty_not_error(self):
        from apo_engine import core

        health = core.index_health()
        self.assertEqual(health.get("hooks"), {})


if __name__ == "__main__":
    unittest.main()
