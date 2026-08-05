"""Wire-compat aliases on patch_note ops (telemetry burn-down)."""

from __future__ import annotations

import unittest

from pydantic import TypeAdapter

from apo_engine.patch_ops import PatchOp


class PatchOpAliasTest(unittest.TestCase):
    def test_set_field_path_alias(self):
        op = TypeAdapter(PatchOp).validate_python(
            {"op": "set_field", "path": "status", "value": "active"}
        )
        self.assertEqual(op.field, "status")
        self.assertEqual(op.value, "active")

    def test_replace_text_old_new_aliases(self):
        op = TypeAdapter(PatchOp).validate_python(
            {
                "op": "replace_text",
                "old_text": "foo",
                "new_text": "bar",
                "heading": "History",
            }
        )
        self.assertEqual(op.find, "foo")
        self.assertEqual(op.replace, "bar")
        self.assertEqual(op.scope.heading, "History")


if __name__ == "__main__":
    unittest.main()
