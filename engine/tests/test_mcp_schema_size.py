"""MCP list_tools schema size ceilings — prevent instruction bloat regression."""

from __future__ import annotations

import json
import unittest

from apo_engine.mcp_instructions import MCP_INSTRUCTIONS

from test_patch_note_schema import _list_tools_lean, _tool_params

# Baseline after 0.23.0 patch_note slim + patch_table split (2026-08-26).
# Raised 2026-08-28: vault (and the since-retired scratchpad) became discriminated unions on `action`
# (mcp_action_schemas.py) instead of one flat param bag with "lint only:"/
# "clone only:" prose — each action now gets only the params it uses, which
# JSON Schema's `oneOf` renders as N fully-inlined variant objects (no $ref
# dedup across siblings, confirmed empirically). That structural cost floors
# around +500 chars even with every optional description trimmed to the
# point of terseness; the alternative (silently-ignored fields on the wrong
# action) was worse. See mcp_action_schemas.py's own docstring.
# Raised 2026-08-31: graph_neighbors tool + search_notes explain= param.
# Raised 2026-09-26: vault action okf_dry_run — was implemented in ops.vault_op
# and tested at the okf module level, but unreachable from any user-facing
# surface (missing from this union, no CLI path, no RPC route). Closing a
# real capability gap costs one more discriminated-union variant (+284 chars).
TOTAL_TOOLS_CHAR_CEILING = 31_900
PATCH_NOTE_CHAR_CEILING = 6_000
PATCH_TABLE_CHAR_CEILING = 5_500
MCP_INSTRUCTIONS_CHAR_CEILING = 900


def _tool_chars(tool) -> int:
    params = getattr(tool, "parameters", None) or tool.model_dump().get("parameters")
    desc = tool.description or ""
    params_json = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    return len(tool.name) + len(desc) + len(params_json)


class McpSchemaSizeTest(unittest.TestCase):
    def test_total_list_tools_char_budget(self):
        _mod, tools = _list_tools_lean(collection="mcp_schema_size_test")
        total = sum(_tool_chars(t) for t in tools)
        self.assertLessEqual(
            total,
            TOTAL_TOOLS_CHAR_CEILING,
            msg=f"list_tools total {total} chars exceeds {TOTAL_TOOLS_CHAR_CEILING}",
        )

    def test_patch_note_slimmer_than_pre_023_baseline(self):
        _mod, tools = _list_tools_lean(collection="mcp_schema_size_patch_test")
        by_name = {t.name: t for t in tools}
        patch_chars = _tool_chars(by_name["patch_note"])
        self.assertLessEqual(patch_chars, PATCH_NOTE_CHAR_CEILING)
        self.assertLess(patch_chars, 12_000)

    def test_patch_table_bounded(self):
        _mod, tools = _list_tools_lean(collection="mcp_schema_size_table_test")
        by_name = {t.name: t for t in tools}
        self.assertLessEqual(_tool_chars(by_name["patch_table"]), PATCH_TABLE_CHAR_CEILING)

    def test_mcp_instructions_handshake_ceiling(self):
        self.assertLessEqual(len(MCP_INSTRUCTIONS), MCP_INSTRUCTIONS_CHAR_CEILING)

    def test_patch_note_no_items_param(self):
        _mod, tools = _list_tools_lean(collection="mcp_schema_size_items_test")
        by_name = {t.name: t for t in tools}
        self.assertNotIn("items", _tool_params(by_name["patch_note"]))


if __name__ == "__main__":
    unittest.main()
