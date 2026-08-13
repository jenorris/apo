"""write_note dual-write — frontmatter= + content=/text= (body= rejected)."""

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


class WriteNoteDualWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "write_dual_test"),
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

    def test_frontmatter_and_content_dual_write(self):
        out = ops.write_note(
            "areas/notes/dual.md",
            content="# Dual\n\nBody text here.",
            frontmatter={"title": "Dual write", "tags": ["apo"]},
        )
        self.assertTrue(out["ok"], out)
        text = (self.vault / "areas/notes/dual.md").read_text()
        self.assertIn("title: Dual write", text)
        self.assertIn("tags:", text)
        self.assertIn("# Dual", text)
        self.assertIn("Body text here.", text)

    def test_frontmatter_and_text_alias(self):
        out = ops.write_note(
            "dual_text.md",
            text="Just the body.",
            frontmatter={"title": "Via text"},
        )
        self.assertTrue(out["ok"], out)
        text = (self.vault / "dual_text.md").read_text()
        self.assertIn("title: Via text", text)
        self.assertIn("Just the body.", text)

    def test_frontmatter_only_no_content(self):
        out = ops.write_note("fm_only.md", frontmatter={"title": "Only FM"})
        self.assertTrue(out["ok"], out)
        text = (self.vault / "fm_only.md").read_text()
        self.assertIn("title: Only FM", text)

    def test_body_with_frontmatter_rejected(self):
        out = ops.write_note(
            "bad.md",
            body="legacy body",
            frontmatter={"title": "x"},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_content_with_sections_rejected(self):
        out = ops.write_note(
            "bad2.md",
            content="hi",
            sections=[{"heading": "H", "content": "y"}],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_conflicting_content_and_text_rejected(self):
        out = ops.write_note(
            "bad3.md",
            content="one",
            text="two",
            frontmatter={"title": "x"},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_dual_write_indexes_and_filter_finds(self):
        out = ops.write_note(
            "areas/notes/searchable.md",
            content="Unique token zorbafraz.",
            frontmatter={"title": "Searchable", "tags": ["findme"]},
        )
        self.assertTrue(out["ok"], out)
        core.index_files([self.vault / "areas/notes/searchable.md"])
        hits = ops.filter_notes({"tags": {"$contains": "findme"}}, limit=10)["notes"]
        self.assertTrue(any("searchable.md" in h.get("path", "") for h in hits), hits)
        hits = ops.search("zorbafraz", limit=10)["results"]
        self.assertTrue(
            any("searchable.md" in (h.get("path") or h.get("source") or "") for h in hits),
            hits,
        )


if __name__ == "__main__":
    unittest.main()
