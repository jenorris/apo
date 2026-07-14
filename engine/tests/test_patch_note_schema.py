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


def _patch_note_tool():
    with tempfile.TemporaryDirectory(prefix="apo-patch-schema-") as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        os.environ["APO_MCP_LEAN"] = "1"
        os.environ["APO_NOTES_ROOT"] = str(vault)
        os.environ["APO_INDEX"] = str(Path(tmp) / "index.db")
        os.environ["APO_COLLECTION"] = "patch_schema_test"
        # Prefer this worktree's apo_engine over a stale editable install.
        import sys

        src = str(_SRC)
        if src not in sys.path:
            sys.path.insert(0, src)
        # Drop cached apo_engine modules so patch_ops from this tree loads.
        for name in list(sys.modules):
            if name == "apo_engine" or name.startswith("apo_engine."):
                del sys.modules[name]
        spec = importlib.util.spec_from_file_location("apo_mcp_patch_schema", _SERVER)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        tools = asyncio.run(mod.mcp.list_tools())
        for t in tools:
            if t.name == "patch_note":
                return t
        raise AssertionError("patch_note not registered")


def _ops_schema(tool) -> dict:
    schema = getattr(tool, "parameters", None) or tool.model_dump().get("parameters")
    assert isinstance(schema, dict)
    return schema["properties"]["ops"]


class PatchNoteSchemaTest(unittest.TestCase):
    def test_ops_description_names_contract(self):
        ops = _ops_schema(_patch_note_tool())
        desc = ops.get("description") or ""
        for token in ("discriminated", "field", "find", "replace", "key/old/new"):
            self.assertIn(token, desc, msg=f"ops description missing {token!r}: {desc!r}")

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


if __name__ == "__main__":
    unittest.main()
