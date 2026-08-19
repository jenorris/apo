"""Local RPC HTTP smoke tests (no Ollama — fake embed + temp vault)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from apo_engine import config, core, rpc

_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


class TestLocalRpc(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text(
            "---\ntitle: Alpha\nstatus: open\n---\n\n# Alpha\n\nalpha widget body\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.index),
            unittest.mock.patch.object(config, "COLLECTION", "rpc_test"),
            unittest.mock.patch.object(config, "VAULTS_CONFIG", ""),
            unittest.mock.patch.object(core, "embed", _fake_embed),
            unittest.mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

        self.port = self._free_port()
        self.token = "test-token"
        rpc.RpcHandler.rpc_token = self.token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), rpc.RpcHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _free_port() -> int:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _post(self, path: str, body: dict, *, token: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token if token is not None else self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get(self, path: str, *, token: str | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": f"Bearer {token if token is not None else self.token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health_and_search(self):
        status, health = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "apo-engine-rpc")

        status, search = self._post("/v1/search", {"query": "alpha widget", "top_k": 3})
        self.assertEqual(status, 200)
        self.assertTrue(search["ok"])
        self.assertGreaterEqual(len(search["results"]), 1)
        self.assertIn("alpha", search["results"][0]["content"].lower())

    def test_instructions_matches_mcp_handshake_text(self):
        """The stdio MCP server hands this text to clients in its handshake;
        HTTP-only RPC clients (no stdio transport) have no other way to get it."""
        from apo_engine.mcp_instructions import MCP_INSTRUCTIONS

        status, out = self._get("/v1/instructions")
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["instructions"], MCP_INSTRUCTIONS)

    def test_read_note_with_unquoted_date_frontmatter(self):
        """Regression: a YAML date in frontmatter must not kill the connection.

        `timestamp: 2026-08-12` loads as a `datetime.date`; before the
        `_json_default` coercion, json.dumps raised out of `_dispatch` and the
        handler thread died with no bytes written (client saw a reset socket).
        """
        (self.vault / "dated.md").write_text(
            "---\ntitle: Dated\ntimestamp: 2026-08-12\nupdated: 2026-08-12 07:30:00\n---\n\nbody\n",
            encoding="utf-8",
        )
        status, read = self._post("/v1/read", {"path": "dated.md"})
        self.assertEqual(status, 200)
        self.assertTrue(read["ok"])
        self.assertEqual(read["frontmatter"]["timestamp"], "2026-08-12")
        self.assertEqual(read["frontmatter"]["updated"], "2026-08-12T07:30:00")

    def test_read_and_filter(self):
        status, read = self._post("/v1/read", {"path": "note.md"})
        self.assertEqual(status, 200)
        self.assertTrue(read["ok"])
        self.assertIn("alpha widget", read["content"].lower())
        self.assertNotIn("title: Alpha", read["content"])
        self.assertEqual(read["frontmatter"]["title"], "Alpha")
        self.assertEqual(read["frontmatter"]["status"], "open")

        status, raw = self._post("/v1/read", {"path": "note.md", "raw": True})
        self.assertEqual(status, 200)
        self.assertTrue(raw["ok"])
        self.assertIn("title: Alpha", raw["content"])
        self.assertEqual(raw["frontmatter"]["title"], "Alpha")

        status, filt = self._post(
            "/v1/filter",
            {"where": {"status": "open"}, "limit": 10},
        )
        self.assertEqual(status, 200)
        self.assertTrue(filt["ok"])
        self.assertGreaterEqual(filt["total"], 1)

    def test_write_append_patch_delete(self):
        status, written = self._post(
            "/v1/write",
            {
                "path": "inbox/rpc-write.md",
                "content": "---\ntitle: RPC Write\n---\n\n# Head\n\nbody\n",
            },
        )
        self.assertEqual(status, 200, written)
        self.assertTrue(written["ok"])
        self.assertEqual(written["action"], "created")

        status, rejected = self._post(
            "/v1/write",
            {
                "path": "inbox/rpc-write.md",
                "content": "tail\n",
                "append": True,
            },
        )
        self.assertEqual(status, 400, rejected)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "append_removed")

        status, appended = self._post(
            "/v1/append",
            {"path": "inbox/rpc-write.md", "text": "- bullet\n", "heading": "Head"},
        )
        self.assertEqual(status, 200, appended)
        self.assertTrue(appended["ok"])

        status, appended_alias = self._post(
            "/v1/append",
            {
                "path": "inbox/rpc-write.md",
                "content": "- via content alias\n",
                "heading": "Head",
            },
        )
        self.assertEqual(status, 200, appended_alias)
        self.assertTrue(appended_alias["ok"])
        self.assertIn("content=", appended_alias.get("tip") or "")

        status, written_alias = self._post(
            "/v1/write",
            {
                "path": "inbox/rpc-write-alias.md",
                "text": "---\ntitle: Alias Write\n---\n\n# Head\n\nbody\n",
            },
        )
        self.assertEqual(status, 200, written_alias)
        self.assertTrue(written_alias["ok"])
        self.assertIn("text=", written_alias.get("tip") or "")

        status, conflict = self._post(
            "/v1/append",
            {
                "path": "inbox/rpc-write.md",
                "text": "a\n",
                "content": "b\n",
                "heading": "Head",
            },
        )
        self.assertEqual(status, 400, conflict)
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"], "bad_request")
        self.assertIn("conflicting", conflict.get("message") or "")

        status, patched = self._post(
            "/v1/patch",
            {
                "path": "inbox/rpc-write.md",
                "ops": [{"op": "set_field", "field": "status", "value": "open"}],
            },
        )
        self.assertEqual(status, 200, patched)
        self.assertTrue(patched["ok"])

        mtime = patched["mtime"]
        status, stale = self._post(
            "/v1/move",
            {
                "src": "inbox/rpc-write.md",
                "dst": "inbox/rpc-moved.md",
                "expected_mtime": mtime - 10,
            },
        )
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale["error"], "stale_write")

        status, moved = self._post(
            "/v1/move",
            {
                "src": "inbox/rpc-write.md",
                "dst": "inbox/rpc-moved.md",
                "expected_mtime": mtime,
            },
        )
        self.assertEqual(status, 200, moved)
        self.assertTrue(moved["ok"])

        status, deleted = self._post("/v1/delete", {"path": "inbox/rpc-moved.md"})
        self.assertEqual(status, 200, deleted)
        self.assertTrue(deleted["ok"])
        self.assertFalse((self.vault / "inbox" / "rpc-moved.md").exists())

    def test_place_note_promotes_host_md(self):
        host = self.tmp / "host-report.md"
        host.write_text("---\ntitle: Host\n---\n\n# Host\n\npromoted\n", encoding="utf-8")
        with unittest.mock.patch.object(config, "SEND_ALLOW_ROOTS", str(self.tmp.resolve())):
            status, placed = self._post(
                "/v1/place",
                {
                    "src": str(host),
                    "dst": "resources/wiki/host-report.md",
                    "fields": {"source": "rpc-test"},
                },
            )
        self.assertEqual(status, 200, placed)
        self.assertTrue(placed["ok"], placed)
        self.assertEqual(placed.get("mode"), "copy")
        self.assertTrue(host.exists())
        dest = self.vault / "resources" / "wiki" / "host-report.md"
        self.assertTrue(dest.is_file())
        self.assertIn("source: rpc-test", dest.read_text(encoding="utf-8"))

    def test_patch_scratchpad_promote(self):
        spill = self.tmp / "spill"
        spill.mkdir()
        prev = os.environ.get("APO_SCRATCHPADS_ROOT")
        os.environ["APO_SCRATCHPADS_ROOT"] = str(spill)
        try:
            status, created = self._post(
                "/v1/scratchpad",
                {
                    "action": "create",
                    "format": "markdown",
                    "content": "# Rpc\nbody\n",
                },
            )
            self.assertEqual(status, 200, created)
            self.assertTrue(created["ok"], created)
            sid = created["session_id"]
            status, patched = self._post(
                "/v1/patch",
                {
                    "path": "inbox/rpc-spill.md",
                    "scratchpad": sid,
                    "ops": [{"op": "set_field", "field": "status", "value": "open"}],
                },
            )
            self.assertEqual(status, 200, patched)
            self.assertTrue(patched["ok"], patched)
            self.assertEqual(patched.get("scratchpad_state"), "PROMOTED")
            dest = self.vault / "inbox" / "rpc-spill.md"
            self.assertTrue(dest.is_file())
            self.assertIn("status: open", dest.read_text(encoding="utf-8"))
        finally:
            if prev is None:
                os.environ.pop("APO_SCRATCHPADS_ROOT", None)
            else:
                os.environ["APO_SCRATCHPADS_ROOT"] = prev

    def test_search_prefers_limit(self):
        status, search = self._post("/v1/search", {"query": "alpha widget", "limit": 3})
        self.assertEqual(status, 200)
        self.assertTrue(search["ok"])
        self.assertGreaterEqual(len(search["results"]), 1)

    def test_auth_required(self):
        status, body = self._get("/health", token="wrong")
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
