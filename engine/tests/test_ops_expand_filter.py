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

    def test_expand_section_includes_mtime(self):
        hits = core.search("alpha", k=1, hybrid=False)
        self.assertTrue(hits)
        ch = hits[0].chunk_hash
        out = ops.expand_chunk(ch, scope="section")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["path"], "note.md")
        self.assertIn("mtime", out)
        expected = (self.vault / "note.md").stat().st_mtime
        self.assertAlmostEqual(out["mtime"], expected, places=5)

    def test_filter_notes_omitted_where(self):
        out = ops.filter_notes(None, folder="", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertGreaterEqual(out["total"], 1)


if __name__ == "__main__":
    unittest.main()
