"""Path-list / collection-root discovery + default resolution."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import vaults


def _write_usage(root: Path, vault_id: str, *, default_vault: str | None = None) -> None:
    contracts = root / "system" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    body = f"vault_id: {vault_id}\n"
    if default_vault:
        body += f"memory:\n  default_vault: {default_vault}\n"
    (contracts / "usage-contract.schema.yaml").write_text(body, encoding="utf-8")


class DiscoveryRegistryTests(unittest.TestCase):
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

    def test_collection_root_skips_wiki_sibling(self):
        notes = self.root / "Notes"
        work = notes / "Work"
        wiki = notes / "Wiki"
        work.mkdir(parents=True)
        wiki.mkdir()
        _write_usage(work, "work")
        (wiki / "readme.md").write_text("# wiki\n", encoding="utf-8")
        os.environ["APO_COLLECTION_ROOT"] = str(notes)
        os.environ["APO_DEFAULT_VAULT"] = "work"
        default, bindings = vaults.load_bindings()
        self.assertEqual(default, "work")
        self.assertEqual(set(bindings), {"work"})

    def test_duplicate_vault_id_fails(self):
        a = self.root / "a"
        b = self.root / "b"
        a.mkdir()
        b.mkdir()
        _write_usage(a, "dup")
        _write_usage(b, "dup")
        os.environ["APO_VAULT_PATHS"] = f"{a}:{b}"
        with self.assertRaises(ValueError) as ctx:
            vaults.load_bindings()
        self.assertIn("duplicate vault_id", str(ctx.exception))

    def test_ambiguous_default_fails(self):
        a = self.root / "a"
        b = self.root / "b"
        a.mkdir()
        b.mkdir()
        _write_usage(a, "alpha")
        _write_usage(b, "beta")
        os.environ["APO_VAULT_PATHS"] = f"{a}:{b}"
        with self.assertRaises(ValueError) as ctx:
            vaults.load_bindings()
        self.assertIn("ambiguous default vault", str(ctx.exception))

    def test_default_from_usage_memory_claim(self):
        a = self.root / "a"
        b = self.root / "b"
        a.mkdir()
        b.mkdir()
        _write_usage(a, "alpha", default_vault="alpha")
        _write_usage(b, "beta")
        os.environ["APO_VAULT_PATHS"] = f"{a}:{b}"
        default, bindings = vaults.load_bindings()
        self.assertEqual(default, "alpha")
        self.assertEqual(set(bindings), {"alpha", "beta"})

    def test_apo_vaults_shim_ignores_json_keys(self):
        meta = self.root / "Meta"
        meta.mkdir()
        _write_usage(meta, "jeremy")
        cfg = self.root / "vaults.json"
        cfg.write_text(
            json.dumps(
                {
                    "default": "jeremy",
                    "vaults": {
                        "meta": {"root": str(meta), "collection": "ignored"},
                    },
                }
            ),
            encoding="utf-8",
        )
        os.environ["APO_VAULTS"] = str(cfg)
        default, bindings = vaults.load_bindings()
        self.assertEqual(default, "jeremy")
        self.assertEqual(set(bindings), {"jeremy"})
        self.assertNotIn("meta", bindings)

    def test_disabled_marker_skips_vault(self):
        notes = self.root / "Notes"
        work = notes / "Work"
        work.mkdir(parents=True)
        _write_usage(work, "work")
        (work / "system" / "contracts" / ".apo-disabled").write_text("", encoding="utf-8")
        os.environ["APO_COLLECTION_ROOT"] = str(notes)
        with mock.patch("apo_engine.vaults.config") as cfg:
            cfg.NOTES_ROOT = self.root / "legacy"
            cfg.INDEX_PATH = self.root / "legacy.db"
            cfg.COLLECTION = "legacy"
            # No vaults discovered → fall through to legacy requires NOTES_ROOT
            (self.root / "legacy").mkdir(exist_ok=True)
            default, bindings = vaults.load_bindings()
        self.assertEqual(default, "default")
        self.assertEqual(set(bindings), {"default"})

    def test_apply_discovery_argv(self):
        a = self.root / "a"
        a.mkdir()
        _write_usage(a, "alpha")
        remaining = vaults.apply_discovery_argv(
            ["server.py", "--vault", str(a), "--default", "alpha", "--other"]
        )
        self.assertEqual(remaining, ["server.py", "--other"])
        self.assertIn(str(a), os.environ.get("APO_VAULT_PATHS", ""))
        self.assertEqual(os.environ.get("APO_DEFAULT_VAULT"), "alpha")


if __name__ == "__main__":
    unittest.main()
