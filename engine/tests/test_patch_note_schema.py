"""patch_note MCP schema exposes a discriminated union of typed ops."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"
_SRC = _ENGINE / "src"


def _list_tools_lean(*, collection: str):
    with tempfile.TemporaryDirectory(prefix="apo-mcp-schema-") as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        os.environ["APO_MCP_LEAN"] = "1"
        os.environ["APO_NOTES_ROOT"] = str(vault)
        os.environ["APO_INDEX"] = str(Path(tmp) / "index.db")
        os.environ["APO_COLLECTION"] = collection
        # Prefer this worktree's apo_engine over a stale editable install.
        import sys

        src = str(_SRC)
        if src not in sys.path:
            sys.path.insert(0, src)
        # Drop cached apo_engine modules so patch_ops from this tree loads.
        for name in list(sys.modules):
            if name == "apo_engine" or name.startswith("apo_engine."):
                del sys.modules[name]
        spec = importlib.util.spec_from_file_location(f"apo_mcp_{collection}", _SERVER)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        tools = asyncio.run(mod.mcp.list_tools())
        return mod, tools


def _patch_note_tool():
    _mod, tools = _list_tools_lean(collection="patch_schema_test")
    for t in tools:
        if t.name == "patch_note":
            return t
    raise AssertionError("patch_note not registered")


def _ops_schema(tool) -> dict:
    schema = getattr(tool, "parameters", None) or tool.model_dump().get("parameters")
    assert isinstance(schema, dict)
    return schema["properties"]["ops"]


def _tool_params(tool) -> dict:
    schema = getattr(tool, "parameters", None) or tool.model_dump().get("parameters")
    assert isinstance(schema, dict)
    return schema["properties"]


class PatchNoteSchemaTest(unittest.TestCase):
    def test_ops_description_names_contract(self):
        ops = _ops_schema(_patch_note_tool())
        desc = ops.get("description") or ""
        for token in (
            "discriminated",
            "field",
            "find",
            "replace",
            "key/old/new",
            "set_field",
            "replace_text",
            "append_note",
            "scope",
            "Aliases frozen",
        ):
            self.assertIn(token, desc, msg=f"ops description missing {token!r}: {desc!r}")
        # Prefer compact key map over shipping full JSON examples in schema.
        self.assertNotIn('"op":"set_field"', desc)

    def test_tool_descriptions_prefer_canonical_routing(self):
        mod, tools = _list_tools_lean(collection="patch_desc_test")
        by_name = {t.name: t for t in tools}

        self.assertIn("prefer", (by_name["append_note"].description or "").lower())
        self.assertIn("archive", (by_name["move_note"].description or "").lower())
        self.assertNotIn("delete_note", by_name)

        write_params = _tool_params(by_name["write_note"])
        self.assertNotIn("index", write_params)
        self.assertNotIn("append", write_params)
        self.assertIn("mtime", (write_params["expected_mtime"].get("description") or "").lower())

        move_params = _tool_params(by_name["move_note"])
        self.assertIn("expected_mtime", move_params)
        self.assertNotIn("index", move_params)

        search_params = _tool_params(by_name["search_notes"])
        self.assertIn("Prefer", search_params["limit"].get("description") or "")
        self.assertIn("Alias", search_params["top_k"].get("description") or "")

        filter_params = _tool_params(by_name["filter_notes"])
        self.assertIn("canonical", (filter_params["where"].get("description") or "").lower())
        self.assertIn("Alias", filter_params["filters"].get("description") or "")
        self.assertIn("fields", filter_params)

        instr = getattr(mod.mcp, "instructions", None) or ""
        self.assertIn("append_note", instr)
        self.assertIn("note://", instr)
        self.assertIn("Lean desk is default", instr)
        self.assertIn("fields=", instr)
        self.assertIn("parallel", instr.lower())
        self.assertIn("history", instr)
        self.assertNotIn("recent_activity", instr)

    def test_ops_items_are_typed_oneof(self):
        tool = _patch_note_tool()
        ops = _ops_schema(tool)
        items = ops.get("items") or {}
        variants = items.get("oneOf") or items.get("anyOf")
        self.assertIsInstance(variants, list, msg=f"expected oneOf/anyOf, got: {json.dumps(items)[:800]}")
        self.assertGreaterEqual(len(variants), 6)

        op_names: set[str] = set()
        for v in variants:
            props = v.get("properties") or {}
            op_schema = props.get("op") or {}
            if "const" in op_schema:
                op_names.add(op_schema["const"])
            elif "enum" in op_schema:
                op_names.update(op_schema["enum"])
            self.assertNotEqual(v.get("additionalProperties"), True)

        expected = {
            "set_field",
            "delete_field",
            "replace_text",
            "replace_section",
            "append",
            "prepend",
            "append_eof",
        }
        self.assertTrue(
            expected <= op_names,
            msg=f"missing op variants: {expected - op_names}; saw {op_names}",
        )

    def test_set_field_requires_field(self):
        from apo_engine.patch_ops import SetFieldOp, ops_to_dicts
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            SetFieldOp.model_validate({"op": "set_field"})  # missing field
        with self.assertRaises(ValidationError):
            SetFieldOp.model_validate({"op": "set_field", "field": "status", "key": "nope"})
        dumped = ops_to_dicts([SetFieldOp(op="set_field", field="status", value="active")])
        self.assertEqual(dumped[0]["field"], "status")
        self.assertNotIn("key", dumped[0])

    def test_replace_text_heading_alias_normalizes_to_scope(self):
        from apo_engine.patch_ops import ReplaceTextOp, ops_to_dicts
        from pydantic import ValidationError

        dumped = ops_to_dicts([
            ReplaceTextOp.model_validate({
                "op": "replace_text",
                "find": "- [ ] x",
                "replace": "- [x] x",
                "heading": "## Next action",
            })
        ])
        self.assertEqual(dumped[0]["scope"], {"heading": "## Next action"})
        self.assertNotIn("heading", dumped[0])

        with self.assertRaises(ValidationError):
            ReplaceTextOp.model_validate({
                "op": "replace_text",
                "find": "a",
                "heading": "## A",
                "scope": {"heading": "## B"},
            })

    def test_replace_section_target_alias(self):
        from apo_engine.patch_ops import ReplaceSectionOp, ops_to_dicts

        dumped = ops_to_dicts([
            ReplaceSectionOp.model_validate({
                "op": "replace_section",
                "target": "## Summary",
                "text": "New",
            })
        ])
        self.assertEqual(dumped[0]["heading"], "## Summary")
        self.assertNotIn("target", dumped[0])


if __name__ == "__main__":
    unittest.main()
