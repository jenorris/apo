"""write_note sections[]/frontmatter (XOR content=) — CSV/JSON → indexed table rows."""

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


class WriteNoteSectionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "write_sections_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        core.writer_close()
        core.reader_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_csv_section_becomes_table(self):
        out = ops.write_note(
            "areas/vehicle/maintenance.md",
            frontmatter={"title": "Maintenance log", "tags": ["vehicle"]},
            sections=[
                {
                    "heading": "Brake Work",
                    "content_type": "csv",
                    "content": "date,mileage,service\n2026-06-07,114587,Brake flush\n",
                },
                {"heading": "Notes", "content_type": "markdown", "content": "Warranty until 2028."},
            ],
        )
        self.assertTrue(out["ok"], out)
        text = (self.vault / "areas/vehicle/maintenance.md").read_text()
        self.assertIn("title: Maintenance log", text)
        self.assertIn("## Brake Work", text)
        self.assertIn("| date | mileage | service |", text)
        self.assertIn("Warranty until 2028", text)

        core.index_files([self.vault / "areas/vehicle/maintenance.md"])
        hits = ops.search("Brake flush", limit=10)["results"]
        self.assertTrue(any(h.get("chunk_kind") == "table_row" for h in hits), hits)

    def test_json_section_becomes_table(self):
        out = ops.write_note(
            "log.md",
            sections=[
                {
                    "heading": "Data",
                    "content_type": "json",
                    "content": [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
                }
            ],
        )
        self.assertTrue(out["ok"], out)
        text = (self.vault / "log.md").read_text()
        self.assertIn("| a | b |", text)
        self.assertIn("| 3 | 4 |", text)

    def test_content_and_sections_mutually_exclusive(self):
        out = ops.write_note("x.md", content="hi", sections=[{"heading": "H", "content": "y"}])
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_plain_content_still_works(self):
        out = ops.write_note("plain.md", content="# Title\n\nBody.\n")
        self.assertTrue(out["ok"], out)
        self.assertEqual((self.vault / "plain.md").read_text(), "# Title\n\nBody.\n")


if __name__ == "__main__":
    unittest.main()
