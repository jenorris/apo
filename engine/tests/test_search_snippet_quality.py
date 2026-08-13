"""Snippet value optimization: table collapsing, markdown decoration
stripping, word-boundary truncation, and FTS5 query-anchored excerpts.

See jeremy vault projects/apo-pkb/search-snippet-optimization.md for the
design note this implements (Phases 1-3; Phase 4 storage-time normalization
is deferred pending a full reindex).
"""

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


# --------------------------------------------------------------------------- #
# Pure helper unit tests — no index/vault needed.
# --------------------------------------------------------------------------- #
class CollapseFullTablesTest(unittest.TestCase):
    def test_replaces_table_with_marker(self):
        text = (
            "#### Barbarian Level Table\n\n"
            "**Table- The Barbarian**\n\n"
            "| Level | Proficiency Bonus | Features |\n"
            "|-------|-------------------|----------|\n"
            "| 1st   | +2                | Rage     |\n"
            "| 2nd   | +2                | Reckless |\n"
        )
        out = core._collapse_full_tables(text)
        self.assertNotIn("|-------|", out)
        self.assertNotIn("| 1st", out)
        self.assertIn("[table: 2 rows — Level, Proficiency Bonus, Features]", out)
        # Non-table prose is untouched.
        self.assertIn("#### Barbarian Level Table", out)

    def test_no_table_is_a_noop(self):
        text = "Just prose, no pipes here at all."
        self.assertEqual(core._collapse_full_tables(text), text)

    def test_singular_row_count(self):
        text = "| A | B |\n|---|---|\n| x | y |\n"
        out = core._collapse_full_tables(text)
        self.assertIn("[table: 1 row —", out)


class StripMarkdownDecorationTest(unittest.TestCase):
    def test_strips_bold_italic_heading_list_quote_link(self):
        text = (
            "#### Heading\n\n"
            "**bold** and *italic* and a [link](https://example.com/x).\n\n"
            "- bullet one\n"
            "> a quote\n"
        )
        out = core._strip_markdown_decoration(text)
        self.assertNotIn("**", out)
        self.assertNotIn("#### ", out)
        self.assertNotIn("- bullet", out)
        self.assertNotIn("> a quote", out)
        self.assertIn("bold", out)
        self.assertIn("italic", out)
        self.assertIn("link", out)
        self.assertIn("bullet one", out)
        self.assertIn("a quote", out)

    def test_does_not_touch_bare_pipes(self):
        # Table cells are already collapsed upstream by _collapse_full_tables;
        # this function must not go hunting for pipes on its own.
        text = "a | b | c"
        self.assertEqual(core._strip_markdown_decoration(text), text)


