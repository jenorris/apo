"""patch_table MCP schema — table ops split from patch_note."""

from __future__ import annotations

import json
import unittest

from test_patch_note_schema import _list_tools_lean, _ops_schema, _tool_params

PATCH_TABLE_TOOL_CHAR_CEILING = 5400
PATCH_TABLE_OP_VARIANTS = 6


def _patch_table_tool():
    _mod, tools = _list_tools_lean(collection="patch_table_schema_test")
    for t in tools:
        if t.name == "patch_table":
            return t
    raise AssertionError("patch_table not registered")


def _tool_chars(tool) -> int:
    params = getattr(tool, "parameters", None) or tool.model_dump().get("parameters")
    desc = tool.description or ""
    params_json = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    return len(tool.name) + len(desc) + len(params_json)


class PatchTableSchemaTest(unittest.TestCase):
    def test_patch_table_tool_size_ceiling(self):
        tool = _patch_table_tool()
        chars = _tool_chars(tool)
        self.assertLessEqual(chars, PATCH_TABLE_TOOL_CHAR_CEILING)

    def test_table_ops_are_six_variant_union(self):
        ops = _ops_schema(_patch_table_tool())
        items = ops.get("items") or {}
        variants = items.get("oneOf") or items.get("anyOf") or []
        op_names = set()
        for v in variants:
            props = v.get("properties") or {}
            op_schema = props.get("op") or {}
            if "const" in op_schema:
                op_names.add(op_schema["const"])
        self.assertEqual(len(variants), PATCH_TABLE_OP_VARIANTS)
        self.assertEqual(
            op_names,
            {
                "update_cell",
                "update_row",
                "append_row",
                "delete_row",
                "replace_table",
                "alter_table_schema",
            },
        )

    def test_patch_note_excludes_table_ops(self):
        _mod, tools = _list_tools_lean(collection="patch_table_note_split_test")
        by_name = {t.name: t for t in tools}
        patch_ops = _ops_schema(by_name["patch_note"])
        variants = (patch_ops.get("items") or {}).get("oneOf") or []
        table_ops = {
            "update_cell",
            "update_row",
            "append_row",
            "delete_row",
            "replace_table",
            "alter_table_schema",
        }
        for v in variants:
            op = (v.get("properties") or {}).get("op", {}).get("const")
            self.assertNotIn(op, table_ops)


if __name__ == "__main__":
    unittest.main()
