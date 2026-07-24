"""send_note — promote host .md into the vault without model round-trip."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, ops


class SendNoteTest(unittest.TestCase):
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
            mock.patch.object(config, "COLLECTION", "send_test"),
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

    def test_copies_and_merges_fields(self):
        out = ops.send_note(
            str(self.src),
            "resources/wiki/report.md",
            fields={"source": "generator", "ingested_at": "2026-07-24"},
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["action"], "created")
        self.assertEqual(sorted(out["fields_applied"]), ["ingested_at", "source"])
        self.assertTrue(self.src.exists(), "src must remain (copy, not move)")
        dest = self.vault / "resources" / "wiki" / "report.md"
        self.assertTrue(dest.is_file())
        text = dest.read_text(encoding="utf-8")
        self.assertIn("source: generator", text)
        self.assertIn("ingested_at:", text)
        self.assertIn("2026-07-24", text)
        self.assertIn("body line", text)

    def test_rejects_vault_src(self):
        inside = self.vault / "inbox" / "x.md"
        inside.parent.mkdir(parents=True)
        inside.write_text("# x\n", encoding="utf-8")
        out = ops.send_note(str(inside), "resources/wiki/x.md")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "use_move_note")

    def test_rejects_relative_src(self):
        out = ops.send_note("outside/report.md", "inbox/x.md")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_path")

    def test_rejects_non_md(self):
        other = self.outside / "data.txt"
        other.write_text("nope\n", encoding="utf-8")
        out = ops.send_note(str(other), "inbox/data.md")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_path")

    def test_destination_exists_without_overwrite(self):
        dest = self.vault / "inbox" / "report.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("# old\n", encoding="utf-8")
        out = ops.send_note(str(self.src), "inbox/report.md")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "destination_exists")
        out2 = ops.send_note(str(self.src), "inbox/report.md", overwrite=True)
        self.assertTrue(out2["ok"], out2)
        self.assertEqual(out2["action"], "overwrote")


if __name__ == "__main__":
    unittest.main()
