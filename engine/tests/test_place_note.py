"""place_note — move in-vault src; copy host .md otherwise."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, ops


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

    def test_rejects_non_md_host(self):
        other = self.outside / "data.txt"
        other.write_text("nope\n", encoding="utf-8")
        out = ops.place_note(str(other), "inbox/data.md")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_path")


if __name__ == "__main__":
    unittest.main()
