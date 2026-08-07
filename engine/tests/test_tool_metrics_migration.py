"""One-time JSONL → DuckDB migration for tool metrics."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import tool_metrics


class ToolMetricsMigrationTest(unittest.TestCase):
    def test_migrate_jsonl_imports_and_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            jsonl = runtime / "tool-metrics-migrate.jsonl"
            jsonl.write_text(
                json.dumps(
                    {
                        "ts": "2026-01-15T12:00:00Z",
                        "tool": "search_notes",
                        "ok": True,
                        "duration_ms": 5.0,
                        "req_bytes": 10,
                        "resp_bytes": 100,
                        "folder_set": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db_path = runtime / "metrics.duckdb"
            with mock.patch.object(tool_metrics, "DEFERRED_DIR", runtime):
                events = tool_metrics.read_events("migrate", path=db_path)
                self.assertFalse(jsonl.is_file())
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["tool"], "search_notes")
                tool_metrics.record_call(
                    collection="migrate",
                    tool="append_note",
                    ok=True,
                    path=db_path,
                )
                stats = tool_metrics.tool_stats("migrate", days=None, path=db_path)
                self.assertEqual(stats["calls"], 2)
                by_tool = {t["tool"]: t for t in stats["by_tool"]}
                self.assertEqual(by_tool["search_notes"]["calls"], 1)
                self.assertEqual(by_tool["append_note"]["calls"], 1)

    def test_read_events_migrates_jsonl_before_db_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            jsonl = runtime / "tool-metrics-only.jsonl"
            jsonl.write_text(
                json.dumps(
                    {
                        "ts": "2026-01-15T12:00:00Z",
                        "tool": "search_notes",
                        "ok": True,
                        "duration_ms": 5.0,
                        "req_bytes": 10,
                        "resp_bytes": 100,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db_path = runtime / "metrics.duckdb"
            with mock.patch.object(tool_metrics, "DEFERRED_DIR", runtime):
                events = tool_metrics.read_events("only", path=db_path)
                self.assertFalse(jsonl.is_file())
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["tool"], "search_notes")

    def test_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            db_path = runtime / "metrics.duckdb"
            with mock.patch.object(tool_metrics, "DEFERRED_DIR", runtime):
                tool_metrics.record_call(
                    collection="once",
                    tool="read_note",
                    ok=True,
                    path=db_path,
                )
                tool_metrics.record_call(
                    collection="once",
                    tool="read_note",
                    ok=True,
                    path=db_path,
                )
                stats = tool_metrics.tool_stats("once", days=None, path=db_path)
                self.assertEqual(stats["calls"], 2)


if __name__ == "__main__":
    unittest.main()
