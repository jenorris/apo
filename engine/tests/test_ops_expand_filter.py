"""expand_chunk / filter_notes ops polish."""

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


class ExpandChunkMtimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text(
            "---\ntitle: Alpha\n---\n\n# Alpha\n\nalpha widget body\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "expand_mtime_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_note_chunk_includes_mtime(self):
        hits = core.search("alpha", k=1, hybrid=False)
        self.assertTrue(hits)
        ch = hits[0].chunk_hash
        out = ops.read_note("", chunk_hash=ch)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["path"], "note.md")
        self.assertIn("mtime", out)
        expected = (self.vault / "note.md").stat().st_mtime
        self.assertAlmostEqual(out["mtime"], expected, places=5)

    def test_filter_notes_omitted_where(self):
        out = ops.filter_notes(None, folder="", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertGreaterEqual(out["total"], 1)

    def test_search_folders_fanout(self):
        (self.vault / "projects").mkdir(parents=True, exist_ok=True)
        (self.vault / "projects" / "p.md").write_text(
            "---\ntitle: Proj\n---\n\n# Proj\n\nproject alpha\n",
            encoding="utf-8",
        )
        core.index_vault(rebuild=True, verbose=False)
        out = ops.search(
            "alpha",
            folders=["projects", "prjects"],
            limit=5,
        )
        self.assertTrue(out["ok"], out)
        self.assertIn("folders", out)
        paths = {r["source"] for r in out["results"]}
        self.assertIn("projects/p.md", paths)
        self.assertIn("prjects", out.get("warning", ""))


class MissingFolderWarningTest(unittest.TestCase):
    """search/filter with a nonexistent folder= must warn, not lie with []."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / "projects").mkdir(parents=True)
        (self.vault / "projects" / "note.md").write_text(
            "---\ntitle: Alpha\nstatus: open\n---\n\n# Alpha\n\nalpha widget body\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "folder_warn_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        core.writer_close()
        core.reader_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_search_missing_folder_warns(self):
        out = ops.search("alpha", folder="prjects", limit=3)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["results"], [])
        self.assertIn("prjects", out.get("warning", ""))
        self.assertIn("projects", out.get("warning", ""))  # suggests real top-level dirs

    def test_search_existing_folder_no_warning(self):
        out = ops.search("alpha", folder="projects", limit=3)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["results"])
        self.assertNotIn("warning", out)

    def test_search_traversal_folder_rejected(self):
        out = ops.search("alpha", folder="../outside", limit=3)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_path")

    def test_filter_missing_folder_warns(self):
        out = ops.filter_notes({}, folder="prjects", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["total"], 0)
        self.assertIn("prjects", out.get("warning", ""))

    def test_filter_existing_folder_no_warning(self):
        out = ops.filter_notes({}, folder="projects", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertGreaterEqual(out["total"], 1)
        self.assertNotIn("warning", out)

    def test_embed_down_degrades_to_bm25_with_warning(self):
        """Dead embed backend (Ollama down) → keyword-only results + warning, not silent []."""
        with mock.patch.object(core, "query_embed", lambda q: None):
            out = ops.search("alpha widget", limit=3)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["results"], out)
        self.assertIn("keyword-only", out.get("warning", ""))

    def test_embed_down_no_hybrid_returns_empty_with_warning(self):
        with mock.patch.object(core, "query_embed", lambda q: None):
            out = ops.search("alpha widget", limit=3, hybrid=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["results"], [])
        self.assertIn("embedding failed", out.get("warning", ""))


if __name__ == "__main__":
    unittest.main()
