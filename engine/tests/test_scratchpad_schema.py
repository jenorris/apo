"""scratchpad MCP schema size ceilings — prevent PatchOp union creep."""

from __future__ import annotations

import json
import unittest

from apo_engine.mcp_instructions import MCP_INSTRUCTIONS

from test_patch_note_schema import _list_tools_lean, _ops_schema, _tool_params


SCRATCHPAD_TOOL_CHAR_CEILING = 3000
SCRATCHPAD_PROPERTY_CEILING = 10
SCRATCHPAD_OP_VARIANTS = 2
SCRATCHPAD_BLURB_CHAR_CEILING = 220


def _scratchpad_tool():
    _mod, tools = _list_tools_lean(collection="scratchpad_schema_test")
    for t in tools:
        if t.name == "scratchpad":
            return t
    raise AssertionError("scratchpad not registered")


def _tool_chars(tool) -> int:
    params = getattr(tool, "parameters", None) or tool.model_dump().get("parameters")
    desc = tool.description or ""
    params_json = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    return len(tool.name) + len(desc) + len(params_json)


def _scratchpad_blurb_chars() -> int:
    idx = MCP_INSTRUCTIONS.find("scratchpad(")
    end = MCP_INSTRUCTIONS.find("Routing:")
    if idx < 0:
        return 0
    return len(MCP_INSTRUCTIONS[idx:end])


class ScratchpadSchemaTest(unittest.TestCase):
    def test_scratchpad_tool_size_ceiling(self):
        tool = _scratchpad_tool()
        chars = _tool_chars(tool)
        self.assertLessEqual(
            chars,
            SCRATCHPAD_TOOL_CHAR_CEILING,
            msg=f"scratchpad tool {chars} chars exceeds ceiling {SCRATCHPAD_TOOL_CHAR_CEILING}",
        )

    def test_scratchpad_ops_are_two_variant_union(self):
        ops = _ops_schema(_scratchpad_tool())
        items = ops.get("items") or {}
        variants = items.get("oneOf") or items.get("anyOf") or []
        op_names = set()
        for v in variants:
            props = v.get("properties") or {}
            op_schema = props.get("op") or {}
            if "const" in op_schema:
                op_names.add(op_schema["const"])
        self.assertEqual(len(variants), SCRATCHPAD_OP_VARIANTS)
        self.assertEqual(op_names, {"set_field", "delete_field"})

    def test_scratchpad_property_count(self):
        props = _tool_params(_scratchpad_tool())
        self.assertLessEqual(len(props), SCRATCHPAD_PROPERTY_CEILING)
        expected = {
            "action",
            "session_id",
            "format",
            "content",
            "ops",
            "vault",
            "destination_path",
            "schema_path",
            "schema_type",
        }
        self.assertTrue(expected <= set(props.keys()))

    def test_no_scratchpad_param_on_sibling_tools(self):
        _mod, tools = _list_tools_lean(collection="scratchpad_sibling_test")
        by_name = {t.name: t for t in tools}
        for name in ("write_note", "append_note", "patch_note"):
            props = _tool_params(by_name[name])
            self.assertNotIn("scratchpad", props)

    def test_handshake_scratchpad_blurb_ceiling(self):
        blurb = _scratchpad_blurb_chars()
        self.assertLessEqual(
            blurb,
            SCRATCHPAD_BLURB_CHAR_CEILING,
            msg=f"scratchpad blurb {blurb} chars exceeds {SCRATCHPAD_BLURB_CHAR_CEILING}",
        )
