"""scratchpad MCP schema — discriminated union on `action`, PatchOp union creep guard."""

from __future__ import annotations

import json
import unittest

from apo_engine.mcp_instructions import MCP_INSTRUCTIONS

from test_patch_note_schema import _list_tools_lean, _tool_params


SCRATCHPAD_TOOL_CHAR_CEILING = 3000
SCRATCHPAD_OP_VARIANTS = 2
SCRATCHPAD_BLURB_CHAR_CEILING = 220

EXPECTED_ACTION_FIELDS = {
    "create": {"action", "format", "content"},
    "read": {"action", "session_id"},
    "patch": {"action", "session_id", "ops"},
    "commit": {"action", "session_id", "vault", "schema_path", "schema_type", "destination_path"},
    "discard": {"action", "session_id"},
}


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


def _action_variants(tool) -> dict[str, dict]:
    """Map action name -> that action's variant schema, from the `request` oneOf."""
    props = _tool_params(tool)
    variants = props["request"]["oneOf"]
    out = {}
    for v in variants:
        action = (v.get("properties") or {}).get("action", {}).get("const")
        assert action is not None, f"variant missing action const: {v}"
        out[action] = v
    return out


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
        variants = _action_variants(_scratchpad_tool())
        ops = variants["patch"]["properties"]["ops"]
        op_variants = ops.get("items", {}).get("oneOf") or ops.get("items", {}).get("anyOf") or []
        op_names = set()
        for v in op_variants:
            props = v.get("properties") or {}
            op_schema = props.get("op") or {}
            if "const" in op_schema:
                op_names.add(op_schema["const"])
        self.assertEqual(len(op_variants), SCRATCHPAD_OP_VARIANTS)
        self.assertEqual(op_names, {"set_field", "delete_field"})

    def test_each_action_variant_has_only_its_own_fields(self):
        """The whole point of the discriminated union: no action sees another
        action's params (e.g. `create` can't accept `session_id`, `patch` can't
        accept `format`) — pydantic/FastMCP reject them, this pins the schema
        that makes that true."""
        variants = _action_variants(_scratchpad_tool())
        self.assertEqual(set(variants), set(EXPECTED_ACTION_FIELDS))
        for action, expected_fields in EXPECTED_ACTION_FIELDS.items():
            with self.subTest(action=action):
                variant = variants[action]
                self.assertEqual(set(variant["properties"].keys()), expected_fields)
                self.assertFalse(variant.get("additionalProperties", True))

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
