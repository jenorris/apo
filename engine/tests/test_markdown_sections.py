"""Shared section splitter — index span == patch span == read span."""

from __future__ import annotations

import unittest

from apo_engine.core import section_markdown
from apo_engine.markdown_patch import find_section, section_from_chunk
from apo_engine.markdown_sections import find_headings, split_sections

NESTED = """---
title: Nested
---

## Parent

Parent body.

### Child A

Child A body.

### Child B

Child B body.

## Sibling

Sibling body.
"""


class TestHierarchicalBoundaries(unittest.TestCase):
    def test_parent_owns_children_in_index(self):
        secs = {bc: (s, e) for bc, _lvl, _txt, s, e in section_markdown(NESTED)}
        parent = next(k for k in secs if k == "Parent")
        p_start, p_end = secs[parent]
        # Parent's hierarchical span must include both children (ends before ## Sibling).
        self.assertIn("Child A", "\n".join(NESTED.split("\n")[p_start - 1 : p_end]))
        self.assertIn("Child B", "\n".join(NESTED.split("\n")[p_start - 1 : p_end]))

    def test_patch_span_matches_index_span(self):
        lines = NESTED.split("\n")
        # Index span for "## Parent"
        idx_span = next(
            (s, e) for bc, lvl, _t, s, e in section_markdown(NESTED) if bc == "Parent"
        )
        # Patch span via find_section (heading anchor)
        sec = find_section(lines, "## Parent")
        # find_section body_end is exclusive 0-based; index end_line is inclusive 1-based.
        self.assertEqual(sec.heading_line + 1, idx_span[0])
        self.assertEqual(sec.body_end, idx_span[1])

    def test_read_span_matches_index_span(self):
        lines = NESTED.split("\n")
        idx_start, idx_end = next(
            (s, e) for bc, lvl, _t, s, e in section_markdown(NESTED) if bc == "Parent"
        )
        sec = section_from_chunk(lines, start_line=idx_start, heading_level=2)
        self.assertEqual(sec.title, "Parent")
        self.assertEqual(sec.body_end, idx_end)

    def test_breadcrumb_trail(self):
        crumbs = {tuple(s.breadcrumb) for s in split_sections(NESTED.split("\n")) if s.title}
        self.assertIn(("Parent", "Child A"), crumbs)
        self.assertIn(("Parent",), crumbs)

    def test_headings_skip_code_fences(self):
        text = "## Real\n\n```\n## Fake heading in fence\n```\n"
        heads = find_headings(text.split("\n"))
        self.assertEqual([h.title for h in heads], ["Real"])


if __name__ == "__main__":
    unittest.main()
