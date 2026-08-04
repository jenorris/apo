"""Soft agent habit tips on search/write responses."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops

_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


class HabitTipsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        areas = self.vault / "areas"
        areas.mkdir()
        (areas / "note.md").write_text(
            "---\ntitle: Widget\n---\n\n# Widget\n\nwidget body here\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "habit_tips_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
            mock.patch.object(ops, "watcher_status", lambda: {"running": True}),
        ]
        for p in self._patches:
            p.start()
        ops._recent_touches.clear()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        ops._recent_touches.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_search_tips_when_folder_omitted(self):
        out = ops.search("widget", limit=3, folder="")
        self.assertTrue(out["ok"], out)
        tip = out.get("tip", "")
        self.assertIn("folder=", tip)
        self.assertIn("areas", tip)

    def test_search_no_tip_when_folder_set(self):
        out = ops.search("widget", limit=3, folder="areas")
        self.assertTrue(out["ok"], out)
        self.assertNotIn("tip", out)

    def test_search_hits_include_mtime(self):
        out = ops.search("widget", limit=3, folder="areas")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["results"])
        hit = out["results"][0]
        self.assertIn("mtime", hit)
        self.assertIsInstance(hit["mtime"], float)
        self.assertIn("modified", hit)

    def test_second_write_without_mtime_tips(self):
        path = "areas/note.md"
        first = ops.append_note(path, "- one\n")
        self.assertTrue(first["ok"], first)
        self.assertNotIn("expected_mtime", first.get("tip", ""))

        second = ops.append_note(path, "- two\n")
        self.assertTrue(second["ok"], second)
        tip = second.get("tip", "")
        self.assertIn("expected_mtime", tip)
        self.assertIn(str(first["mtime"]), tip)

    def test_second_write_with_mtime_skips_tip(self):
        path = "areas/note.md"
        first = ops.append_note(path, "- a\n")
        self.assertTrue(first["ok"], first)
        second = ops.append_note(
            path,
            "- b\n",
            expected_mtime=first["mtime"],
        )
        self.assertTrue(second["ok"], second)
        self.assertNotIn("expected_mtime", second.get("tip", ""))

    def test_read_then_write_without_mtime_tips(self):
        path = "areas/note.md"
        read = ops.read_note(path)
        self.assertTrue(read["ok"], read)
        write = ops.append_note(path, "- after read\n")
        self.assertTrue(write["ok"], write)
        tip = write.get("tip", "")
        self.assertIn("expected_mtime", tip)
        self.assertIn(str(read["mtime"]), tip)


if __name__ == "__main__":
    unittest.main()
