"""Unit tests for ``ranking._generic_table_header_demotion`` — a ``table_header``
chunk whose column list is a generic 2-column key/value shape (``Field, Value`` /
``Key, Value`` / ``Property, Value``) carries no differentiating content beyond
what the section chunk's own ``[table: N rows — cols]`` marker already repeats
(``core._collapse_full_tables``), so it reads as a "super-matcher" for almost
any query mentioning one of those generic column names. See
``table_markdown.header_flatten_text`` for the ``"{breadcrumb} — Columns: {cols}"``
shape this parses.
"""

from __future__ import annotations

import unittest

from apo_engine import ranking


class GenericTableHeaderDemotionTest(unittest.TestCase):
    def test_field_value_is_generic(self):
        self.assertTrue(
            ranking._is_generic_table_header_text("Some Note > Metadata — Columns: Field, Value")
        )

    def test_key_value_is_generic(self):
        self.assertTrue(ranking._is_generic_table_header_text("Columns: Key, Value"))

    def test_property_value_is_generic(self):
        self.assertTrue(
            ranking._is_generic_table_header_text("Widget > Spec — Columns: Property, Value")
        )

    def test_order_and_case_insensitive(self):
        self.assertTrue(ranking._is_generic_table_header_text("Columns: value, FIELD"))

    def test_differentiated_columns_are_not_generic(self):
        self.assertFalse(
            ranking._is_generic_table_header_text(
                "Wizard > Class Features — Columns: Level, 1st, 2nd, 3rd, 4th"
            )
        )

    def test_non_header_text_is_not_generic(self):
        self.assertFalse(ranking._is_generic_table_header_text("just some prose, no columns"))

    def test_demotion_only_applies_to_table_header_chunk_kind(self):
        generic_text = "Columns: Field, Value"
        self.assertLess(ranking._generic_table_header_demotion("table_header", generic_text), 1.0)
        self.assertEqual(ranking._generic_table_header_demotion("section", generic_text), 1.0)
        self.assertEqual(ranking._generic_table_header_demotion("table_row", generic_text), 1.0)

    def test_path_retrieval_boost_demotes_generic_header(self):
        generic_mult, generic_detail = ranking._path_retrieval_boost(
            "notes/anything.md", "table_header", "value field lookup", text="Columns: Field, Value"
        )
        specific_mult, specific_detail = ranking._path_retrieval_boost(
            "notes/anything.md",
            "table_header",
            "value field lookup",
            text="Notes > Spec — Columns: Torque, Bolt Size, Material",
        )
        self.assertLess(generic_detail["generic_table_header_demotion"], 1.0)
        self.assertEqual(specific_detail["generic_table_header_demotion"], 1.0)
        self.assertLess(generic_mult, specific_mult)


if __name__ == "__main__":
    unittest.main()