class TruncateWordBoundaryTest(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(core._truncate_word_boundary("hello world", 240), "hello world")

    def test_cuts_at_whitespace_not_mid_word(self):
        text = "one two three four five six seven eight nine ten"
        out = core._truncate_word_boundary(text, 20)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(text.startswith(out))
        # The next character after the cut in the source must be a word
        # boundary (space) or end of string — never mid-word.
        self.assertIn(text[len(out) : len(out) + 1], (" ", ""))

    def test_single_long_token_falls_back_to_hard_cut(self):
        text = "x" * 500
        out = core._truncate_word_boundary(text, 50)
        self.assertEqual(len(out), 50)

    def test_zero_limit_is_noop(self):
        self.assertEqual(core._truncate_word_boundary("anything", 0), "anything")


class BuildSnippetTest(unittest.TestCase):
    def test_snippet_chars_zero_returns_full_text(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n" * 5
        self.assertEqual(core._build_snippet(text, 0, "section"), text)

    def test_table_row_chunk_never_truncated(self):
        flat = "Wizard > Class Features > Wizard Level Table — Level: 7th, 1st: 4, 2nd: 3, 3rd: 3, 4th: 1"
        out = core._build_snippet(flat, 10, "table_row")
        self.assertEqual(out, flat)

    def test_table_header_chunk_never_truncated(self):
        flat = "Wizard > Class Features > Wizard Level Table — Columns: Level, 1st, 2nd, 3rd, 4th"
        out = core._build_snippet(flat, 5, "table_header")
        self.assertEqual(out, flat)

    def test_section_chunk_collapses_table_before_truncating(self):
        text = (
            "#### Barbarian Level Table\n\n"
            "| Level | Proficiency Bonus | Features |\n"
            "|-------|-------------------|----------|\n"
            "| 1st   | +2                | Rage     |\n"
            "| 2nd   | +2                | Reckless |\n"
        )
        out = core._build_snippet(text, 60, "section")
        self.assertNotIn("|-------|", out)
        self.assertIn("[table:", out)


# --------------------------------------------------------------------------- #
# Phase 4: chunks.text (canonical, read_note/hash-relevant) must stay raw;
# only the embed/FTS input is cleaned. Pure unit tests first.
# --------------------------------------------------------------------------- #
class IndexTextForEmbeddingTest(unittest.TestCase):
    def test_section_text_is_cleaned(self):
        raw = "**bold** text\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        out = core._index_text_for_embedding(raw, "section")
        self.assertNotIn("**", out)
        self.assertNotIn("|---|", out)
        self.assertIn("bold", out)

    def test_table_row_and_header_pass_through_unchanged(self):
        flat = "Wizard > Class Features > Wizard Level Table — Level: 7th, 1st: 4"
        self.assertEqual(core._index_text_for_embedding(flat, "table_row"), flat)
        self.assertEqual(core._index_text_for_embedding(flat, "table_header"), flat)

    def test_default_chunk_kind_treated_as_section(self):
        raw = "**bold**"
        self.assertEqual(core._index_text_for_embedding(raw, ""), "bold")

    def test_never_returns_empty_string(self):
        # A pathological input that cleans down to nothing must still index
        # as *something*, or the chunk becomes unsearchable by keyword.
        raw = "**"
        out = core._index_text_for_embedding(raw, "section")
        self.assertTrue(out)


# --------------------------------------------------------------------------- #
# End-to-end: canonical chunks.text / read_note stay byte-for-byte raw, while
# chunks_fts (and therefore embeddings) get the cleaned text. Verifies the
# storage-time split actually holds through the real indexing pipeline, not
# just in the pure helper.
# --------------------------------------------------------------------------- #
class IndexTextSeparationIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "gear.md").write_text(
            "---\ntitle: Gear\n---\n\n"
            "#### Gear Table\n\n"
            "**Table- The Gear**\n\n"
            "| Item | Cost | Weight |\n"
            "|------|------|--------|\n"
            "| Rope | 1 gp | 10 lb. |\n"
            "| Torch | 1 cp | 1 lb. |\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "index_text_separation_test"),
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

    def test_canonical_chunks_text_is_untouched(self):
        db = core.reader_connect()
        row = db.execute(
            "SELECT text FROM chunks WHERE path=? AND chunk_kind='section' "
            "AND text LIKE '%Gear Table%'",
            ("gear.md",),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("**Table- The Gear**", row[0])
        self.assertIn("|------|------|--------|", row[0])

    def test_chunks_fts_text_is_cleaned(self):
        db = core.reader_connect()
        row = db.execute(
            "SELECT chunks_fts.text FROM chunks_fts "
            "JOIN chunks c ON c.id = chunks_fts.rowid "
            "WHERE c.path=? AND c.chunk_kind='section' AND chunks_fts.text LIKE '%Gear Table%'",
            ("gear.md",),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn("|------|------|--------|", row[0])
        self.assertNotIn("**Table- The Gear**", row[0])
        self.assertIn("[table:", row[0])

    def test_read_note_still_returns_raw_markdown(self):
        out = ops.read_note("gear.md", vault="")
        self.assertTrue(out["ok"], out)
        self.assertIn("**Table- The Gear**", out["content"])
        self.assertIn("|------|------|--------|", out["content"])


# --------------------------------------------------------------------------- #
# End-to-end: real chunks_fts index, proving query-anchored excerpts surface
# a match that a naive prefix slice would have missed.
# --------------------------------------------------------------------------- #
class QueryAnchoredSnippetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        # The matching term sits ~800 chars into the section — well past any
        # reasonable snippet_chars prefix. A naive text[:240] would never
        # show it; a query-anchored excerpt should.
        filler = "Lorem ipsum filler sentence about nothing in particular. " * 14
        (self.vault / "deep.md").write_text(
            "---\ntitle: Deep\n---\n\n"
            "# Deep\n\n"
            f"{filler}\nThe secret unicorn treasure is buried under the oak tree.\n{filler}\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "snippet_quality_test"),
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

    def test_fts_anchored_excerpt_surfaces_deep_match(self):
        out = ops.search("unicorn treasure oak tree", limit=3, snippet_chars=120)
        self.assertTrue(out["ok"], out)
        hit = next(r for r in out["results"] if r["source"] == "deep.md")
        self.assertIn("unicorn", hit["content"])

    def test_prefix_fallback_when_snippet_chars_zero(self):
        # Sanity: full-text mode still returns the whole chunk untouched.
        out = ops.search("unicorn treasure oak tree", limit=3, snippet_chars=0)
        self.assertTrue(out["ok"], out)
        hit = next(r for r in out["results"] if r["source"] == "deep.md")
        self.assertIn("unicorn", hit["content"])
        self.assertIn("Lorem ipsum", hit["content"])


if __name__ == "__main__":
    unittest.main()
