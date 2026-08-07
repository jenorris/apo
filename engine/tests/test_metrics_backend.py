"""Tests for pluggable metrics backends."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from apo_engine import metrics_backend as mb
from apo_engine import tool_metrics


class _FakeDeskHandler(BaseHTTPRequestHandler):
    events: list[dict] = []

    def log_message(self, fmt, *args):  # noqa: ARG002
        return

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path.rstrip("/") == "/v1/health":
            body = json.dumps({"ok": True, "service": "test"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        data = json.loads(raw.decode())
        path = urlparse(self.path).path.rstrip("/")
        if path == "/v1/events":
            _FakeDeskHandler.events.append(data)
            body = json.dumps({"ok": True}).encode()
        elif path == "/v1/query":
            body = json.dumps({"ok": True, "events": list(_FakeDeskHandler.events)}).encode()
        else:
            body = json.dumps({"ok": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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

    def test_local_backend_http(self):
        _FakeDeskHandler.events = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDeskHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            uri = f"http://127.0.0.1:{port}"
            backend = mb.LocalDeskMetricsBackend(uri)
            self.assertTrue(backend.status()["reachable"])
            backend.record(
                "work",
                {"ts": "2026-01-01T00:00:00Z", "tool": "search_notes", "ok": True},
            )
            events = backend.read_events("work")
            self.assertGreaterEqual(len(events), 1)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
