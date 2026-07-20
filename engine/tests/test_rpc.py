"""Local RPC HTTP smoke tests (no Ollama — fake embed + temp vault)."""

from __future__ import annotations

import hashlib
import json
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

    def test_read_and_filter(self):
        status, read = self._post("/v1/read", {"path": "note.md"})
        self.assertEqual(status, 200)
        self.assertTrue(read["ok"])
        self.assertIn("alpha widget", read["content"].lower())

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

        status, appended = self._post(
            "/v1/append",
            {"path": "inbox/rpc-write.md", "text": "- bullet\n", "heading": "Head"},
        )
        self.assertEqual(status, 200, appended)
        self.assertTrue(appended["ok"])

        status, patched = self._post(
            "/v1/patch",
            {
                "path": "inbox/rpc-write.md",
                "ops": [{"op": "set_field", "field": "status", "value": "open"}],
            },
        )
        self.assertEqual(status, 200, patched)
        self.assertTrue(patched["ok"])

        status, moved = self._post(
            "/v1/move",
            {"src": "inbox/rpc-write.md", "dst": "inbox/rpc-moved.md"},
        )
        self.assertEqual(status, 200, moved)
        self.assertTrue(moved["ok"])

        status, deleted = self._post("/v1/delete", {"path": "inbox/rpc-moved.md"})
        self.assertEqual(status, 200, deleted)
        self.assertTrue(deleted["ok"])
        self.assertFalse((self.vault / "inbox" / "rpc-moved.md").exists())

    def test_auth_required(self):
        status, body = self._get("/health", token="wrong")
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
