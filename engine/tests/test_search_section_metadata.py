"""Search hit metadata and expand_section preview policy."""

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
_UNIQUE = "zzzzzzzzzzzzzzzzzzzz"


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


class SearchSectionMetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        big = _UNIQUE + ("y" * 9000)
        (self.vault / "big.md").write_text(
            "---\ntitle: Big\n---\n\n# Big\n\n" + big + "\n",
            encoding="utf-8",
        )
        (self.vault / "dup.md").write_text(
            "---\ntitle: Dup\n---\n\n## Status\n\none\n\n## Other\n\nmid\n\n## Status\n\ntwo\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "section_meta_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for patch in self._patches:
            patch.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        core.writer_close()
        core.reader_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_search_hit_includes_bytes_no_lines(self):
        out = ops.search("one", limit=5)
        self.assertTrue(out["ok"], out)
        hit = next(r for r in out["results"] if r["source"] == "dup.md")
        self.assertGreater(hit["file_bytes"], 0)
        self.assertGreater(hit["section_bytes"], 0)
        self.assertNotIn("start_line", hit)
        self.assertNotIn("end_line", hit)

    def _big_hash(self) -> str:
        db = core.reader_connect()
        row = db.execute(
            "SELECT chunk_hash FROM chunks WHERE path=? ORDER BY ord LIMIT 1",
            ("big.md",),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row[0])

    def test_large_file_tip(self):
        out = ops.search("Big", limit=3, hybrid=False)
        self.assertTrue(out["ok"], out)
        tip = out.get("tip") or ""
        self.assertIn("expand_section", tip)

    def test_expand_section_preview(self):
        out = ops.expand_section(self._big_hash())
        self.assertTrue(out["ok"], out)
        self.assertTrue(out.get("preview_truncated"))
        self.assertLess(len(out["content"]), 9000)

    def test_expand_section_force_full(self):
        out = ops.expand_section(self._big_hash(), force=True)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("preview_truncated", out)
        self.assertGreater(len(out["content"]), 8000)

    def test_duplicate_headings_distinct_hashes(self):
        out = ops.search("one", limit=5)
        one_hit = next(r for r in out["results"] if r["source"] == "dup.md")
        out2 = ops.search("two", limit=5)
        two_hit = next(r for r in out2["results"] if r["source"] == "dup.md")
        self.assertNotEqual(one_hit["chunk_hash"], two_hit["chunk_hash"])
        self.assertIn("one", ops.expand_section(one_hit["chunk_hash"], force=True)["content"])
        self.assertIn("two", ops.expand_section(two_hit["chunk_hash"], force=True)["content"])


if __name__ == "__main__":
    unittest.main()
