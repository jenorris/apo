"""Vault-prefixed tool paths + write gate (process registry only)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import ops, vaults


def _mk_vault(root: Path, vault_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cdir = root / "system" / "contracts"
    cdir.mkdir(parents=True)
    (cdir / "usage-contract.schema.yaml").write_text(
        f"vault_id: {vault_id}\n", encoding="utf-8"
    )
    return root


class VaultPathPrefixOpsTests(unittest.TestCase):
    """Workbench-shaped registry (work + contracts) — atlas prefix denied."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = _mk_vault(self.tmp / "Work", "work")
        self.contracts = _mk_vault(self.tmp / "Contracts", "contracts")
        # Atlas exists on disk but is NOT in this process registry.
        self.atlas = _mk_vault(self.tmp / "Atlas", "atlas")
        (self.work / "areas").mkdir()
        (self.work / "areas" / "threads").mkdir()
        (self.work / "areas" / "threads" / "seed.md").write_text(
            "# Seed\n\nbody\n", encoding="utf-8"
        )
        paths = f"{self.work}:{self.contracts}"
        self._env = mock.patch.dict(
            os.environ,
            {
                "APO_VAULT_PATHS": paths,
                "APO_DEFAULT_VAULT": "work",
            },
            clear=False,
        )
        self._env.start()
        vaults._vault_id_cache.clear()
        _default, bindings = vaults.load_bindings()
        self.assertEqual(set(bindings), {"work", "contracts"}, bindings)
        self.assertEqual(_default, "work")

    def tearDown(self):
        self._env.stop()
        vaults._vault_id_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unknown_prefix_write_bad_vault(self):
        out = ops.write_note(
            "atlas:areas/threads/x.md",
            content="# Nope\n",
        )
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_vault")
        self.assertFalse((self.atlas / "areas" / "threads" / "x.md").exists())

    def test_workbench_rejects_atlas_prefix(self):
        out = ops.append_note(
            path="atlas:areas/threads/seed.md",
            text="\nmore\n",
        )
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_vault")

    def test_matching_prefix_write_ok(self):
        out = ops.write_note(
            "work:areas/threads/prefixed.md",
            content="# Prefixed\n\nhello\n",
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("vault"), "work")
        self.assertEqual(out.get("path"), "areas/threads/prefixed.md")
        self.assertEqual(out.get("qualified_path"), "work:areas/threads/prefixed.md")
        self.assertTrue((self.work / "areas" / "threads" / "prefixed.md").exists())

    def test_prefix_vault_arg_conflict(self):
        out = ops.write_note(
            "work:areas/threads/conflict.md",
            content="# x\n",
            vault="contracts",
        )
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_request")
        self.assertIn("conflicts", out.get("message", ""))

    def test_read_qualified_path(self):
        out = ops.read_note("work:areas/threads/seed.md")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("qualified_path"), "work:areas/threads/seed.md")
        self.assertEqual(out.get("vault"), "work")

    def test_mixed_vault_patch_batch_fails(self):
        (self.contracts / "note.md").write_text("# C\n", encoding="utf-8")
        out = ops.patch_notes(
            [
                {
                    "path": "work:areas/threads/seed.md",
                    "ops": [{"op": "replace_text", "find": "body", "replace": "BODY"}],
                },
                {
                    "path": "contracts:note.md",
                    "ops": [{"op": "replace_text", "find": "C", "replace": "CC"}],
                },
            ]
        )
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_request")
        self.assertIn("single vault", out.get("message", ""))

    def test_same_vault_prefixed_batch_ok(self):
        (self.work / "areas" / "threads" / "a.md").write_text("# A\n\nx\n", encoding="utf-8")
        (self.work / "areas" / "threads" / "b.md").write_text("# B\n\ny\n", encoding="utf-8")
        out = ops.patch_notes(
            [
                {
                    "path": "work:areas/threads/a.md",
                    "ops": [{"op": "replace_text", "find": "x", "replace": "X"}],
                },
                {
                    "path": "work:areas/threads/b.md",
                    "ops": [{"op": "replace_text", "find": "y", "replace": "Y"}],
                },
            ]
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("vault"), "work")
        self.assertIn("X", (self.work / "areas" / "threads" / "a.md").read_text())
        self.assertIn("Y", (self.work / "areas" / "threads" / "b.md").read_text())

    def test_delete_unknown_prefix_denied(self):
        out = ops.delete_note("atlas:areas/threads/seed.md")
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_vault")
        self.assertTrue((self.work / "areas" / "threads" / "seed.md").exists())
