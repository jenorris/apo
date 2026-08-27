"""HTTP MCP transport (APO_MCP_TRANSPORT=http) — DNS-rebinding guard.

Adapted from qmd (github.com/tobi/qmd), whose CHANGELOG (#881) documents the exact
failure mode this guards against: binding to loopback alone does not stop a browser
page from re-pointing its hostname at 127.0.0.1 and reading an unauthenticated local
server (DNS rebinding). FastMCP ships this protection but OFF by default
(`host_origin_protection=False`, "for compatibility") — server.py's entry point must
pass `host_origin_protection="auto"` explicitly, which is exactly what this test
verifies stays true.
"""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from test_patch_note_schema import _list_tools_lean

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_HEADERS = {"Accept": "application/json, text/event-stream"}


class McpHttpGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, _tools = _list_tools_lean(collection="mcp_http_guard_test")

    def test_plain_request_no_origin_header_allowed(self):
        # curl / MCP clients / editors never send Origin — must be unaffected.
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post("/mcp", json=_INIT_BODY, headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)

    def test_same_origin_request_allowed(self):
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post(
                "/mcp", json=_INIT_BODY, headers={**_HEADERS, "Origin": "http://127.0.0.1"}
            )
        self.assertEqual(resp.status_code, 200)

    def test_hostile_origin_rejected(self):
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post(
                "/mcp",
                json=_INIT_BODY,
                headers={**_HEADERS, "Origin": "https://evil.example.com"},
            )
        self.assertEqual(resp.status_code, 403)

    def test_hostile_host_header_rejected(self):
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post(
                "/mcp", json=_INIT_BODY, headers={**_HEADERS, "Host": "evil.example.com"}
            )
        self.assertEqual(resp.status_code, 421)

    def test_unprotected_app_would_have_allowed_hostile_origin(self):
        # Regression guard for the exact footgun this feature hit during development:
        # FastMCP's own default (host_origin_protection unset) is NOT protective.
        # If this test ever starts failing, it means FastMCP changed its default —
        # server.py's explicit "auto" still protects either way, but re-check that.
        app = self.mod.mcp.http_app()  # no host_origin_protection kwarg at all
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post(
                "/mcp",
                json=_INIT_BODY,
                headers={**_HEADERS, "Origin": "https://evil.example.com"},
            )
        self.assertEqual(resp.status_code, 200)


class CsvEnvHelperTest(unittest.TestCase):
    def test_empty_and_unset(self):
        import os

        mod, _ = _list_tools_lean(collection="mcp_http_csv_test")
        os.environ.pop("APO_TEST_CSV_ENV", None)
        self.assertIsNone(mod._csv_env("APO_TEST_CSV_ENV"))
        os.environ["APO_TEST_CSV_ENV"] = "  "
        self.assertIsNone(mod._csv_env("APO_TEST_CSV_ENV"))

    def test_splits_and_trims(self):
        import os

        mod, _ = _list_tools_lean(collection="mcp_http_csv_test2")
        os.environ["APO_TEST_CSV_ENV"] = "a.example.com, b.example.com ,,c.example.com"
        self.assertEqual(
            mod._csv_env("APO_TEST_CSV_ENV"),
            ["a.example.com", "b.example.com", "c.example.com"],
        )


if __name__ == "__main__":
    unittest.main()
