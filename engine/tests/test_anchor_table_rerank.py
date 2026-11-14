"""Phase 1: section anchors, table escapes, and rerank text alignment."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import chunk_anchor, config, core, ranking, table_markdown, vaults
from apo_engine.index_models import Hit
from apo_engine.markdown_patch import PatchError, find_section

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from test_core import VaultTestCase  # noqa: E402


class ParentAnchorTest(VaultTestCase):
    def test_parent_chunk_hash_resolves_to_parent_heading(self):
        self.write(
            "nest.md",
            "## Parent\n\nparent body stays here\n\n### Child\n\nchild body\n",
        )
        core.index_vault(verbose=False)
        db = core.writer_connect()
        rows = db.execute(
            "SELECT heading, heading_level, chunk_hash, start_line FROM chunks WHERE path = ?",
            ("nest.md",),
        ).fetchall()
        parent = next(r for r in rows if r[0] == "Parent" and int(r[1]) == 2)
        binding = vaults.VaultBinding("t", self.vault, Path(config.INDEX_PATH), "t")
        resolved = chunk_anchor.resolve_chunk_anchor(binding, parent[2])
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["heading"], "## Parent")


class FindSectionAmbiguousTest(unittest.TestCase):
    def test_two_matches_without_occurrence_are_ambiguous(self):
        lines = ["# Notes", "", "one", "# Notes", "", "two"]
        with self.assertRaises(PatchError) as ctx:
            find_section(lines, "# Notes")
        self.assertEqual(ctx.exception.code, "anchor_ambiguous")
        second = find_section(lines, "# Notes", occurrence=2)
        self.assertEqual(second.heading_line, 3)


class TableEscapeTest(unittest.TestCase):
    def test_windows_path_survives_edit_of_another_cell(self):
        rendered = table_markdown.records_to_gfm(
            ["path", "note"],
            [["C:\\Users", "keep"]],
        )
        table = table_markdown.find_tables(rendered.splitlines())[0]
        self.assertEqual(table.rows[0][0], "C:\\Users")
        edited = [list(table.rows[0])]
        edited[0][1] = "changed"
        again = table_markdown.records_to_gfm(["path", "note"], edited)
        reread = table_markdown.find_tables(again.splitlines())[0]
        self.assertEqual(reread.rows[0][0], "C:\\Users")
        self.assertEqual(reread.rows[0][1], "changed")


class _Row:
    def __init__(self, values):
        self._values = values

    def fetchone(self):
        return self._values


class _Conn:
    def execute(self, _sql, _params):
        return _Row(
            (
                "zebra-shade.md",
                "Z",
                "INJECTED",
                "hash",
                1,
                1,
                2,
                0.0,
                0,
                9,
                "c",
                "section",
                "",
                "",
            )
        )


class RerankAlignmentTest(unittest.TestCase):
    def test_injected_hit_keeps_its_text_after_sort(self):
        hits = [
            Hit(path="keep-low.md", heading="", text="LOW", score=0.4),
            Hit(path="keep-mid.md", heading="", text="MID", score=0.5),
        ]
        full = ["LOW", "MID"]
        with (
            mock.patch.object(
                ranking, "_phrase_stem_paths_in_folder", return_value=["zebra-shade.md"]
            ),
            mock.patch.object(ranking.index_db, "reader_connect", return_value=_Conn()),
        ):
            out_hits, out_texts = ranking._inject_phrase_stem_hits(
                hits, "zebra shade", "notes", full_texts=full
            )
        self.assertEqual(len(out_hits), len(out_texts))
        for hit, text in zip(out_hits, out_texts):
            self.assertEqual(hit.text, text)
        self.assertIn("INJECTED", out_texts)
