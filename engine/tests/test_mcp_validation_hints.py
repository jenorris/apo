"""MCP call_tool surfaces rewritten ValidationError hints (not raw pydantic)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"


def _load_server(vault: Path, tmp: Path):
    env_keys = {
        "APO_MCP_LEAN": "1",
        "APO_NOTES_ROOT": str(vault),
        "APO_INDEX": str(tmp / "index.db"),
        "APO_COLLECTION": "hint_test",
    }
    for k, v in env_keys.items():
        os.environ[k] = v
    # Fresh import each time — lean/env are read at module load.
    for name in list(sys_modules_apo()):
        pass
    spec = importlib.util.spec_from_file_location("apo_mcp_hints", _SERVER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sys_modules_apo():
    import sys

    return [k for k in sys.modules if k.startswith("apo_mcp")]


class McpValidationHintTest(unittest.TestCase):
    def test_patch_note_missing_op_is_rewritten(self):
        with tempfile.TemporaryDirectory(prefix="apo-hint-") as tmp_s:
            tmp = Path(tmp_s)
            vault = tmp / "vault"
            vault.mkdir()
            (vault / "n.md").write_text("---\ntitle: t\n---\n\n# Hi\n\nbody\n", encoding="utf-8")
            mod = _load_server(vault, tmp)

            async def run():
                try:
                    await mod.mcp.call_tool(
                        "patch_note",
                        {
                            "path": "n.md",
                            "ops": [{"field": "content", "old": "a", "value": "b"}],
                        },
                    )
                    return None
                except Exception as e:
                    return e

            exc = asyncio.run(run())
            self.assertIsNotNone(exc)
            text = str(exc)
            self.assertIn('missing required "op"', text)
            self.assertIn("replace_text", text)
            self.assertNotIn("union_tag_not_found", text)

    def test_read_note_snippet_chars_hint(self):
        with tempfile.TemporaryDirectory(prefix="apo-hint-") as tmp_s:
            tmp = Path(tmp_s)
            vault = tmp / "vault"
            vault.mkdir()
            (vault / "n.md").write_text("---\ntitle: t\n---\n\nx\n", encoding="utf-8")
            mod = _load_server(vault, tmp)

            async def run():
                try:
                    await mod.mcp.call_tool(
                        "read_note",
                        {"path": "n.md", "snippet_chars": 0},
                    )
                    return None
                except Exception as e:
                    return e

            exc = asyncio.run(run())
            self.assertIsNotNone(exc)
            text = str(exc)
            self.assertIn("snippet_chars", text)
            self.assertIn("max_chars", text)
            self.assertNotIn("unexpected_keyword_argument", text)


if __name__ == "__main__":
    unittest.main()
