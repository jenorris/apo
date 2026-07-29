"""Agent arg aliases + note slicing."""

from __future__ import annotations

import unittest

from apo_engine.agent_args import (
    project_frontmatter,
    resolve_top_k,
    resolve_where,
    shape_note_read,
    slice_note_content,
)
from apo_engine.markdown_patch import PatchError


class ResolveTopKTest(unittest.TestCase):
    def test_default(self):
        k, err = resolve_top_k(None, None)
        self.assertEqual(k, 5)
        self.assertIsNone(err)

    def test_limit_alias(self):
        k, err = resolve_top_k(None, 10)
        self.assertEqual(k, 10)
        self.assertIsNone(err)

    def test_top_k_wins_when_equal(self):
        k, err = resolve_top_k(7, 7)
        self.assertEqual(k, 7)
        self.assertIsNone(err)

    def test_conflict(self):
        k, err = resolve_top_k(5, 10)
        self.assertIsNone(k)
        self.assertIn("alias for top_k", err or "")


class ResolveWhereTest(unittest.TestCase):
    def test_where(self):
        w, err = resolve_where({"status": "active"}, None)
        self.assertEqual(w, {"status": "active"})
        self.assertIsNone(err)

    def test_filters_alias(self):
        w, err = resolve_where(None, {"status": "active"})
        self.assertEqual(w, {"status": "active"})
        self.assertIsNone(err)

    def test_missing_defaults_empty(self):
        w, err = resolve_where(None, None)
        self.assertEqual(w, {})
        self.assertIsNone(err)

    def test_conflict(self):
        w, err = resolve_where({"a": 1}, {"a": 2})
        self.assertIsNone(w)
        self.assertIn("filters is an alias", err or "")


class ProjectFrontmatterTest(unittest.TestCase):
    FM = {
        "title": "T",
        "status": "active",
        "okf_type": "Thread",
        "description": "long text",
        "last_checked": "2026-07-24",
    }

    def test_default_full(self):
        self.assertEqual(project_frontmatter(self.FM, None), self.FM)
        self.assertEqual(project_frontmatter(self.FM, []), self.FM)

    def test_projection(self):
        out = project_frontmatter(
            self.FM,
            ["status", "okf_type", "last_checked", "title", "missing"],
        )
        self.assertEqual(
            out,
            {
                "status": "active",
                "okf_type": "Thread",
                "last_checked": "2026-07-24",
                "title": "T",
            },
        )
        self.assertNotIn("description", out)
        self.assertNotIn("missing", out)


class SliceNoteContentTest(unittest.TestCase):
    SAMPLE = (
        "---\ntitle: T\n---\n\n"
        "# Intro\n\n"
        "line one\n"
        "line two\n\n"
        "## Detail\n\n"
        "detail body\n"
    )

    def test_full(self):
        out = slice_note_content(self.SAMPLE)
        self.assertIn("line one", out["content"])
        self.assertIn("detail body", out["content"])
        self.assertFalse(out["truncated"])

    def test_line_range(self):
        out = slice_note_content(self.SAMPLE, start_line=7, end_line=8)
        self.assertEqual(out["content"], "line one\nline two")
        self.assertEqual(out["start_line"], 7)
        self.assertEqual(out["end_line"], 8)

    def test_heading(self):
        out = slice_note_content(self.SAMPLE, heading="## Detail")
        self.assertIn("detail body", out["content"])
        self.assertNotIn("line one", out["content"])
        self.assertTrue(out["heading"].startswith("##"))

    def test_max_chars(self):
        out = slice_note_content(self.SAMPLE, max_chars=20)
        self.assertEqual(len(out["content"]), 20)
        self.assertTrue(out["truncated"])

    def test_bad_heading(self):
        with self.assertRaises(PatchError):
            slice_note_content(self.SAMPLE, heading="## Missing")


class ShapeNoteReadTest(unittest.TestCase):
    SAMPLE = (
        "---\ntitle: T\nstatus: open\n---\n\n"
        "# Intro\n\n"
        "line one\n"
        "line two\n\n"
        "## Detail\n\n"
        "detail body\n"
    )

    def test_full_body_strips_frontmatter(self):
        out = shape_note_read(self.SAMPLE)
        self.assertEqual(out["frontmatter"], {"title": "T", "status": "open"})
        self.assertNotIn("title: T", out["content"])
        self.assertIn("# Intro", out["content"])
        self.assertIn("detail body", out["content"])
        self.assertEqual(out["start_line"], 5)
        self.assertFalse(out["truncated"])

    def test_raw_keeps_yaml(self):
        out = shape_note_read(self.SAMPLE, raw_content=True)
        self.assertEqual(out["frontmatter"], {"title": "T", "status": "open"})
        self.assertTrue(out["content"].startswith("---\n"))
        self.assertIn("title: T", out["content"])
        self.assertEqual(out["start_line"], 1)

    def test_heading_keeps_sidecar(self):
        out = shape_note_read(self.SAMPLE, heading="## Detail")
        self.assertEqual(out["frontmatter"]["title"], "T")
        self.assertIn("detail body", out["content"])
        self.assertNotIn("line one", out["content"])
        self.assertNotIn("title: T", out["content"])

    def test_no_frontmatter(self):
        out = shape_note_read("# Only\n\nbody\n")
        self.assertIsNone(out["frontmatter"])
        self.assertEqual(out["content"], "# Only\n\nbody\n")
        self.assertEqual(out["start_line"], 1)

    def test_line_range_absolute(self):
        # Lines: 1-4 FM, 5 blank, 6 # Intro, 7 blank, 8–9 body lines
        out = shape_note_read(self.SAMPLE, start_line=8, end_line=9)
        self.assertEqual(out["frontmatter"]["status"], "open")
        self.assertEqual(out["content"], "line one\nline two")
        self.assertEqual(out["start_line"], 8)


if __name__ == "__main__":
    unittest.main()
