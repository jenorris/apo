"""Local-web HTTP viewer smoke tests."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import textwrap
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from apo_engine import local_web_contract as lwc
from apo_engine import render_html
from apo_engine.local_web import (
    LocalWebHandler,
    _clean_heading,
    _dedupe_hits,
    inject_live_chrome,
)


def _write_contract(vault: Path) -> Path:
    data = {
        "local_web_version": "0.2",
        "bind": "127.0.0.1",
        "port": 0,
        "browse_roots": ["areas"],
        "exclude_globs": ["inbox/daily/**"],
        "features": {"search": False},
        "render": {
            "default_layout": "memo",
            "resolve_wikilinks": "plain",
            "stylesheet": "missing.css",
            "template": "missing.html",
            "header": "missing-header.html",
        },
    }
    path = vault / lwc.LOCAL_WEB_CONTRACT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
class LocalWebHttpTests(unittest.TestCase):
    def setUp(self):
        render_html.clear_render_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-web-http-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "areas").mkdir()
        (self.vault / "areas" / "demo.md").write_text(
            textwrap.dedent(
                """\
                ---
                title: HTTP Demo
                ---
                # HTTP Demo

                Body text.
                """
            ),
            encoding="utf-8",
        )
        contract_path = _write_contract(self.vault)
        contract = lwc.load_local_web_contract(self.vault)
        assert contract is not None

        class _H(LocalWebHandler):
            pass

        _H.vault_name = "test"
        _H.vault_root = self.vault
        _H.contract = contract
        _H.features = lwc.resolve_features(contract)
        _H.contract_path = contract_path
        _H._contract_mtime_ns = contract_path.stat().st_mtime_ns

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        render_html.clear_render_cache()

    def _get(self, path: str, accept: str = "*/*") -> tuple[int, str, bytes, dict[str, str]]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path, headers={"Accept": accept})
        resp = conn.getresponse()
        body = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        headers = {k: v for k, v in resp.getheaders()}
        status = resp.status
        conn.close()
        return status, ctype, body, headers

    def test_health(self):
        status, ctype, body, _h = self._get("/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "apo-local-web")
        self.assertIn("cache", data)

    def test_note_html_live_chrome(self):
        status, ctype, body, headers = self._get("/note?path=areas/demo.md")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"HTTP Demo", body)
        self.assertIn(b"apo-live-bar", body)
        self.assertIn(b"/api/mtime", body)
        self.assertEqual(headers.get("X-Apo-Cached"), "0")
        self.assertFalse((self.vault / "areas" / "demo.html").exists())
        # Second hit should be cached
        status2, _c2, _b2, headers2 = self._get("/note?path=areas/demo.md")
        self.assertEqual(status2, 200)
        self.assertEqual(headers2.get("X-Apo-Cached"), "1")

    def test_api_mtime(self):
        status, ctype, body, _h = self._get("/api/mtime?path=areas/demo.md")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], "areas/demo.md")
        self.assertIsInstance(data["mtime_ns"], int)
        self.assertGreater(data["mtime_ns"], 0)
        self.assertEqual(data["stamp"], str(data["mtime_ns"]))

    def test_injected_stamp_matches_poll_stamp(self):
        """Guards the reload loop: ns mtimes lose precision as JSON numbers."""
        _s, _c, page, _h = self._get("/note?path=areas/demo.md")
        injected = re.search(rb'data-mtime="(\d+)"', page)
        assert injected is not None
        _s2, _c2, body, _h2 = self._get("/api/mtime?path=areas/demo.md")
        data = json.loads(body)
        self.assertEqual(injected.group(1).decode(), data["stamp"])
        # The poller must not compare against the lossy JSON number.
        self.assertIn(b"data.stamp", page)

    def test_api_render_json(self):
        status, ctype, body, _h = self._get(
            "/api/render?path=areas/demo.md", accept="application/json"
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIn("<html", data["html"])
        self.assertIn("mtime_ns", data)
        self.assertIn("cached", data)

    def test_api_render_html_accept(self):
        status, ctype, body, _h = self._get("/api/render?path=areas/demo.md", accept="text/html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"<html", body)
        self.assertIn(b"apo-live-bar", body)

    def test_forbidden(self):
        (self.vault / "inbox" / "daily").mkdir(parents=True)
        (self.vault / "inbox" / "daily" / "x.md").write_text("# x\n", encoding="utf-8")
        status, _ctype, body, _h = self._get("/note?path=inbox/daily/x.md")
        self.assertEqual(status, 403)
        self.assertIn(b"forbidden", body)

    def test_inject_live_chrome_helper(self):
        html = "<html><body><p>x</p></body></html>"
        out = inject_live_chrome(html, path="areas/demo.md", mtime_ns=123)
        self.assertIn('data-mtime="123"', out)
        self.assertIn("areas/demo.md", out)
        self.assertTrue(out.endswith("</html>") or "</body>" in out)


class SearchHitRenderTests(unittest.TestCase):
    def test_dedupe_keeps_first_of_each_chunk(self):
        hits = [
            {"chunk_hash": "a", "source": "policies/x.md", "score": 1.0},
            {"chunk_hash": "a", "source": "policies/x.md", "score": 0.4},
            {"chunk_hash": "b", "source": "policies/y.md", "score": 0.9},
        ]
        out = _dedupe_hits(hits)
        self.assertEqual([h["chunk_hash"] for h in out], ["a", "b"])
        self.assertEqual(out[0]["score"], 1.0)

    def test_dedupe_falls_back_to_source_heading(self):
        hits = [
            {"source": "a.md", "heading": "H"},
            {"source": "a.md", "heading": "H"},
            {"source": "a.md", "heading": "H2"},
            "not-a-dict",
        ]
        self.assertEqual(len(_dedupe_hits(hits)), 2)

    def test_clean_heading_strips_emphasis(self):
        self.assertEqual(
            _clean_heading("Access Control Policy › **3.0 Policy** › `code`"),
            "Access Control Policy › 3.0 Policy › code",
        )


if __name__ == "__main__":
    unittest.main()
