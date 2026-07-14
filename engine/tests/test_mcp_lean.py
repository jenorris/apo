"""APO_MCP_LEAN hides admin tools from FastMCP list_tools."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"
_ADMIN = frozenset({"reload_config", "memory_status", "reindex_deferred", "reindex"})


def _list_tool_names(*, lean: bool) -> set[str]:
    """Import server in a subprocess so lean env is fixed before registration."""
    with tempfile.TemporaryDirectory(prefix="apo-lean-") as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        env = os.environ.copy()
        env["APO_MCP_LEAN"] = "1" if lean else "0"
        env["APO_NOTES_ROOT"] = str(vault)
        env["APO_INDEX"] = str(Path(tmp) / "index.db")
        env["APO_COLLECTION"] = "lean_test"
        # Avoid inheriting a parent lean setting confused by empty-vs-unset
        script = r"""
import asyncio, importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("apo_mcp_lean", Path(sys.argv[1]))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

async def main():
    tools = await mod.mcp.list_tools()
    print("\n".join(sorted(t.name for t in tools)))

asyncio.run(main())
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, str(_SERVER)],
            cwd=str(_ENGINE),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"lean={lean} failed rc={proc.returncode}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        return {line for line in proc.stdout.splitlines() if line.strip()}


class LeanModeTest(unittest.TestCase):
    def test_full_mode_includes_admin(self):
        names = _list_tool_names(lean=False)
        self.assertTrue(_ADMIN <= names, msg=f"missing admin in full: {_ADMIN - names}")
        self.assertIn("search_notes", names)
        self.assertIn("append_note", names)

    def test_lean_mode_hides_admin(self):
        names = _list_tool_names(lean=True)
        self.assertFalse(names & _ADMIN, msg=f"admin still listed: {names & _ADMIN}")
        self.assertIn("search_notes", names)
        self.assertIn("filter_notes", names)
        self.assertIn("append_note", names)
        self.assertNotIn("list_directory", names)
        self.assertEqual(len(names), len(_list_tool_names(lean=False)) - len(_ADMIN))


if __name__ == "__main__":
    unittest.main()
