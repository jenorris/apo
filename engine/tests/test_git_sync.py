"""Git contract sync — commit/push/pull + never_commit gates."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from apo_engine import config, git_sync, ops, rpc


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _write_contract(vault: Path, *, enabled: bool = True, extra: str = "") -> None:
    cfg = vault / "system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    body = (
        "git_contract_version: '0.1'\n"
        "remote: 'https://example.com/r.git'\n"
        "host: local\n"
        "default_branch: main\n"
        "never_commit:\n"
        "  - '*.db'\n"
        "  - '.apo/'\n"
        "  - '.env'\n"
        "sync:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        "  debounce_seconds: 2\n"
        "  pull_interval_seconds: 60\n"
        "  commit_message_template: 'apo: sync {iso_local}'\n"
        "  auto_push: true\n"
    )
    if extra:
        body += extra
    (cfg / "git-contract.schema.yaml").write_text(body, encoding="utf-8")


class GitSyncHelpersTest(unittest.TestCase):
    def test_never_commit_globs(self):
        pats = ("*.db", ".apo/", "**/Passport*.key", ".env")
        self.assertTrue(git_sync.path_never_commit("index.db", pats))
        self.assertTrue(git_sync.path_never_commit(".apo/git-sync-status.json", pats))
        self.assertTrue(git_sync.path_never_commit("secrets/PassportFoo.key", pats))
        self.assertTrue(git_sync.path_never_commit(".env", pats))
        self.assertFalse(git_sync.path_never_commit("inbox/a.md", pats))

    def test_format_commit_message(self):
        dt = datetime(2026, 8, 3, 0, 35, tzinfo=ZoneInfo("America/New_York"))
        msg = git_sync.format_commit_message("apo: sync {iso_local}", now=dt)
        self.assertEqual(msg, "apo: sync 2026-08-03 00:35 ET")


class GitSyncRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-gsync-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# N\n\nv1\n", encoding="utf-8")
        _git(self.vault, "init", "-b", "main")
        _git(self.vault, "config", "user.email", "test@example.com")
        _git(self.vault, "config", "user.name", "Test")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "initial")
        self.bare = self.tmp / "remote.git"
        _git(self.tmp, "init", "--bare", str(self.bare))
        _git(self.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(self.vault, "remote", "add", "origin", str(self.bare))
        _git(self.vault, "push", "-u", "origin", "main")
        _write_contract(self.vault, enabled=True)
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            unittest.mock.patch.object(config, "COLLECTION", "gsync_test"),
            unittest.mock.patch.object(config, "VAULTS_CONFIG", ""),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_commit_push_template_and_never_commit(self):
        (self.vault / "note.md").write_text("# N\n\nv2\n", encoding="utf-8")
        (self.vault / "secret.db").write_text("nope", encoding="utf-8")
        (self.vault / ".apo").mkdir(exist_ok=True)
        (self.vault / ".apo" / "x.json").write_text("{}", encoding="utf-8")

        out = git_sync.commit_and_push(self.vault)
        self.assertTrue(out["ok"], msg=out)
        self.assertTrue(out["committed"])
        self.assertTrue(out["pushed"])
        self.assertTrue(out["message"].startswith("apo: sync "))
        self.assertIn("note.md", out["paths"])
        self.assertNotIn("secret.db", out["paths"])

        log = _git(self.vault, "log", "-1", "--name-only", "--pretty=%s")
        self.assertIn("note.md", log.stdout)
        self.assertNotIn("secret.db", log.stdout)

    def test_commits_paths_git_would_quote_or_glob(self):
        # core.quotePath C-escapes non-ASCII names in porcelain output, and
        # glob metacharacters are pathspec patterns unless passed literally.
        # Either one previously failed `git add` and blocked the whole vault.
        awkward = [
            "tasks/Open Items Triage \u2014 2026-04-28.md",
            "tasks/report [2026] draft.md",
            "tasks/caf\u00e9 notes.md",
        ]
        (self.vault / "tasks").mkdir()
        for rel in awkward:
            (self.vault / rel).write_text("x\n", encoding="utf-8")
        (self.vault / "note.md").write_text("# N\n\nv4\n", encoding="utf-8")

        out = git_sync.commit_and_push(self.vault)
        self.assertTrue(out["ok"], msg=out)
        self.assertTrue(out["committed"], msg=out)
        for rel in awkward:
            self.assertIn(rel, out["paths"])

        tracked = _git(self.vault, "ls-files", "-z").stdout.split("\0")
        dirty = _git(self.vault, "status", "--porcelain", "-z", "-u").stdout
        for rel in awkward:
            self.assertIn(rel, tracked)
            self.assertNotIn(rel, dirty)

    def test_stages_renames_without_consuming_origin_as_entry(self):
        _git(self.vault, "mv", "note.md", "renamed \u2014 note.md")
        paths = git_sync.list_stageable_paths(self.vault, ())
        self.assertIn("renamed \u2014 note.md", paths)
        self.assertNotIn("note.md", paths)

    def test_on_block_command_fires_once_per_block_episode(self):
        sentinel = self.tmp / "notified.log"
        _write_contract(
            self.vault,
            enabled=True,
            extra=f"  on_block_command: 'printf \"%s\\n\" \"$APO_SYNC_ERROR\" >> {sentinel}'\n",
        )

        git_sync._block(self.vault, "boom: push rejected")
        self.assertEqual(sentinel.read_text(encoding="utf-8").splitlines(), ["boom: push rejected"])

        # Still blocked → no repeat alert on every later tick.
        git_sync._block(self.vault, "boom: push rejected")
        self.assertEqual(sentinel.read_text(encoding="utf-8").splitlines(), ["boom: push rejected"])

        # Cleared, then blocked again → new episode, new alert.
        git_sync.clear_block(self.vault)
        git_sync._block(self.vault, "boom: second episode")
        self.assertEqual(
            sentinel.read_text(encoding="utf-8").splitlines(),
            ["boom: push rejected", "boom: second episode"],
        )

    def test_block_survives_failing_on_block_command(self):
        _write_contract(self.vault, enabled=True, extra="  on_block_command: 'exit 7'\n")
        st = git_sync._block(self.vault, "boom")
        self.assertEqual(st["state"], "blocked")
        self.assertTrue(git_sync.is_blocked(self.vault))

    def test_tool_message_override(self):
        (self.vault / "note.md").write_text("# N\n\nv3\n", encoding="utf-8")
        out = ops.git_sync_op("run", message="agent: batch sync")
        self.assertTrue(out["ok"], msg=out)
        self.assertEqual(out["message"], "agent: batch sync")

    def test_ff_only_pull_blocks_on_diverge(self):
        # Diverging remote tip (fetch into vault after remote advances)
        clone = self.tmp / "other"
        _git(self.tmp, "clone", str(self.bare), str(clone))
        _git(clone, "config", "user.email", "test@example.com")
        _git(clone, "config", "user.name", "Test")
        (clone / "note.md").write_text("# remote\n", encoding="utf-8")
        _git(clone, "add", "note.md")
        _git(clone, "commit", "-m", "remote change")
        _git(clone, "push", "origin", "HEAD:main")

        (self.vault / "note.md").write_text("# local diverge\n", encoding="utf-8")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "local change")

        out = git_sync.pull_ff_only(self.vault)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "pull_failed")
        self.assertEqual(git_sync.read_status(self.vault)["state"], "blocked")

        # Further run refused until clear
        (self.vault / "extra.md").write_text("# x\n", encoding="utf-8")
        blocked = git_sync.commit_and_push(self.vault)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "blocked")

        cleared = ops.git_sync_op("clear_block")
        self.assertTrue(cleared["ok"])
        self.assertNotEqual(cleared["status"]["state"], "blocked")

    def test_sync_disabled_status(self):
        _write_contract(self.vault, enabled=False)
        st = ops.git_sync_op("status")
        self.assertTrue(st["ok"])
        self.assertFalse(st["sync_enabled"])

    def test_controller_debounce_commit(self):
        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        (self.vault / "note.md").write_text("# N\n\ndebounced\n", encoding="utf-8")
        ctl.note_apo_writes()
        self.assertTrue(ctl.pending_commit())
        # Force due immediately
        ctl._commit_due_at = 0.0  # noqa: SLF001 — test hook
        ctl.tick(index_busy=False)
        log = _git(self.vault, "log", "-1", "--pretty=%s")
        self.assertTrue(log.stdout.strip().startswith("apo: sync "))


class GitSyncRpcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-gsync-rpc-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# rpc\n", encoding="utf-8")
        _git(self.vault, "init", "-b", "main")
        _git(self.vault, "config", "user.email", "test@example.com")
        _git(self.vault, "config", "user.name", "Test")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "initial")
        _write_contract(self.vault, enabled=True)
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            unittest.mock.patch.object(config, "COLLECTION", "gsync_rpc"),
            unittest.mock.patch.object(config, "VAULTS_CONFIG", ""),
        ]
        for p in self._patches:
            p.start()

        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.token = "gsync-token"
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

    def test_git_sync_status(self):
        status, body = self._post("/v1/git_sync", {"action": "status"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["sync_enabled"])


if __name__ == "__main__":
    unittest.main()
