"""Row-keyed table patch ops: strict preconditions, watcher-only re-embed."""

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


class TablePatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "maint.md").write_text(NOTE, encoding="utf-8")
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "table_patch_test"),
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

    def _row_hash(self, row_key: str) -> str:
        hit = next(
            r for r in ops.search("Oil Brake flush change", limit=20)["results"]
            if r.get("row_key") == row_key
        )
        out = ops.read_note("", chunk_hash=hit["chunk_hash"], format="row")
        return out["row_hash"]

    def _content_hash(self, row_key: str) -> str:
        return next(
            r["content_hash"]
            for r in ops.search("Oil Brake flush change", limit=20)["results"]
            if r.get("row_key") == row_key
        )

    def test_update_cell_requires_precondition(self):
        out = ops.patch_note(
            "maint.md",
            [{"op": "update_cell", "row_key": "2026-06-07", "column": "Mileage", "value": "999"}],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "precondition_required")

    def test_update_cell_stale_rejected(self):
        out = ops.patch_note(
            "maint.md",
            [{
                "op": "update_cell", "row_key": "2026-06-07", "column": "Mileage",
                "value": "999", "expected_row_hash": "deadbeefdeadbeef",
            }],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "stale_write")

    def test_update_cell_row_hash_precondition(self):
        rh = self._row_hash("2026-06-07")
        out = ops.patch_note(
            "maint.md",
            [{
                "op": "update_cell", "row_key": "2026-06-07", "column": "Mileage",
                "value": "999", "expected_row_hash": rh,
            }],
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reembed"], "pending")
        self.assertIn("999", (self.vault / "maint.md").read_text())

    def test_update_cell_content_hash_precondition(self):
        ch = self._content_hash("2026-07-01")
        out = ops.patch_note(
            "maint.md",
            [{
                "op": "update_cell", "row_key": "2026-07-01", "column": "Service",
                "value": "Oil + filter", "expected_content_hash": ch,
            }],
        )
        self.assertTrue(out["ok"], out)
        self.assertIn("Oil + filter", (self.vault / "maint.md").read_text())

    def test_delete_row(self):
        rh = self._row_hash("2026-07-01")
        out = ops.patch_note(
            "maint.md",
            [{"op": "delete_row", "row_key": "2026-07-01", "expected_row_hash": rh}],
        )
        self.assertTrue(out["ok"], out)
        self.assertNotIn("Oil change", (self.vault / "maint.md").read_text())

    def test_append_row_no_precondition(self):
        out = ops.patch_note(
            "maint.md",
            [{"op": "append_row", "row": {"Date": "2026-08-01", "Mileage": "116000", "Service": "Tires"}}],
        )
        self.assertTrue(out["ok"], out)
        self.assertIn("Tires", (self.vault / "maint.md").read_text())

    def test_append_row_unknown_column_rejected(self):
        out = ops.patch_note(
            "maint.md",
            [{"op": "append_row", "row": {"Date": "2026-08-01", "Bogus": "x"}}],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unknown_column")

    def test_replace_table_upsert_fuzzy(self):
        out = ops.patch_note(
            "maint.md",
            [{
                "op": "replace_table",
                "merge": "upsert",
                "rows": [{"date": "2026-06-07", "mileage": "114600", "servce": "Brake bleed"}],
            }],
        )
        self.assertTrue(out["ok"], out)
        text = (self.vault / "maint.md").read_text()
        self.assertIn("Brake bleed", text)
        self.assertIn("114600", text)
        # upsert on existing key does not add a new row
        self.assertEqual(text.count("2026-06-07"), 1)

    def test_replace_table_ambiguous_header_rejected(self):
        out = ops.patch_note(
            "maint.md",
            [{"op": "replace_table", "merge": "append", "rows": [{"zzz": "1"}]}],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "header_ambiguous")

    def test_alter_schema_requires_confirm(self):
        out = ops.patch_note(
            "maint.md",
            [{"op": "alter_table_schema", "add_column": "Cost"}],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "confirm_required")

    def test_alter_schema_add_column(self):
        out = ops.patch_note(
            "maint.md",
            [{"op": "alter_table_schema", "add_column": "Cost", "confirm": True}],
        )
        self.assertTrue(out["ok"], out)
        self.assertIn("Cost", (self.vault / "maint.md").read_text())

    def test_table_ops_cannot_mix_with_prose(self):
        out = ops.patch_note(
            "maint.md",
            [
                {"op": "append_row", "row": {"Date": "x"}},
                {"op": "append_eof", "text": "trailer"},
            ],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")


class ReembedReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "maint.md").write_text(NOTE, encoding="utf-8")
        self.index = self.tmp / "index.db"
        self.calls: list[list[str]] = []

        def _counting_embed(texts, **kwargs):
            self.calls.append(list(texts))
            return _fake_embed(texts, **kwargs)

        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "reembed_reuse_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _counting_embed),
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

    def test_single_cell_edit_reembeds_one_row(self):
        # Rewrite one cell on disk, then let the watcher entry point re-embed.
        text = (self.vault / "maint.md").read_text().replace("114587", "999999")
        (self.vault / "maint.md").write_text(text, encoding="utf-8")
        self.calls.clear()
        core.reembed_one("maint.md")
        embedded = [t for batch in self.calls for t in batch]
        # The changed row re-embeds; the unchanged row and the header chunk are
        # reused by content_hash (row-level O(changed), not O(table)). The owning
        # prose section necessarily re-embeds because it carries the raw table.
        row_texts = [t for t in embedded if t.startswith("Pacifica >")]
        self.assertEqual(len(row_texts), 1, embedded)
        self.assertIn("999999", row_texts[0])
        self.assertFalse(
            any("Oil change" in t for t in row_texts),
            "unchanged row should be reused, not re-embedded",
        )
        self.assertFalse(
            any(t.startswith("Pacifica > Maintenance History — Columns:") for t in embedded),
            "table header chunk should be reused, not re-embedded",
        )


if __name__ == "__main__":
    unittest.main()
