"""Unit tests for unified telemetry MCP/RPC actions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apo_engine import telemetry_ops, tool_metrics


class TelemetryOpsTest(unittest.TestCase):
    def test_efficiency_rollups(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metrics.duckdb"
            for i in range(5):
                tool_metrics.record_call(
                    collection="eff",
                    tool="search_notes",
                    ok=True,
                    duration_ms=100.0,
                    flags={"folder_set": i < 2},
                    path=db_path,
                )
            tool_metrics.record_call(
                collection="eff",
                tool="expand_section",
                ok=True,
                duration_ms=5.0,
                path=db_path,
            )
            tool_metrics.record_call(
                collection="eff",
                tool="patch_note",
                ok=False,
                error="validation_error",
                flags={"error_shape": ["missing:op"]},
                path=db_path,
            )
            events = tool_metrics.read_events("eff", path=db_path)
            eff = telemetry_ops.compute_efficiency(events)
            self.assertTrue(eff["ok"])
            self.assertEqual(eff["search_notes"]["calls"], 5)
            self.assertEqual(eff["search_notes"]["folder_scoped_pct"], 40.0)
            self.assertGreater(len(eff["tips"]), 0)

    def test_telemetry_bad_action(self):
        out = telemetry_ops.telemetry("nope")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_action")

    def test_admin_collection_rejects_agent_surface(self):
        out = telemetry_ops.telemetry("collection", surface="agent")
        self.assertFalse(out["ok"])
        self.assertIn("hint", out)

    def test_telemetry_status(self):
        out = telemetry_ops.telemetry("status")
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "status")
        self.assertIn("store", out)
        self.assertEqual(out["store"]["backend"], "embedded")

    def test_telemetry_active(self):
        out = telemetry_ops.telemetry("active")
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "active")

    def test_collection_rollup_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metrics.duckdb"
            tool_metrics.record_call(
                collection="coll",
                tool="filter_notes",
                ok=True,
                flags={"fields_set": True},
                path=db_path,
            )
            events = tool_metrics.read_events("coll", path=db_path)
            rollup = tool_metrics.rollup_events(events)
            self.assertEqual(rollup["calls"], 1)
            self.assertEqual(rollup["flags"]["fields_set"], 1)


if __name__ == "__main__":
    unittest.main()
