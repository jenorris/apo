"""Table rows are indexed as their own chunks and surface in search."""

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


NOTE = """---
title: Pacifica
---

## Maintenance History

| Date       | Mileage | Service     |
| ---------- | ------- | ----------- |
| 2026-06-07 | 114587  | Brake flush |
| 2026-07-01 | 115200  | Oil change  |
"""


class TableIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "maint.md").write_text(NOTE, encoding="utf-8")
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "table_index_test"),
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

    def _rows(self):
        db = core.reader_connect()
        return db.execute(
            "SELECT chunk_kind, row_index, row_key, text, content_hash FROM chunks "
            "WHERE path='maint.md' ORDER BY ord"
        ).fetchall()

    def test_emits_section_header_and_row_chunks(self):
        kinds = [r[0] for r in self._rows()]
        self.assertIn("section", kinds)
        self.assertIn("table_header", kinds)
        self.assertEqual(kinds.count("table_row"), 2)

    def test_row_keys_and_flatten(self):
        rows = [r for r in self._rows() if r[0] == "table_row"]
        keys = {r[2] for r in rows}
        self.assertEqual(keys, {"2026-06-07", "2026-07-01"})
        flush = next(r for r in rows if r[2] == "2026-06-07")
        self.assertIn("Pacifica", flush[3])
        self.assertIn("Service: Brake flush", flush[3])

    def test_search_finds_specific_row(self):
        out = ops.search("Brake flush", limit=10)
        self.assertTrue(out["ok"], out)
        row_hits = [r for r in out["results"] if r.get("chunk_kind") == "table_row"]
        self.assertTrue(row_hits, out["results"])
        top = row_hits[0]
        self.assertEqual(top["row_key"], "2026-06-07")

    def test_search_hit_content_hash_matches_index(self):
        out = ops.search("Oil change", limit=10)
        row_hit = next(r for r in out["results"] if r.get("row_key") == "2026-07-01")
        db = core.reader_connect()
        idx_hash = db.execute(
            "SELECT content_hash FROM chunks WHERE chunk_hash=?",
            (row_hit["chunk_hash"],),
        ).fetchone()[0]
        # The bug: search used to hash the snippet, not the full chunk body.
        self.assertEqual(row_hit["content_hash"], idx_hash)


if __name__ == "__main__":
    unittest.main()
