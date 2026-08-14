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

    def test_append_strips_duplicate_anchor_heading(self):
        # Clients often pass heading= and also put "## Session log" in text.
        lines = """---
title: '2026-07-09'
---

## Session log

**old**
""".split("\n")
        merged, _ = apply_append(
            lines,
            "## Session log\n\n**2026-07-09 17:19 ET** — new.\n\n",
            heading="## Session log",
            position="start",
        )
        text = "\n".join(merged)
        self.assertEqual(text.count("## Session log"), 1)
        self.assertIn("**2026-07-09 17:19 ET** — new.", text)
        self.assertLess(text.index("**2026-07-09 17:19 ET**"), text.index("**old**"))

    def test_prepend_strips_duplicate_anchor_heading_via_patch(self):
        daily = """---
title: '2026-07-09'
---

## Session log

**old entry**
"""
        result = apply_patch(
            daily,
            [{
                "op": "prepend",
                "heading": "## Session log",
                "text": "## Session log\n\n**new entry**\n\n",
            }],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.content.count("## Session log"), 1)
        self.assertLess(
            result.content.index("**new entry**"),
            result.content.index("**old entry**"),
        )

    def test_append_keeps_unrelated_leading_heading(self):
        lines = THREAD.split("\n")
        merged, _ = apply_append(
            lines,
            "### Notes\n\n- detail\n",
            heading="## History",
        )
        text = "\n".join(merged)
        self.assertIn("### Notes", text)
        self.assertIn("- detail", text)

    def test_append_eof_does_not_strip_heading(self):
        # No section anchor → leading heading is intentional (new section at EOF).
        result = apply_patch(
            "line1\n",
            [{"op": "append_eof", "text": "## Footer\n\nx\n"}],
        )
        self.assertTrue(result.ok)
        self.assertIn("## Footer", result.content)

    def test_append_eof(self):
        result = apply_patch("line1\n", [{"op": "append_eof", "text": "line2\n"}])
        self.assertTrue(result.ok)
        self.assertIn("line2", result.content)

    def test_bare_prepend_after_frontmatter(self):
        src = """---
title: Test
---

## 2026-08-13

Body here.
"""
        result = apply_patch(
            src,
            [{"op": "prepend", "position": "start", "text": "## 2026-08-14 — process\n\nDecision: x\n\n"}],
        )
        self.assertTrue(result.ok)
        self.assertIn("document start", result.results[0]["detail"])
        self.assertLess(
            result.content.index("## 2026-08-14 — process"),
            result.content.index("## 2026-08-13"),
        )
        self.assertTrue(result.content.startswith("---\ntitle: Test\n---\n"))

    def test_bare_prepend_without_frontmatter(self):
        result = apply_patch(
            "## First\n\nbody\n",
            [{"op": "prepend", "text": "## NEW\n\nx\n"}],
        )
        self.assertTrue(result.ok)
        self.assertIn("document start", result.results[0]["detail"])
        self.assertTrue(result.content.startswith("## NEW\n"))
        self.assertLess(result.content.index("## NEW"), result.content.index("## First"))

    def test_bare_append_still_eof(self):
        src = """---
title: Test
---

## Section

body
"""
        result = apply_patch(src, [{"op": "append", "text": "## Footer\n\nz\n"}])
        self.assertTrue(result.ok)
        self.assertIn("at EOF", result.results[0]["detail"])
        self.assertGreater(
            result.content.index("## Footer"),
            result.content.index("## Section"),
        )


