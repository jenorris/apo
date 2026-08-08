"""read_note mode=toc, nav prev/next, sibling hop, and format=json|row."""

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

## Overview

Prose overview.

## Maintenance History

| Date       | Mileage | Service     |
| ---------- | ------- | ----------- |
| 2026-06-07 | 114587  | Brake flush |
| 2026-07-01 | 115200  | Oil change  |

### Notes

Sub notes.

## Contacts

Dealer info.
"""


class TocReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "pacifica.md").write_text(NOTE, encoding="utf-8")
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "toc_read_test"),
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

    def test_toc_outline_no_body(self):
        out = ops.read_note("pacifica.md", mode="toc")
        self.assertTrue(out["ok"], out)
        titles = [e["title"] for e in out["toc"]]
        self.assertIn("Overview", titles)
        self.assertIn("Maintenance History", titles)
        self.assertIn("Contacts", titles)
        # ToC entries carry a chunk_hash for a follow-up section read.
        self.assertTrue(all(e["chunk_hash"] for e in out["toc"]))
        # No section body dumped.
        self.assertNotIn("content", out)

    def _hash_for(self, title: str) -> str:
        toc = ops.read_note("pacifica.md", mode="toc")["toc"]
        return next(e["chunk_hash"] for e in toc if e["title"] == title)

    def test_chunk_read_has_breadcrumb_and_nav(self):
        h = self._hash_for("Maintenance History")
        out = ops.read_note("", chunk_hash=h)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["breadcrumb"][-1], "Maintenance History")
        self.assertIn("nav", out)
        self.assertIn("position", out["nav"])

    def test_sibling_next_hops_same_depth(self):
        h = self._hash_for("Overview")
        out = ops.read_note("", chunk_hash=h, sibling="next")
        self.assertTrue(out["ok"], out)
        # Next level-2 section after Overview is Maintenance History.
        self.assertEqual(out["breadcrumb"][-1], "Maintenance History")

    def test_format_row_returns_columns(self):
        hits = ops.search("Oil change", limit=10)["results"]
        row = next(r for r in hits if r.get("row_key") == "2026-07-01")
        out = ops.read_note("", chunk_hash=row["chunk_hash"], format="row")
        self.assertEqual(out["format"], "row")
        self.assertEqual(out["columns"]["Service"], "Oil change")
        self.assertEqual(out["row_key"], "2026-07-01")

    def test_format_json_on_section_returns_table(self):
        h = self._hash_for("Maintenance History")
        out = ops.read_note("", chunk_hash=h, format="json")
        self.assertEqual(out["format"], "json")
        self.assertEqual(out["table"]["headers"], ["Date", "Mileage", "Service"])
        self.assertEqual(len(out["table"]["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
