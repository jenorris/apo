"""patch_notes — same-vault multi-path patch batch."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from apo_engine import config, ops


class PatchNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-pn-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "a.md").write_text(
            "---\nstatus: active\n---\n\n# A\n\nbody a\n", encoding="utf-8"
        )
        (self.vault / "b.md").write_text(
            "---\nstatus: active\n---\n\n# B\n\nbody b\n", encoding="utf-8"
        )
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            unittest.mock.patch.object(config, "COLLECTION", "pn_test"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batch_ok(self):
        out = ops.patch_notes(
            [
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "done"}],
                },
                {
                    "path": "b.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "waiting"}],
                },
            ]
        )
        self.assertTrue(out["ok"], msg=out)
        self.assertFalse(out["partial"])
        self.assertEqual(out["applied_paths"], 2)
        self.assertEqual(out["failed_paths"], 0)
        self.assertIn("done", (self.vault / "a.md").read_text(encoding="utf-8"))
        self.assertIn("waiting", (self.vault / "b.md").read_text(encoding="utf-8"))

    def test_partial_continues(self):
        out = ops.patch_notes(
            [
                {
                    "path": "missing.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "x"}],
                },
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "done"}],
                },
            ]
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["partial"])
        self.assertEqual(out["applied_paths"], 1)
        self.assertEqual(out["failed_paths"], 1)
        self.assertEqual(out["error"], "batch_partial")
        self.assertIn("done", (self.vault / "a.md").read_text(encoding="utf-8"))

    def test_duplicate_path_rejected(self):
        out = ops.patch_notes(
            [
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "one"}],
                },
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "two"}],
                },
            ]
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_paths"], 1)
        self.assertEqual(out["results"][1]["error"], "duplicate_path")

    def test_xor_rejects_both(self):
        out = ops.patch_entry(
            path="a.md",
            ops=[{"op": "set_field", "field": "status", "value": "x"}],
            items=[
                {
                    "path": "b.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "y"}],
                }
            ],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_items_via_patch_entry(self):
        out = ops.patch_entry(
            items=[
                {
                    "path": "a.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "done"}],
                },
            ]
        )
        self.assertTrue(out["ok"], msg=out)
        self.assertEqual(out["applied_paths"], 1)


if __name__ == "__main__":
    unittest.main()
