"""Unit tests for tool-use analytics JSONL + rollups."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apo_engine import tool_metrics


class ToolMetricsTest(unittest.TestCase):
    def test_record_and_rollup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tool-metrics-test.jsonl"
            tool_metrics.record_call(
                collection="test",
                tool="search_notes",
                ok=True,
                duration_ms=12.5,
                req_bytes=40,
                resp_bytes=800,
                flags={"folder_set": True, "used_alias": True},
                path=path,
            )
            tool_metrics.record_call(
                collection="test",
                tool="filter_notes",
                ok=False,
                error="bad_query",
                duration_ms=3.0,
                req_bytes=20,
                resp_bytes=50,
                flags={"fields_set": True},
                path=path,
            )
            tool_metrics.record_call(
                collection="test",
                tool="search_notes",
                ok=True,
                duration_ms=8.0,
                req_bytes=30,
                resp_bytes=400,
                flags={"expected_mtime_set": True},
                path=path,
            )
            tool_metrics.record_call(
                collection="test",
                tool="patch_note",
                ok=False,
                error="validation_error",
                duration_ms=2.0,
                req_bytes=10,
                resp_bytes=20,
                flags={"error_shape": ["union_tag_not_found:ops.0", "missing:op"]},
                path=path,
            )
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 4)
            row = json.loads(lines[0])
            self.assertEqual(row["tool"], "search_notes")
            self.assertTrue(row["ok"])
            self.assertNotIn("path", row)
            self.assertNotIn("content", row)

            stats = tool_metrics.tool_stats("test", days=None, path=path)
            self.assertEqual(stats["calls"], 4)
            self.assertEqual(stats["ok_count"], 2)
            self.assertEqual(stats["error_count"], 2)
            self.assertEqual(stats["by_error"]["bad_query"], 1)
            self.assertEqual(stats["by_error"]["validation_error"], 1)
            self.assertEqual(stats["by_error_shape"]["union_tag_not_found:ops.0"], 1)
            self.assertEqual(stats["by_error_shape"]["missing:op"], 1)
            by_tool = {t["tool"]: t for t in stats["by_tool"]}
            self.assertEqual(by_tool["search_notes"]["calls"], 2)
            self.assertEqual(by_tool["filter_notes"]["error"], 1)
            self.assertEqual(
                by_tool["patch_note"]["by_error_shape"]["union_tag_not_found:ops.0"],
                1,
            )

    def test_extract_arg_flags(self):
        flags = tool_metrics.extract_arg_flags(
            {
                "top_k": 5,
                "folder": "areas/threads",
                "fields": ["status"],
                "expected_mtime": 1.0,
                "ops": [{"op": "set_field", "field": "status", "value": "a"}],
                "path": "secret.md",
                "text": "should not appear",
            }
        )
        self.assertTrue(flags["used_alias"])
        self.assertTrue(flags["folder_set"])
        self.assertTrue(flags["fields_set"])
        self.assertTrue(flags["expected_mtime_set"])
        self.assertEqual(flags["ops_count"], 1)
        self.assertNotIn("path", flags)
        self.assertNotIn("text", flags)

    def test_extract_arg_flags_body_aliases(self):
        append_flags = tool_metrics.extract_arg_flags(
            {"path": "a.md", "content": "body"},
            tool="append_note",
        )
        self.assertTrue(append_flags["used_alias"])
        write_flags = tool_metrics.extract_arg_flags(
            {"path": "a.md", "text": "body"},
            tool="write_note",
        )
        self.assertTrue(write_flags["used_alias"])
        # Canonical-only append should not count as alias.
        canonical = tool_metrics.extract_arg_flags(
            {"path": "a.md", "text": "body"},
            tool="append_note",
        )
        self.assertNotIn("used_alias", canonical)

    def test_days_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            old = {
                "ts": "2020-01-01T00:00:00Z",
                "tool": "search_notes",
                "ok": True,
                "duration_ms": 1,
                "req_bytes": 1,
                "resp_bytes": 1,
            }
            path.write_text(json.dumps(old) + "\n", encoding="utf-8")
            tool_metrics.record_call(
                collection="t",
                tool="append_note",
                ok=True,
                path=path,
            )
            stats = tool_metrics.tool_stats("t", days=7, path=path)
            self.assertEqual(stats["calls"], 1)
            self.assertEqual(stats["by_tool"][0]["tool"], "append_note")


if __name__ == "__main__":
    unittest.main()
