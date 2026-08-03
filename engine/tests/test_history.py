"""history + git-contract file log."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from apo_engine import config, core, ops, rpc
from apo_engine import git_contract

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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "inbox").mkdir()
        (self.vault / "note.md").write_text("# Hello\n\nbody\n", encoding="utf-8")
        (self.vault / "inbox" / "a.md").write_text("# A\n", encoding="utf-8")
        self.index = self.tmp / "index.db"
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.index),
            unittest.mock.patch.object(config, "COLLECTION", "history_test"),
            unittest.mock.patch.object(config, "VAULTS_CONFIG", ""),
            unittest.mock.patch.object(core, "embed", _fake_embed),
            unittest.mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_browse(self):
        h = ops.history(limit=5)
        self.assertTrue(h["ok"])
        self.assertGreaterEqual(len(h["notes"]), 1)
        self.assertIn("path", h["notes"][0])
        self.assertIn("modified", h["notes"][0])
        self.assertIn("first_line", h["notes"][0])

    def test_browse_folder(self):
        h = ops.history(limit=5, folder="inbox")
        self.assertTrue(h["ok"])
        self.assertTrue(all(n["path"].startswith("inbox/") for n in h["notes"]))

    def test_file_mtime_fallback_without_git_contract(self):
        out = ops.history(path="note.md", limit=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["source"], "mtime")
        self.assertEqual(out["path"], "note.md")
        self.assertIn("modified", out)
        self.assertNotIn("commits", out)

    def test_file_git_log_with_contract(self):
        _git(self.vault, "init")
        _git(self.vault, "config", "user.email", "test@example.com")
        _git(self.vault, "config", "user.name", "Test")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "initial note")
        (self.vault / "note.md").write_text("# Hello\n\nbody v2\n", encoding="utf-8")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "update note")

        cfg = self.vault / "system" / "config"
        cfg.mkdir(parents=True)
        (cfg / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: 'https://example.com/r.git'\nhost: local\n",
            encoding="utf-8",
        )
        self.assertTrue(git_contract.git_contract_active(self.vault))

        out = ops.history(path="note.md", limit=10)
        self.assertTrue(out["ok"], msg=out)
        self.assertEqual(out["source"], "git")
        self.assertGreaterEqual(len(out["commits"]), 2)
        subjects = [c["subject"] for c in out["commits"]]
        self.assertIn("update note", subjects)
        self.assertIn("initial note", subjects)
        for c in out["commits"]:
            self.assertIn("hash", c)
            self.assertIn("author", c)
            self.assertIn("date", c)
            self.assertIn("subject", c)

    def test_nested_vault_under_parent_git(self):
        """Meta-style: contract on vault subdir; .git on parent (foam)."""
        parent = self.tmp / "notes"
        parent.mkdir()
        vault = parent / "Meta"
        vault.mkdir()
        (vault / "note.md").write_text("# Nested\n", encoding="utf-8")
        cfg = vault / "system" / "config"
        cfg.mkdir(parents=True)
        (cfg / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: 'https://example.com/foam.git'\nhost: github\n",
            encoding="utf-8",
        )
        _git(parent, "init")
        _git(parent, "config", "user.email", "test@example.com")
        _git(parent, "config", "user.name", "Test")
        _git(parent, "add", "Meta/note.md")
        _git(parent, "commit", "-m", "add meta note")

        self.assertTrue(git_contract.is_git_work_tree(vault))
        self.assertTrue(git_contract.git_contract_active(vault))
        commits = git_contract.git_file_log(vault, "note.md", limit=5)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["subject"], "add meta note")

        with unittest.mock.patch.object(config, "NOTES_ROOT", vault):
            with unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "nested-index.db"):
                with unittest.mock.patch.object(config, "COLLECTION", "nested_hist"):
                    with unittest.mock.patch.object(config, "VAULTS_CONFIG", ""):
                        core.index_vault(rebuild=True, verbose=False)
                        out = ops.history(path="note.md", limit=5)
        self.assertTrue(out["ok"], msg=out)
        self.assertEqual(out["source"], "git")
        self.assertEqual(out["commits"][0]["subject"], "add meta note")


class TestHistoryRpc(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# Alpha\n\nalpha\n", encoding="utf-8")
        self.index = self.tmp / "index.db"
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.index),
            unittest.mock.patch.object(config, "COLLECTION", "history_rpc"),
            unittest.mock.patch.object(config, "VAULTS_CONFIG", ""),
            unittest.mock.patch.object(core, "embed", _fake_embed),
            unittest.mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.token = "hist-token"
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

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_history(self):
        s1, h = self._post("/v1/history", {"limit": 3})
        self.assertEqual(s1, 200)
        self.assertTrue(h["ok"])
        self.assertGreaterEqual(len(h["notes"]), 1)


if __name__ == "__main__":
    unittest.main()