class TestPatch(unittest.TestCase):
    def test_set_field_existing(self):
        result = apply_patch(THREAD, [{"op": "set_field", "field": "status", "value": "resolved"}])
        self.assertTrue(result.ok)
        self.assertIn("status: resolved", result.content)
        self.assertIn("title: Test Thread", result.content)

    def test_set_field_new(self):
        result = apply_patch(THREAD, [{"op": "set_field", "field": "timestamp", "value": "2026-07-09T19:30:00Z"}])
        self.assertTrue(result.ok)
        # parse/dump may use single or double quotes
        self.assertRegex(result.content, r"timestamp:\s*['\"]2026-07-09T19:30:00Z['\"]")

    def test_set_field_quotes_invalid_date(self):
        # Invalid YYYY-MM-DD must survive as a quoted string (not a YAML timestamp).
        result = apply_patch(
            THREAD,
            [{"op": "set_field", "field": "effective_date", "value": "2017-00-00"}],
        )
        self.assertTrue(result.ok)
        self.assertRegex(result.content, r"effective_date:\s*['\"]2017-00-00['\"]")

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

    def test_replace_text_top_level_heading_alias(self):
        result = apply_patch(
            THREAD,
            [{
                "op": "replace_text",
                "find": "- [ ] Do thing",
                "replace": "- [x] Do thing",
                "heading": "## Next action",
            }],
        )
        self.assertTrue(result.ok)
        self.assertIn("- [x] Do thing", result.content)

    def test_replace_section_target_alias(self):
        result = apply_patch(
            THREAD,
            [{"op": "replace_section", "target": "## Summary", "text": "Via target."}],
        )
        self.assertTrue(result.ok)
        self.assertIn("Via target.", result.content)
        self.assertNotIn("Short summary.", result.content)

    def test_batch_thread_upsert(self):
        ops = [
            {"op": "append", "heading": "## History", "text": "- 2026-07-09 — done.\n"},
            {"op": "set_field", "field": "last_checked", "value": "2026-07-09 15:30"},
            {"op": "set_field", "field": "status", "value": "resolved"},
        ]
        result = apply_patch(THREAD, ops)
        self.assertTrue(result.ok)
        self.assertEqual(result.applied, 3)
        self.assertRegex(result.content, r"last_checked:\s*['\"]?2026-07-09 15:30['\"]?")
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
        # Partial success: applied ops persist in content, but ok is false so callers notice.
        self.assertFalse(result.ok)
        self.assertEqual(result.applied, 1)
        self.assertIn("status: resolved", result.content)
        self.assertTrue(any(r.get("status") == "error" for r in result.results))

    def test_replace_section(self):
        result = apply_patch(
            THREAD,
            [{"op": "replace_section", "heading": "## Summary", "text": "Updated summary."}],
        )
        self.assertTrue(result.ok)
        self.assertIn("Updated summary.", result.content)
        self.assertNotIn("Short summary.", result.content)

    def test_replace_section_strips_duplicate_heading(self):
        result = apply_patch(
            THREAD,
            [{
                "op": "replace_section",
                "heading": "## Summary",
                "text": "## Summary\n\nUpdated summary.\n",
            }],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.content.count("## Summary"), 1)
        self.assertIn("Updated summary.", result.content)
        self.assertNotIn("Short summary.", result.content)

    def test_heading_not_found_suggestions(self):
        lines = THREAD.split("\n")
        with self.assertRaises(PatchError) as ctx:
            find_section(lines, "## Histroy")
        self.assertEqual(ctx.exception.code, "anchor_not_found")
        self.assertTrue(ctx.exception.suggestions)

    def test_set_field_todos_by_id(self):
        src = """---
title: Plan
okf_type: Plan
todos:
  - id: skypad-resolver
    content: Do thing
    status: pending
  - id: other
    content: More
    status: pending
---

# Body
"""
        result = apply_patch(
            src,
            [{"op": "set_field", "field": "todos[id=skypad-resolver].status", "value": "completed"}],
        )
        self.assertTrue(result.ok)
        self.assertIn("status: completed", result.content)
        self.assertIn("id: other", result.content)
        fm = result.content.split("---")[1]
        self.assertIn("skypad-resolver", fm)
        self.assertEqual(fm.count("status: pending"), 1)
        self.assertEqual(fm.count("status: completed"), 1)

    def test_set_field_todos_by_index_and_replace_list(self):
        src = """---
todos:
  - id: a
    status: pending
---

# X
"""
        r1 = apply_patch(src, [{"op": "set_field", "field": "todos.0.status", "value": "done"}])
        self.assertTrue(r1.ok)
        self.assertIn("status: done", r1.content)
        r2 = apply_patch(
            src,
            [
                {
                    "op": "set_field",
                    "field": "todos",
                    "value": [{"id": "n", "content": "new", "status": "pending"}],
                }
            ],
        )
        self.assertTrue(r2.ok)
        self.assertIn("id: n", r2.content)
        self.assertNotIn("id: a", r2.content)

    def test_delete_field_todos_no_orphans(self):
        src = """---
title: Plan
todos:
  - id: a
    status: pending
status: active
---

# Body stays
"""
        result = apply_patch(src, [{"op": "delete_field", "field": "todos"}])
        self.assertTrue(result.ok)
        self.assertNotIn("todos:", result.content)
        self.assertNotIn("id: a", result.content)
        self.assertIn("status: active", result.content)
        self.assertIn("# Body stays", result.content)


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
