"""Regression test for folder-scoped phrase-stem injection's representative-chunk
selection (``ranking._inject_phrase_stem_hits``).

``core._markdown_chunk_rows`` and ``mermaid_index.chunk_mermaid_rows`` both
hardcode ``level = 0`` for table_header/table_row/mermaid_* chunks, while real
prose section chunks carry ``heading_level >= 1``. The old
``ORDER BY c.heading_level ASC, c.start_line ASC`` picked a note's table/mermaid
chunk as its representative over any real section chunk, regardless of where
the note's prose actually starts — injected hits bypass
``_catalog_retrieval_boost`` entirely, so nothing downstream could correct it.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ranking

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


class InjectPhraseStemRepresentativeChunkTest(unittest.TestCase):
    """A note with both a table_header chunk (heading_level hardcoded to 0) and a
    real prose section chunk (heading_level >= 1) — folder-scoped phrase-stem
    injection must pick the prose chunk, not the table.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / "catalog" / "pages").mkdir(parents=True)
        (self.vault / "catalog" / "pages" / "widget-catalog-diagram.md").write_text(
            "---\ntitle: Widget Overview\n---\n\n"
            "# Widget Overview\n\n"
            "This gadget assembly requires careful torque calibration on every bolt.\n\n"
            "## Metadata\n\n"
            "| Field | Value |\n"
            "|-------|-------|\n"
            "| Owner | Ops   |\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "inject_repr_chunk_test"),
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

    def test_prose_section_wins_over_table_header(self):
        # Confirm the fixture actually reproduces the heading_level=0 collision
        # this bug depended on, before asserting the fix picked around it.
        db = core.reader_connect()
        rows = db.execute(
            "SELECT chunk_kind, heading_level FROM chunks WHERE path=?",
            ("catalog/pages/widget-catalog-diagram.md",),
        ).fetchall()
        kinds = {kind: level for kind, level in rows}
        self.assertEqual(kinds.get("table_header"), 0)
        self.assertGreaterEqual(kinds.get("section"), 1)

        hits, _ = ranking._inject_phrase_stem_hits([], "widget catalog diagram", "catalog")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.path, "catalog/pages/widget-catalog-diagram.md")
        self.assertEqual(hit.chunk_kind, "section")
        self.assertIn("torque calibration", hit.text)


if __name__ == "__main__":
    unittest.main()
