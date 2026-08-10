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
        os.environ["APO_NOTES_ROOT"] = str(vault)
        os.environ["APO_INDEX"] = str(Path(tmp) / "index.db")
        os.environ["APO_COLLECTION"] = collection
        os.environ.pop("APO_MCP_LEAN", None)
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
    ops = schema["properties"]["ops"]
    # Optional list → anyOf [null, array] (or similar); unwrap to the array schema.
    if ops.get("type") == "array" or ops.get("items"):
        return ops
    for alt in ops.get("anyOf") or ops.get("oneOf") or []:
        if isinstance(alt, dict) and (alt.get("type") == "array" or alt.get("items")):
            # Preserve description from parent when present
            if ops.get("description") and not alt.get("description"):
                alt = {**alt, "description": ops["description"]}
            return alt
    return ops


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
        self.assertIn("place", (by_name["patch_note"].description or "").lower())
        self.assertNotIn("place_note", by_name)
        self.assertNotIn("delete_note", by_name)
        self.assertNotIn("move_note", by_name)
        self.assertNotIn("send_note", by_name)
        self.assertNotIn("telemetry", by_name)
        self.assertNotIn("expand_section", by_name)

        write_params = _tool_params(by_name["write_note"])
        self.assertNotIn("index", write_params)
        self.assertNotIn("append", write_params)
        self.assertIn("content", write_params)
        self.assertNotIn("text", write_params)
        self.assertNotIn("body", write_params)
        write_desc = (by_name["write_note"].description or "").lower()
        self.assertIn("content", write_desc)

        append_params = _tool_params(by_name["append_note"])
        self.assertIn("text", append_params)
        self.assertNotIn("content", append_params)
        self.assertNotIn("body", append_params)
        append_desc = (by_name["append_note"].description or "").lower()
        self.assertIn("text", append_desc)

        read_params = _tool_params(by_name["read_note"])
        self.assertIn("chunk_hash", read_params)
        self.assertIn("force", read_params)

        search_params = _tool_params(by_name["search_notes"])
        self.assertIn("limit", search_params)
        self.assertIn("folders", search_params)
        self.assertNotIn("top_k", search_params)

        filter_params = _tool_params(by_name["filter_notes"])
        self.assertIn("where", filter_params)
        self.assertNotIn("filters", filter_params)
        self.assertIn("fields", filter_params)
        self.assertIn("sort", filter_params)
        self.assertIn("order", filter_params)

        vault_params = _tool_params(by_name["vault"])
        self.assertIn("action", vault_params)

        instr = getattr(mod.mcp, "instructions", None) or ""
        self.assertIn("append_note", instr)
        self.assertIn("chunk_hash=", instr)
        self.assertIn("apo_admin", instr)
        self.assertNotIn("expand_section", instr)
        self.assertNotIn("telemetry", instr.lower())

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
            "place",
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
