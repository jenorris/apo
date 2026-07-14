"""patch_note MCP schema must name ops keys (field/find/replace) — not opaque additionalProperties."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"


def _patch_note_tool():
    with tempfile.TemporaryDirectory(prefix="apo-patch-schema-") as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        os.environ["APO_MCP_LEAN"] = "1"
        os.environ["APO_NOTES_ROOT"] = str(vault)
        os.environ["APO_INDEX"] = str(Path(tmp) / "index.db")
        os.environ["APO_COLLECTION"] = "patch_schema_test"
        spec = importlib.util.spec_from_file_location("apo_mcp_patch_schema", _SERVER)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        tools = asyncio.run(mod.mcp.list_tools())
        for t in tools:
            if t.name == "patch_note":
                return t
        raise AssertionError("patch_note not registered")


class PatchNoteSchemaTest(unittest.TestCase):
    def test_ops_description_names_required_keys(self):
        tool = _patch_note_tool()
        schema = getattr(tool, "parameters", None) or tool.model_dump().get("parameters")
        self.assertIsInstance(schema, dict)
        ops = schema["properties"]["ops"]
        desc = ops.get("description") or ""
        for token in ("op=", "field", "find", "replace", "heading", "text", "key/old/new"):
            self.assertIn(token, desc, msg=f"ops description missing {token!r}: {desc!r}")


if __name__ == "__main__":
    unittest.main()
