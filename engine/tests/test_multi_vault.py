"""Multi-vault registry + per-index binding tests (no Ollama)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import core, vaults


class VaultRegistryTests(unittest.TestCase):
    def setUp(self):
        self._env = {}
        for key in (
            "APO_VAULTS",
            "APO_VAULT_PATHS",
            "APO_COLLECTION_ROOT",
            "APO_DEFAULT_VAULT",
            "APO_NOTES_ROOT",
            "APO_INDEX",
            "APO_COLLECTION",
        ):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # conftest's autouse _isolated_apo_runtime fixture already redirects
        # vaults._FALLBACK_ID_STORE into a per-test tmp dir; just clear the
        # in-process memo cache so each test re-derives from a clean slate.
        vaults._vault_id_cache.clear()
        vaults._APO_VAULTS_WARNED = False

    def tearDown(self):
        vaults._vault_id_cache.clear()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        # Clear any leftover binding
        while vaults.active() is not None:
            # shouldn't happen if tests use `with bind`
            break

    def test_legacy_single_vault(self):
        os.environ["APO_NOTES_ROOT"] = str(self.root / "a")
        os.environ["APO_INDEX"] = str(self.root / "a.db")
        os.environ["APO_COLLECTION"] = "coll_a"
        # config module already loaded — patch via vaults reading env paths in load
        with mock.patch("apo_engine.vaults.config") as cfg:
            cfg.NOTES_ROOT = Path(os.environ["APO_NOTES_ROOT"])
            cfg.INDEX_PATH = Path(os.environ["APO_INDEX"])
            cfg.COLLECTION = "coll_a"
            default, bindings = vaults.load_bindings()
        self.assertEqual(default, "default")
        self.assertEqual(set(bindings), {"default"})
        self.assertEqual(bindings["default"].collection, "coll_a")

    def test_multi_vault_json_file(self):
        a = self.root / "vault-a"
        b = self.root / "vault-b"
        a.mkdir()
        b.mkdir()
        for root, vid in ((a, "alpha"), (b, "beta")):
            cdir = root / "system" / "contracts"
            cdir.mkdir(parents=True)
            (cdir / "usage-contract.schema.yaml").write_text(
                f"vault_id: {vid}\n", encoding="utf-8"
            )
        cfg_path = self.root / "vaults.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "default": "alpha",
                    "vaults": {
                        # JSON keys ignored — names come from usage vault_id
                        "ignored-a": {"root": str(a), "index": str(self.root / "alpha.db")},
                        "ignored-b": {"root": str(b), "index": str(self.root / "beta.db")},
                    },
                }
            ),
            encoding="utf-8",
        )
        os.environ["APO_VAULTS"] = str(cfg_path)
        default, bindings = vaults.load_bindings()
        self.assertEqual(default, "alpha")
        self.assertEqual(set(bindings), {"alpha", "beta"})
        # collection is no longer configurable — derived, non-empty, distinct per root
        self.assertTrue(bindings["alpha"].collection)
        self.assertTrue(bindings["beta"].collection)
        self.assertNotEqual(bindings["alpha"].collection, bindings["beta"].collection)

    def test_collection_derived_from_git_root(self):
        repo = self.root / "git-vault"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "note.md").write_text("# hi\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "root"], cwd=repo, check=True)
        root_hash = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        vault_id = vaults.compute_vault_id(repo)
        self.assertEqual(vault_id, root_hash[:12])
        # stable across repeated calls (memoized, and re-derivable from git either way)
        self.assertEqual(vaults.compute_vault_id(repo), vault_id)
        self.assertFalse(vaults._FALLBACK_ID_STORE.exists())

    def test_collection_fallback_random_and_cached(self):
        plain = self.root / "not-a-git-repo"
        plain.mkdir()
        first = vaults.compute_vault_id(plain)
        vaults._vault_id_cache.clear()  # force re-derivation, not just in-process memoization
        second = vaults.compute_vault_id(plain)
        self.assertEqual(first, second)
        store = json.loads(vaults._FALLBACK_ID_STORE.read_text(encoding="utf-8"))
        self.assertEqual(store[str(plain.resolve())], first)

    def test_bind_switches_index_key(self):
        a = self.root / "a"
        b = self.root / "b"
        a.mkdir()
        b.mkdir()
        ba = vaults.VaultBinding("a", a, self.root / "a.db", "a").resolved()
        bb = vaults.VaultBinding("b", b, self.root / "b.db", "b").resolved()
        with vaults.bind(ba):
            self.assertEqual(vaults.index_path(), ba.index)
            self.assertEqual(core._index_key(), str(ba.index))
            with vaults.bind(bb):
                self.assertEqual(vaults.index_path(), bb.index)
                self.assertEqual(core._index_key(), str(bb.index))
            self.assertEqual(vaults.index_path(), ba.index)

    def test_separate_indexes_dont_share_chunks(self):
        """Two vaults write distinct sqlite files under bind."""
        a = self.root / "va"
        b = self.root / "vb"
        a.mkdir()
        b.mkdir()
        (a / "note-a.md").write_text("---\ntitle: A\n---\n\n# Alpha uniquezzz\n\nbody a\n", encoding="utf-8")
        (b / "note-b.md").write_text("---\ntitle: B\n---\n\n# Beta uniquezzz\n\nbody b\n", encoding="utf-8")

        ba = vaults.VaultBinding("a", a, self.root / "ia.db", "ca").resolved()
        bb = vaults.VaultBinding("b", b, self.root / "ib.db", "cb").resolved()

        # Fake embedder so we don't need Ollama
        def fake_embed(texts, **kwargs):
            import hashlib
            import re

            out = []
            for t in texts:
                v = [0.0] * 16
                for tok in re.findall(r"\w+", t.lower()):
                    slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % 16
                    v[slot] += 1.0
                norm = sum(x * x for x in v) ** 0.5 or 1.0
                out.append([x / norm for x in v])
            return out

        with mock.patch("apo_engine.core.embed", side_effect=fake_embed):
            with vaults.bind(ba):
                core.index_vault(rebuild=True)
                hits_a = core.search("Alpha uniquezzz", k=3)
            with vaults.bind(bb):
                core.index_vault(rebuild=True)
                hits_b = core.search("Beta uniquezzz", k=3)
                hits_cross = core.search("Alpha uniquezzz", k=3)

        self.assertTrue(any("note-a" in h.path for h in hits_a), hits_a)
        self.assertTrue(any("note-b" in h.path for h in hits_b), hits_b)
        # beta index should not contain alpha's unique token as a strong hit from vault a file
        self.assertFalse(any("note-a" in h.path for h in hits_cross), hits_cross)
        self.assertTrue(ba.index.is_file())
        self.assertTrue(bb.index.is_file())
        self.assertNotEqual(ba.index, bb.index)


if __name__ == "__main__":
    unittest.main()
