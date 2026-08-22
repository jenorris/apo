"""place_note — move in-vault src; copy host .md otherwise."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, ops, vaults


class PlaceNoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.src = self.outside / "report.md"
        self.src.write_text(
            "---\ntitle: Report\n---\n\n# Report\n\nbody line\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "place_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(config, "SEND_ALLOW_ROOTS", str(self.tmp.resolve())),
            mock.patch.object(config, "SEND_MAX_BYTES", 5 * 1024 * 1024),
            mock.patch.object(config, "OKF_CONTRACT", ""),
            mock.patch.object(config, "OKF_ENFORCEMENT", "off"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copies_host_and_merges_fields(self):
        out = ops.place_note(
            str(self.src),
            "resources/wiki/report.md",
            fields={"source": "generator", "ingested_at": "2026-07-24"},
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "copy")
        self.assertEqual(out["action"], "created")
        self.assertTrue(self.src.exists(), "src must remain (copy, not move)")
        dest = self.vault / "resources" / "wiki" / "report.md"
        self.assertTrue(dest.is_file())
        text = dest.read_text(encoding="utf-8")
        self.assertIn("source: generator", text)
        self.assertIn("body line", text)

    def test_moves_vault_relative(self):
        src = self.vault / "inbox" / "a.md"
        src.parent.mkdir(parents=True)
        src.write_text("# A\n", encoding="utf-8")
        out = ops.place_note("inbox/a.md", "archives/a.md")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "move")
        self.assertEqual(out["action"], "moved")
        self.assertFalse(src.exists())
        self.assertTrue((self.vault / "archives" / "a.md").is_file())

    def test_absolute_in_vault_moves(self):
        src = self.vault / "inbox" / "b.md"
        src.parent.mkdir(parents=True)
        src.write_text("# B\n", encoding="utf-8")
        out = ops.place_note(str(src.resolve()), "archives/b.md")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "move")
        self.assertFalse(src.exists())
        self.assertTrue((self.vault / "archives" / "b.md").is_file())

    def test_fields_forbidden_on_move(self):
        src = self.vault / "inbox" / "c.md"
        src.parent.mkdir(parents=True)
        src.write_text("# C\n", encoding="utf-8")
        out = ops.place_note("inbox/c.md", "archives/c.md", fields={"x": 1})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_rejects_relative_host_src(self):
        out = ops.place_note("outside/report.md", "inbox/x.md")
        self.assertFalse(out["ok"])
        # relative non-vault path is treated as vault-relative → not_found
        self.assertEqual(out["error"], "not_found")

    def test_patch_note_place_op_moves(self):
        src = self.vault / "inbox" / "d.md"
        src.parent.mkdir(parents=True)
        src.write_text("# D\n", encoding="utf-8")
        out = ops.patch_entry(
            ops=[{"op": "place", "src": "inbox/d.md", "dst": "archives/d.md"}],
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "move")
        self.assertFalse(src.exists())
        self.assertTrue((self.vault / "archives" / "d.md").is_file())

        other = self.outside / "data.txt"
        other.write_text("nope\n", encoding="utf-8")
        out = ops.place_note(str(other), "inbox/data.md")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_path")


class PlaceNoteCrossVaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-place-xvault-"))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        for root, vid in ((self.a, "alpha"), (self.b, "beta")):
            (root / "system" / "contracts").mkdir(parents=True)
            (root / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
                f"vault_id: {vid}\n", encoding="utf-8"
            )
        (self.a / "areas").mkdir()
        self.src_note = self.a / "areas" / "template.md"
        self.src_note.write_text(
            "---\ntitle: Template\n---\n\n# Template\n\nbody\n", encoding="utf-8"
        )
        registry = {
            "default": "alpha",
            "vaults": {
                "alpha": {"root": str(self.a), "index": str(self.tmp / "a.db")},
                "beta": {"root": str(self.b), "index": str(self.tmp / "b.db")},
            },
        }
        self.reg_path = self.tmp / "vaults.json"
        self.reg_path.write_text(json.dumps(registry), encoding="utf-8")
        self._env = mock.patch.dict(
            os.environ,
            {
                "APO_VAULTS": str(self.reg_path),
                "APO_NOTES_ROOT": str(self.a),
                "APO_INDEX": str(self.tmp / "legacy.db"),
                "APO_COLLECTION": "legacy",
            },
            clear=False,
        )
        self._env.start()
        vaults._vault_id_cache.clear()

    def tearDown(self):
        self._env.stop()
        vaults._vault_id_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_cross_vault_by_default(self):
        # Both src and dst carry explicit vault_id: prefixes — vault= is left empty
        # so neither resolution conflicts; the cross-vault check itself must reject.
        out = ops.place_note("alpha:areas/template.md", "beta:areas/template.md")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")
        self.assertIn("allow_cross_vault", out["message"])
        self.assertTrue(self.src_note.exists())
        self.assertFalse((self.b / "areas" / "template.md").exists())

    def test_allow_cross_vault_copies_and_leaves_src(self):
        out = ops.place_note(
            "alpha:areas/template.md",
            "beta:areas/template.md",
            allow_cross_vault=True,
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "copy")
        self.assertTrue(out["cross_vault"])
        self.assertEqual(out["action"], "created")
        self.assertTrue(self.src_note.exists(), "cross-vault place must copy, never move")
        dest = self.b / "areas" / "template.md"
        self.assertTrue(dest.is_file())
        self.assertIn("body", dest.read_text(encoding="utf-8"))

    def test_allow_cross_vault_respects_overwrite_guard(self):
        dest = self.b / "areas" / "template.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("already here\n", encoding="utf-8")
        out = ops.place_note(
            "alpha:areas/template.md",
            "beta:areas/template.md",
            allow_cross_vault=True,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "destination_exists")

        out2 = ops.place_note(
            "alpha:areas/template.md",
            "beta:areas/template.md",
            allow_cross_vault=True,
            overwrite=True,
        )
        self.assertTrue(out2["ok"], out2)
        self.assertEqual(out2["action"], "overwrote")

    def test_allow_cross_vault_merges_fields(self):
        out = ops.place_note(
            "alpha:areas/template.md",
            "beta:areas/template.md",
            allow_cross_vault=True,
            fields={"cloned_from": "alpha"},
        )
        self.assertTrue(out["ok"], out)
        text = (self.b / "areas" / "template.md").read_text(encoding="utf-8")
        self.assertIn("cloned_from: alpha", text)

    def test_patch_note_place_op_allow_cross_vault(self):
        out = ops.patch_entry(
            ops=[
                {
                    "op": "place",
                    "src": "alpha:areas/template.md",
                    "dst": "beta:areas/template.md",
                    "allow_cross_vault": True,
                }
            ],
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "copy")
        self.assertTrue(self.src_note.exists())
        self.assertTrue((self.b / "areas" / "template.md").is_file())


if __name__ == "__main__":
    unittest.main()
