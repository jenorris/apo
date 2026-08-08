"""Regression tests for Bugbot findings fixed in 0.6.1."""

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


MULTI = """---
title: Multi
---

## Logs

| Date | A |
| ---- | - |
| 2026-01-01 | alpha |

| SKU | Qty |
| --- | --- |
| WIDGET-1 | 3 |
| WIDGET-2 | 5 |
"""

CONTRACT = """
table_contract_version: "0.1"
tables:
  - match: "inventory.md"
    key_column: "SKU"
"""

INVENTORY = """---
title: Inventory
---

## Stock

| Name | SKU | Qty |
| ---- | --- | --- |
| Widget | WIDGET-1 | 3 |
| Gadget | WIDGET-2 | 5 |
"""


class BugbotFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "system" / "contracts").mkdir(parents=True)
        (self.vault / "system" / "contracts" / "table-contract.schema.yaml").write_text(
            CONTRACT, encoding="utf-8"
        )
        (self.vault / "multi.md").write_text(MULTI, encoding="utf-8")
        (self.vault / "inventory.md").write_text(INVENTORY, encoding="utf-8")
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "bugbot_061"),
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

    def test_row_read_content_hash_matches_search(self):
        hit = next(
            r for r in ops.search("WIDGET-1", limit=10)["results"]
            if r.get("row_key") == "WIDGET-1"
        )
        out = ops.read_note("", chunk_hash=hit["chunk_hash"])
        self.assertEqual(out["content_hash"], hit["content_hash"])
        # format=row still usable as expected_content_hash precondition
        row = ops.read_note("", chunk_hash=hit["chunk_hash"], format="row")
        patch = ops.patch_note(
            "inventory.md",
            [{
                "op": "update_cell",
                "row_key": "WIDGET-1",
                "column": "Qty",
                "value": "9",
                "expected_content_hash": out["content_hash"],
                "table_id": hit["table_id"],
            }],
        )
        self.assertTrue(patch["ok"], patch)
        self.assertIn("9", (self.vault / "inventory.md").read_text())

    def test_search_hit_includes_table_id(self):
        hit = next(
            r for r in ops.search("WIDGET-2", limit=10)["results"]
            if r.get("row_key") == "WIDGET-2"
        )
        self.assertTrue(hit.get("table_id"), hit)

    def test_contract_key_column(self):
        rows = [
            r for r in ops.search("WIDGET", limit=20)["results"]
            if r.get("chunk_kind") == "table_row" and r.get("source") == "inventory.md"
        ]
        keys = {r["row_key"] for r in rows}
        self.assertEqual(keys, {"WIDGET-1", "WIDGET-2"})

    def test_heading_ambiguous_when_multiple_tables(self):
        out = ops.patch_note(
            "multi.md",
            [{"op": "append_row", "heading": "Logs", "row": {"Date": "x", "A": "y"}}],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "table_ambiguous")

    def test_fresh_hashes_with_table_id(self):
        hit = next(
            r for r in ops.search("alpha", limit=10)["results"]
            if r.get("row_key") == "2026-01-01"
        )
        out = ops.patch_note(
            "multi.md",
            [{
                "op": "update_cell",
                "table_id": hit["table_id"],
                "row_key": "2026-01-01",
                "column": "A",
                "value": "beta",
                "expected_content_hash": hit["content_hash"],
            }],
        )
        self.assertTrue(out["ok"], out)
        result = out["results"][0]
        self.assertEqual(result.get("table_id"), hit["table_id"])
        self.assertTrue(result.get("content_hash"), result)
        self.assertTrue(result.get("row_hash"), result)
        self.assertNotEqual(result["content_hash"], hit["content_hash"])


if __name__ == "__main__":
    unittest.main()
