"""offset/has_more paging for search_notes, history, backlinks."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops

_DIM = 32


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


class PaginationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        for i in range(8):
            (self.vault / f"note{i}.md").write_text(
                f"---\ntitle: Note {i}\n---\n\n## Topic\n\nshared keyword apple body {i}\n",
                encoding="utf-8",
            )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "pagination_test"),
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

    def test_search_offset_has_more(self):
        p1 = ops.search("apple", limit=3)
        self.assertTrue(p1["ok"], p1)
        self.assertTrue(p1["has_more"])
        self.assertEqual(len(p1["results"]), 3)
        p2 = ops.search("apple", limit=3, offset=3)
        self.assertEqual(p2["offset"], 3)
        seen = {r["source"] for r in p1["results"]}
        # Page 2 does not repeat page 1 hits.
        self.assertFalse(seen & {r["source"] for r in p2["results"]})

    def test_history_offset_has_more(self):
        h1 = ops.history(limit=3)
        self.assertTrue(h1["has_more"])
        self.assertEqual(len(h1["notes"]), 3)
        h2 = ops.history(limit=3, offset=3)
        self.assertEqual(h2["offset"], 3)


if __name__ == "__main__":
    unittest.main()
