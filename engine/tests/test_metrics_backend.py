"""Tests for pluggable metrics backends."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apo_engine import metrics_backend as mb


class MetricsBackendTest(unittest.TestCase):
    def test_embedded_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.duckdb"
            backend = mb.EmbeddedDuckDBBackend(db)
            st = backend.status()
            self.assertEqual(st["backend"], "embedded")
            backend.record("t", {"ts": "2026-01-01T00:00:00Z", "tool": "x", "ok": True})
            events = backend.read_events("t")
            self.assertEqual(len(events), 1)

    def test_local_backend_maps_to_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.duckdb"
            cfg = mb.resolve_store_config(Path(tmp))
            # contract/env "local" is normalized to embedded in v0.5.0
            import os

            old = os.environ.get("APO_METRICS_BACKEND")
            os.environ["APO_METRICS_BACKEND"] = "local"
            try:
                cfg2 = mb.resolve_store_config(Path(tmp))
                self.assertEqual(cfg2.backend, "embedded")
            finally:
                if old is None:
                    os.environ.pop("APO_METRICS_BACKEND", None)
                else:
                    os.environ["APO_METRICS_BACKEND"] = old
            backend = mb.get_backend(Path(tmp), force=True)
            self.assertEqual(backend.status()["backend"], "embedded")


if __name__ == "__main__":
    unittest.main()
