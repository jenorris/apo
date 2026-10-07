"""Oversize section handling: rollup outlines, leaf windows, parent collapse, degrade-not-drop."""

from __future__ import annotations

import sqlite3
import unittest

from apo_engine import config, core, oversize

from test_core import VaultTestCase, _fake_embed


def _ident(t: str) -> str:
    return t


def _para(word: str, n: int) -> str:
    """One paragraph of roughly n chars built from ``word``."""
    return " ".join([word] * max(n // (len(word) + 1), 1))


class TestSplitWindows(unittest.TestCase):
    def test_windows_fit_budget_and_cover_every_paragraph(self):
        paras = [f"{_para(f'alpha{i}', 90)}" for i in range(12)]
        text = "\n\n".join(paras)
        wins = oversize.split_windows(text, 10, budget=300, overlap=0)
        self.assertGreater(len(wins), 1)
        self.assertTrue(all(len(w.text) <= 300 for w in wins))
        joined = "\n".join(w.text for w in wins)
        for i in range(12):
            self.assertIn(f"alpha{i}", joined)

    def test_line_spans_point_at_real_lines(self):
        text = "one\n\ntwo two\n\nthree three three\n"
        wins = oversize.split_windows(text, 100, budget=20, overlap=0)
        self.assertGreater(len(wins), 1)
        lines = text.split("\n")
        for w in wins:
            span = "\n".join(lines[w.start_line - 100 : w.end_line - 100 + 1]).strip()
            self.assertEqual(span, w.text)

    def test_overlap_repeats_trailing_paragraph(self):
        text = "\n\n".join([_para("aaa", 80), _para("bbb", 80), _para("ccc", 80)])
        wins = oversize.split_windows(text, 1, budget=200, overlap=100)
        self.assertGreaterEqual(len(wins), 2)
        last_para_of_first = wins[0].text.split("\n\n")[-1]
        self.assertIn(last_para_of_first, wins[1].text)

    def test_table_without_blank_lines_stays_together(self):
        table = "| a | b |\n|---|---|\n" + "\n".join(f"| r{i} | v{i} |" for i in range(5))
        text = _para("intro", 200) + "\n\n" + table + "\n\n" + _para("outro", 200)
        wins = oversize.split_windows(text, 1, budget=300, overlap=0)
        holder = [w for w in wins if "| a | b |" in w.text]
        self.assertEqual(len(holder), 1)
        self.assertIn("| r4 | v4 |", holder[0].text)

    def test_blank_lines_inside_fence_do_not_split(self):
        text = "```\nline one\n\nline two\n```\n\n" + _para("tail", 300)
        wins = oversize.split_windows(text, 1, budget=100, overlap=0)
        fenced = [w for w in wins if "line one" in w.text]
        self.assertEqual(len(fenced), 1)
        self.assertIn("line two", fenced[0].text)

    def test_single_overlong_line_is_hard_split(self):
        wins = oversize.split_windows("x" * 1000, 1, budget=300, overlap=0)
        self.assertGreater(len(wins), 1)
        self.assertTrue(all(len(w.text) <= 300 for w in wins))
        self.assertEqual(sum(len(w.text) for w in wins), 1000)


class TestPlanSection(unittest.TestCase):
    def test_within_budget_needs_no_plan(self):
        secs = [("A", 1, "# A\n\nshort", 1, 3)]
        self.assertIsNone(oversize.plan_section(secs, 0, 500, 0, _ident))

    def test_rollup_gets_outline_not_full_text(self):
        body = _para("detail", 900)
        secs = [
            ("Doc", 1, f"# Doc\n\nthis note tracks clocks\n\n## One\n\n{body}\n\n## Two\n\n{body}", 1, 12),
            ("Doc › One", 2, f"## One\n\n{body}", 5, 7),
            ("Doc › Two", 2, f"## Two\n\n{body}", 9, 11),
        ]
        plan = oversize.plan_section(secs, 0, 400, 0, _ident)
        self.assertEqual(plan.kind, "rollup")
        self.assertEqual(plan.parts, [])
        self.assertIn("this note tracks clocks", plan.embed_text)
        self.assertIn("- One", plan.embed_text)
        self.assertIn("- Two", plan.embed_text)
        self.assertLessEqual(len(plan.embed_text), 400)
        self.assertNotIn("detail", plan.embed_text)

    def test_outline_is_capped_with_ellipsis(self):
        kids = [(f"Doc › Heading number {i}", 2, f"## Heading number {i}\n\nx", 3 + 2 * i, 3 + 2 * i) for i in range(60)]
        top = ("Doc", 1, "# Doc\n\n" + _para("w", 3000), 1, 200)
        plan = oversize.plan_section([top, *kids], 0, 300, 0, _ident)
        self.assertLessEqual(len(plan.embed_text), 300)
        self.assertIn("…", plan.embed_text)

    def test_leaf_splits_into_head_plus_parts(self):
        text = "# Leaf\n\n" + "\n\n".join(_para(f"w{i}", 150) for i in range(10))
        secs = [("Leaf", 1, text, 1, 30)]
        plan = oversize.plan_section(secs, 0, 400, 0, _ident)
        self.assertEqual(plan.kind, "leaf")
        self.assertGreaterEqual(len(plan.parts), 2)
        self.assertLessEqual(len(plan.embed_text), 400)
        self.assertIn("w0", plan.embed_text)
        self.assertNotIn("w0", " ".join(p.text for p in plan.parts))

    def test_children_must_sit_inside_the_span(self):
        secs = [
            ("A", 1, "# A", 1, 4),
            ("A › B", 2, "## B", 3, 4),
            ("C", 1, "# C", 5, 8),
            ("C › D", 2, "## D", 7, 8),
        ]
        self.assertEqual([c[0] for c in oversize.children_of(secs, 0)], ["A › B"])
        self.assertEqual([c[0] for c in oversize.children_of(secs, 2)], ["C › D"])


class OversizeIndexCase(VaultTestCase):
    def setUp(self):
        super().setUp()
        self._saved_oversize = (config.EMBED_MAX_CHARS, config.EMBED_WINDOW_OVERLAP)
        config.EMBED_MAX_CHARS = 800
        config.EMBED_WINDOW_OVERLAP = 0
        self.embedded: list[str] = []

        def capture(texts, **kwargs):
            self.embedded.extend(texts)
            return _fake_embed(texts, **kwargs)

        core.embed = capture

    def tearDown(self):
        config.EMBED_MAX_CHARS, config.EMBED_WINDOW_OVERLAP = self._saved_oversize
        super().tearDown()

    def rows(self, sql: str, *args):
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def leaf_note(self) -> str:
        paras = [_para(f"filler{i}", 300) for i in range(8)]
        paras[6] = "the deep marker is zibblewomp and nothing else here"
        return "# Leaf Note\n\n" + "\n\n".join(paras) + "\n"


class TestOversizeIndexing(OversizeIndexCase):
    def test_no_embed_input_exceeds_budget(self):
        self.write("leaf.md", self.leaf_note())
        core.index_vault(verbose=False)
        self.assertTrue(self.embedded)
        self.assertLessEqual(max(len(t) for t in self.embedded), config.EMBED_MAX_CHARS)

    def test_leaf_gets_parts_linked_to_parent(self):
        self.write("leaf.md", self.leaf_note())
        core.index_vault(verbose=False)
        parts = self.rows(
            "SELECT parent_chunk_hash FROM chunks WHERE path='leaf.md' AND chunk_kind='section_part'"
        )
        self.assertGreaterEqual(len(parts), 1)
        parent_hash = parts[0][0]
        parent = self.rows(
            "SELECT chunk_kind, length(text) FROM chunks WHERE chunk_hash=?", parent_hash
        )
        self.assertEqual(parent[0][0], "section")
        self.assertGreater(parent[0][1], config.EMBED_MAX_CHARS)  # parent keeps the full text

    def test_deep_text_is_searchable_by_keyword_and_resolves_to_the_section(self):
        self.write("leaf.md", self.leaf_note())
        self.write("other.md", "# Other\n\nunrelated aardvark note\n")
        core.index_vault(verbose=False)
        hits = core.search("zibblewomp", k=5)
        self.assertEqual(hits[0].path, "leaf.md")
        self.assertEqual({h.chunk_kind for h in hits}, {"section"})

    def test_parts_have_no_fts_rows(self):
        self.write("leaf.md", self.leaf_note())
        core.index_vault(verbose=False)
        n_parts = self.rows("SELECT COUNT(*) FROM chunks WHERE chunk_kind='section_part'")[0][0]
        self.assertGreaterEqual(n_parts, 1)
        # FTS rows for parts are empty strings — keyword hits cannot land on a part.
        empty = self.rows(
            "SELECT COUNT(*) FROM chunks_fts_content c JOIN chunks k ON k.id=c.id "
            "WHERE k.chunk_kind='section_part' AND c.c0=''"
        )[0][0]
        self.assertEqual(empty, n_parts)

    def test_vector_search_on_part_text_returns_parent_once(self):
        self.write("leaf.md", self.leaf_note())
        core.index_vault(verbose=False)
        part_text = self.rows(
            "SELECT text FROM chunks WHERE path='leaf.md' AND chunk_kind='section_part' LIMIT 1"
        )[0][0]
        hits = core.search_vector_only(_fake_embed([part_text])[0], k=10)
        leaf_hits = [h for h in hits if h.path == "leaf.md"]
        self.assertEqual(len(leaf_hits), len({h.chunk_hash for h in leaf_hits}))
        self.assertTrue(all(h.chunk_kind == "section" for h in leaf_hits))

    def test_rollup_embeds_outline_but_stores_and_indexes_full_text(self):
        body = _para("cadence", 700)
        self.write(
            "clocks.md",
            f"# Clocks\n\nintro about recurring duties\n\n## Alpha\n\n{body}\n\n## Beta\n\n{body}\n\n"
            f"## Gamma\n\nthe rare token is quuxbarrel\n",
        )
        core.index_vault(verbose=False)
        self.assertTrue(any("- Alpha" in t and "intro about recurring duties" in t for t in self.embedded))
        top = self.rows("SELECT length(text) FROM chunks WHERE path='clocks.md' AND heading_level=1")[0][0]
        self.assertGreater(top, config.EMBED_MAX_CHARS)
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM chunks WHERE path='clocks.md' AND chunk_kind='section_part'")[0][0], 0
        )
        self.assertEqual(core.search("quuxbarrel", k=3)[0].path, "clocks.md")

    def test_disabled_budget_restores_whole_section_embedding(self):
        config.EMBED_MAX_CHARS = 0
        self.write("leaf.md", self.leaf_note())
        core.index_vault(verbose=False)
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM chunks WHERE chunk_kind='section_part'")[0][0], 0
        )
        self.assertGreater(max(len(t) for t in self.embedded), 2000)


class TestDegradeNotDrop(OversizeIndexCase):
    def test_input_the_backend_rejects_is_retried_shorter_instead_of_dropping_the_file(self):
        config.EMBED_MAX_CHARS = 0  # keep the section whole so the backend sees an oversize input
        self.write("big.md", "# Big\n\n" + _para("bulk", 5000) + "\n")

        def picky(texts, **kwargs):
            return [None if len(t) > 3500 else _fake_embed([t])[0] for t in texts]

        core.embed = picky
        with self.assertLogs("apo.index", level="WARNING") as cm:
            core.index_vault(verbose=False)
        self.assertIn("big.md", self.chunk_paths())
        self.assertTrue(any("embed degraded for big.md" in m for m in cm.output))
        self.assertNotIn("bulk bulk", "\n".join(cm.output))  # content never logged

    def test_short_failing_chunk_still_drops_the_file(self):
        self.write("bad.md", "# Bad\n\npoisoned wombat body\n")
        core.embed = lambda texts, **kw: [None for _ in texts]
        core.index_vault(verbose=False)
        self.assertNotIn("bad.md", self.chunk_paths())


if __name__ == "__main__":
    unittest.main()
