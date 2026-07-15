"""Tests for markdown_patch."""

from __future__ import annotations

import unittest

from apo_engine.markdown_patch import (
    PatchError,
    apply_append,
    apply_patch,
    find_section,
    minimal_note_stub,
    section_from_chunk,
)


THREAD = """---
title: Test Thread
status: active
last_checked: 2026-07-08
---

## Summary

Short summary.

## History

- 2026-07-08 — created.

## Next action

- [ ] Do thing
"""


class TestAppend(unittest.TestCase):
    def test_append_history(self):
        lines = THREAD.split("\n")
        merged, detail = apply_append(lines, "- 2026-07-09 — update.\n", heading="## History")
        text = "\n".join(merged)
        self.assertIn("- 2026-07-09 — update.", text)
        self.assertIn("- 2026-07-08 — created.", text)
        self.assertIn("Do thing", text)

    def test_prepend_session_log(self):
        daily = """---
title: '2026-07-09'
---

## Session log

**old entry**

## Briefing
"""
        result = apply_patch(
            daily,
            [{"op": "prepend", "heading": "## Session log", "text": "**new entry**\n\n"}],
        )
        self.assertTrue(result.ok)
        idx_new = result.content.index("**new entry**")
        idx_old = result.content.index("**old entry**")
        self.assertLess(idx_new, idx_old)

    def test_append_eof(self):
        result = apply_patch("line1\n", [{"op": "append_eof", "text": "line2\n"}])
        self.assertTrue(result.ok)
        self.assertIn("line2", result.content)


class TestPatch(unittest.TestCase):
    def test_set_field_existing(self):
        result = apply_patch(THREAD, [{"op": "set_field", "field": "status", "value": "resolved"}])
        self.assertTrue(result.ok)
        self.assertIn("status: resolved", result.content)
        self.assertIn("title: Test Thread", result.content)

    def test_set_field_new(self):
        result = apply_patch(THREAD, [{"op": "set_field", "field": "timestamp", "value": "2026-07-09T19:30:00Z"}])
        self.assertTrue(result.ok)
        self.assertIn('timestamp: "2026-07-09T19:30:00Z"', result.content)

    def test_set_field_quotes_invalid_date(self):
        # Invalid YYYY-MM-DD raises ValueError inside PyYAML; must still quote safely.
        result = apply_patch(
            THREAD,
            [{"op": "set_field", "field": "effective_date", "value": "2017-00-00"}],
        )
        self.assertTrue(result.ok)
        self.assertIn('effective_date: "2017-00-00"', result.content)

    def test_replace_text_scoped(self):
        result = apply_patch(
            THREAD,
            [{
                "op": "replace_text",
                "find": "- [ ] Do thing",
                "replace": "- [x] Do thing",
                "scope": {"heading": "## Next action"},
            }],
        )
        self.assertTrue(result.ok)
        self.assertIn("- [x] Do thing", result.content)

    def test_batch_thread_upsert(self):
        ops = [
            {"op": "append", "heading": "## History", "text": "- 2026-07-09 — done.\n"},
            {"op": "set_field", "field": "last_checked", "value": "2026-07-09 15:30"},
            {"op": "set_field", "field": "status", "value": "resolved"},
        ]
        result = apply_patch(THREAD, ops)
        self.assertTrue(result.ok)
        self.assertEqual(result.applied, 3)
        self.assertIn('last_checked: "2026-07-09 15:30"', result.content)
        self.assertIn("- 2026-07-09 — done.", result.content)

    def test_strict_aborts(self):
        result = apply_patch(
            THREAD,
            [
                {"op": "append", "heading": "## Histroy", "text": "x"},
                {"op": "set_field", "field": "status", "value": "resolved"},
            ],
            strict=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.applied, 0)
        self.assertNotIn("status: resolved", result.content)

    def test_non_strict_partial(self):
        result = apply_patch(
            THREAD,
            [
                {"op": "append", "heading": "## Histroy", "text": "x"},
                {"op": "set_field", "field": "status", "value": "resolved"},
            ],
            strict=False,
        )
        self.assertTrue(result.ok)
        self.assertIn("status: resolved", result.content)

    def test_replace_section(self):
        result = apply_patch(
            THREAD,
            [{"op": "replace_section", "heading": "## Summary", "text": "Updated summary."}],
        )
        self.assertTrue(result.ok)
        self.assertIn("Updated summary.", result.content)
        self.assertNotIn("Short summary.", result.content)

    def test_heading_not_found_suggestions(self):
        lines = THREAD.split("\n")
        with self.assertRaises(PatchError) as ctx:
            find_section(lines, "## Histroy")
        self.assertEqual(ctx.exception.code, "anchor_not_found")
        self.assertTrue(ctx.exception.suggestions)


class TestChunkSection(unittest.TestCase):
    def test_section_from_chunk(self):
        lines = THREAD.split("\n")
        section = section_from_chunk(lines, start_line=12, heading_level=2)
        self.assertEqual(section.title, "History")


class TestStub(unittest.TestCase):
    def test_minimal_stub(self):
        stub = minimal_note_stub("areas/threads/foo-bar.md")
        self.assertIn("title:", stub)
        self.assertTrue(stub.startswith("---"))


if __name__ == "__main__":
    unittest.main()
