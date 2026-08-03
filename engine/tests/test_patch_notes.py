"""patch_notes — same-vault multi-path patch batch."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from apo_engine import config, ops, rpc


class PatchNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-pn-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "a.md").write_text(
            "---\nstatus: active\n---\n\n# A\n\nbody a\n", encoding="utf-8"
        )
        (self.vault / "b.md").write_text(
            "---\nstatus: active\n---\n\n# B\n\nbody b\n", encoding="utf-8"
        )
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            unittest.mock.patch.object(config, "COLLECTION", "pn_test"),
            unittest.mock.patch.object(config, "VAULTS_CONFIG", ""),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batch_ok(self):
        out = ops.patch_notes(
            [
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "done"}],
                },
                {
                    "path": "b.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "waiting"}],
                },
            ]
        )
        self.assertTrue(out["ok"], msg=out)
        self.assertFalse(out["partial"])
        self.assertEqual(out["applied_paths"], 2)
        self.assertEqual(out["failed_paths"], 0)
        self.assertIn("done", (self.vault / "a.md").read_text(encoding="utf-8"))
        self.assertIn("waiting", (self.vault / "b.md").read_text(encoding="utf-8"))

    def test_partial_continues(self):
        out = ops.patch_notes(
            [
                {
                    "path": "missing.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "x"}],
                },
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "done"}],
                },
            ]
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["partial"])
        self.assertEqual(out["applied_paths"], 1)
        self.assertEqual(out["failed_paths"], 1)
        self.assertEqual(out["error"], "batch_partial")
        self.assertIn("done", (self.vault / "a.md").read_text(encoding="utf-8"))

    def test_duplicate_path_rejected(self):
        out = ops.patch_notes(
            [
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "one"}],
                },
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "two"}],
                },
            ]
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_paths"], 1)
        self.assertEqual(out["results"][1]["error"], "duplicate_path")

    def test_xor_rejects_both(self):
        out = ops.patch_entry(
            path="a.md",
            ops=[{"op": "set_field", "field": "status", "value": "x"}],
            items=[
                {
                    "path": "b.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "y"}],
                }
            ],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_items_via_patch_entry(self):
        out = ops.patch_entry(
            items=[
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "done"}],
                },
            ]
        )
        self.assertTrue(out["ok"], msg=out)
        self.assertEqual(out["applied_paths"], 1)


class PatchNotesRpcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-pn-rpc-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "n.md").write_text("---\nx: 1\n---\n\n# N\n", encoding="utf-8")
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            unittest.mock.patch.object(config, "COLLECTION", "pn_rpc"),
            unittest.mock.patch.object(config, "VAULTS_CONFIG", ""),
        ]
        for p in self._patches:
            p.start()
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        rpc.RpcHandler.rpc_token = "pn-token"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), rpc.RpcHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rpc_patch_notes(self):
        data = json.dumps(
            {
                "items": [
                    {
                        "path": "n.md",
                        "ops": [{"op": "set_field", "field": "x", "value": 2}],
                    }
                ]
            }
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/patch_notes",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer pn-token",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        self.assertTrue(body["ok"], msg=body)
        text = (self.vault / "n.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"x:\s*['\"]?2['\"]?")


if __name__ == "__main__":
    unittest.main()
