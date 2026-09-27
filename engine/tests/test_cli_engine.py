"""apo-engine CLI (cli.py) — Problem A (ops-layer routing, $APO_VAULT) and
Problem B (watcher-aware index/reindex) regression coverage.

These are wiring-level tests: the downstream ``ops``/``core`` calls are mocked
so each test isolates one thing — the CLI parses ``--vault``/env correctly and
calls the right function with the right kwargs — rather than re-testing
``ops.search``/``ops.reindex`` themselves (covered elsewhere).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import cli, config, core, ops


class CliVaultEnvTest(unittest.TestCase):
    """apo-engine subcommands must honor $APO_VAULT like apo-local already does."""

    def setUp(self):
        self._env_patch = mock.patch.dict(
            "os.environ", {"APO_VAULT": "envvault"}, clear=False
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_search_default_vault_from_env(self):
        captured = {}

        def fake_search(query, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "results": [], "vault": kwargs.get("vault"), "has_more": False}

        with mock.patch.object(ops, "search", side_effect=fake_search):
            rc = cli.main(["search", "hello", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("vault"), "envvault")

    def test_stats_default_vault_from_env(self):
        captured = {}

        def fake_stats(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "vault": kwargs.get("vault")}

        with mock.patch.object(ops, "stats", side_effect=fake_stats):
            rc = cli.main(["stats"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("vault"), "envvault")

    def test_index_default_vault_from_env(self):
        captured = {}

        def fake_reindex(*, vault="", **kwargs):
            captured["vault"] = vault
            captured.update(kwargs)
            return {"ok": True, "mode": "rebuild", "vault": vault, "inline": False}

        with mock.patch.object(ops, "reindex", side_effect=fake_reindex):
            rc = cli.main(["index"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("vault"), "envvault")
        # --wait defaults on
        self.assertTrue(captured.get("wait"))

    def test_explicit_vault_flag_overrides_env(self):
        captured = {}

        def fake_search(query, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "results": [], "vault": kwargs.get("vault"), "has_more": False}

        with mock.patch.object(ops, "search", side_effect=fake_search):
            rc = cli.main(["search", "hello", "--json", "--vault", "explicit"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("vault"), "explicit")


class CliSearchNoHybridTest(unittest.TestCase):
    """apo-engine search already had --no-hybrid; confirm it still reaches ops.search
    (Problem A: cli.py used to call core.search directly, bypassing ops.search
    entirely — this pins the ops-layer routing so the MCP-shaped envelope holds)."""

    def test_no_hybrid_forwards_hybrid_false(self):
        captured = {}

        def fake_search(query, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "results": [], "vault": "default", "has_more": False}

        with mock.patch.object(ops, "search", side_effect=fake_search):
            rc = cli.main(["search", "hello", "--json", "--no-hybrid"])
        self.assertEqual(rc, 0)
        self.assertFalse(captured.get("hybrid"))

    def test_hybrid_default_true(self):
        captured = {}

        def fake_search(query, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "results": [], "vault": "default", "has_more": False}

        with mock.patch.object(ops, "search", side_effect=fake_search):
            rc = cli.main(["search", "hello", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(captured.get("hybrid"))


class CliIndexWatcherSafetyTest(unittest.TestCase):
    """--inline must refuse when a watcher is live; --force-inline overrides it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-cli-index-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# Note\n\nbody\n", encoding="utf-8")
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            mock.patch.object(config, "COLLECTION", "cli_index_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_inline_refused_when_watcher_running(self):
        with mock.patch.object(ops, "watcher_status", return_value={"running": True}):
            rc = cli.main(["index", "--inline"])
        self.assertEqual(rc, 1)

    def test_force_inline_overrides_refusal(self):
        fake_stats = core.IndexStats(added=1, changed=0, removed=0, chunks=1, seconds=0.01)
        with mock.patch.object(ops, "watcher_status", return_value={"running": True}), mock.patch.object(
            core, "index_vault", return_value=fake_stats
        ) as m:
            rc = cli.main(["index", "--force-inline"])
        self.assertEqual(rc, 0)
        m.assert_called_once()

    def test_inline_allowed_when_no_watcher(self):
        fake_stats = core.IndexStats(added=1, changed=0, removed=0, chunks=1, seconds=0.01)
        with mock.patch.object(ops, "watcher_status", return_value={"running": False}), mock.patch.object(
            core, "index_vault", return_value=fake_stats
        ) as m:
            rc = cli.main(["index", "--inline"])
        self.assertEqual(rc, 0)
        m.assert_called_once()

    def test_limit_forces_inline_and_is_refused_with_live_watcher(self):
        with mock.patch.object(ops, "watcher_status", return_value={"running": True}):
            rc = cli.main(["index", "--limit", "1"])
        self.assertEqual(rc, 1)

    def test_no_watcher_default_path_delegates_to_ops_reindex(self):
        captured = {}

        def fake_reindex(*, vault="", **kwargs):
            captured.update(kwargs)
            captured["vault"] = vault
            return {"ok": True, "mode": "rebuild", "vault": vault, "inline": True}

        with mock.patch.object(ops, "reindex", side_effect=fake_reindex):
            rc = cli.main(["index", "--rebuild"])
        self.assertEqual(rc, 0)
        self.assertTrue(captured.get("force"))
        self.assertEqual(captured.get("mode"), "rebuild")


if __name__ == "__main__":
    unittest.main()
