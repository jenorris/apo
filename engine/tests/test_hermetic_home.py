"""The suite must never read or write the developer's real ``~/.apo`` (or engine/index.db).

conftest.py redirects HOME for the whole session (before apo_engine is imported) and
monkeypatches the per-test runtime paths. These tests pin that contract, including the
case that originally leaked: a test that re-imports apo_engine from scratch, which
re-evaluates every import-time ``Path.home() / ".apo"`` constant.

The real ``~/.apo/vault-ids.json`` is shared with the live watcher and with other test
runs on the same machine, so mtime comparison would be flaky; assert on this test's own
unique vault root instead.
"""

from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path

from apo_engine import config, deferred, metrics_backend, ops, tool_metrics, vaults

import conftest  # tests/conftest.py (rootdir/tests is on sys.path)

_REAL_APO = conftest.REAL_HOME / ".apo"


def _under(path: Path, parent: Path) -> bool:
    return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(parent)))


class HermeticHomeTest(unittest.TestCase):
    def test_home_is_not_the_real_home(self):
        self.assertNotEqual(Path.home().resolve(), conftest.REAL_HOME.resolve())

    def test_runtime_paths_are_outside_real_apo_dir(self):
        for label, path in {
            "deferred.DEFERRED_DIR": deferred.DEFERRED_DIR,
            "tool_metrics.DEFERRED_DIR": tool_metrics.DEFERRED_DIR,
            "ops.WATCH_PID_FILE": ops.WATCH_PID_FILE,
            "vaults._FALLBACK_ID_STORE": vaults._FALLBACK_ID_STORE,
            "metrics_backend.DEFAULT_EMBEDDED_PATH": metrics_backend.DEFAULT_EMBEDDED_PATH,
            "config.INDEX_PATH": config.INDEX_PATH,
            "config.NOTES_ROOT": config.NOTES_ROOT,
            "WATCH_PID_DIR": Path(os.environ["WATCH_PID_DIR"]),
        }.items():
            with self.subTest(label):
                self.assertFalse(_under(path, _REAL_APO), f"{label}={path} is under {_REAL_APO}")

    def test_default_index_is_not_in_source_tree(self):
        engine_root = Path(__file__).resolve().parents[1]
        self.assertFalse(_under(Path(config.INDEX_PATH), engine_root))
        self.assertFalse(_under(Path(os.environ["APO_INDEX"]), engine_root))

    def test_fresh_import_of_vaults_does_not_touch_real_apo_dir(self):
        """Mirrors test_mcp_server_vault_registry: a from-scratch import re-reads Path.home()."""
        path = Path(vaults.__file__)
        spec = importlib.util.spec_from_file_location("apo_engine.vaults", path)
        assert spec is not None and spec.loader is not None
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)  # not registered in sys.modules
        self.assertFalse(_under(fresh._FALLBACK_ID_STORE, _REAL_APO), fresh._FALLBACK_ID_STORE)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"  # not a git repo -> hits the fallback id store
            root.mkdir()
            # Temp roots are not persisted; force the store write this test guards.
            fresh.is_ephemeral_root = lambda _root: False
            fresh.compute_collection_id(root)
            self.assertTrue(fresh._FALLBACK_ID_STORE.is_file())
            real_store = _REAL_APO / "vault-ids.json"
            if real_store.is_file():
                try:
                    real = json.loads(real_store.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    real = {}
                self.assertNotIn(str(root.resolve()), real)


if __name__ == "__main__":
    unittest.main()
