"""Unit tests for habit KPI rollups (vault action=stats)."""

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
            self.assertGreaterEqual(eff["read_patterns"]["read_note_chunk_calls"], 1)

    def test_telemetry_bad_action(self):
        out = telemetry_ops.telemetry("nope")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_action")

    def test_telemetry_efficiency_delegates_to_vault_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metrics.duckdb"
            tool_metrics.record_call(
                collection="eff2",
                tool="search_notes",
                ok=True,
                flags={"folder_set": True},
                path=db_path,
            )
            out = telemetry_ops.telemetry(
                "efficiency",
                collection="eff2",
                vault_root=Path(tmp),
            )
            self.assertTrue(out["ok"])
            self.assertEqual(out["action"], "stats")

    def test_vault_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metrics.duckdb"
            tool_metrics.record_call(
                collection="vs",
                tool="filter_notes",
                ok=True,
                flags={"fields_set": True},
                path=db_path,
            )
            out = telemetry_ops.vault_stats(
                vault_root=Path(tmp),
                collection="vs",
                days=7,
            )
            self.assertTrue(out["ok"])
            self.assertEqual(out["action"], "stats")
            self.assertIn("search_notes", out)
            self.assertEqual(out["store"]["backend"], "embedded")

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
