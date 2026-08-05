"""APO_MCP_LEAN hides admin tools from FastMCP list_tools (default on)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"
_ADMIN = frozenset({
    "reload_config",
    "memory_status",
    "reindex_deferred",
    "reindex",
    "delete_note",
    "tool_stats",
    "git_sync",
})


def _list_tool_names(*, lean: bool | None) -> set[str]:
    """Import server in a subprocess so lean env is fixed before registration.

    lean=True → APO_MCP_LEAN=1; False → 0; None → unset (default lean).
    """
    with tempfile.TemporaryDirectory(prefix="apo-lean-") as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        env = os.environ.copy()
        if lean is None:
            env.pop("APO_MCP_LEAN", None)
        else:
            env["APO_MCP_LEAN"] = "1" if lean else "0"
        env["APO_NOTES_ROOT"] = str(vault)
        env["APO_INDEX"] = str(Path(tmp) / "index.db")
        env["APO_COLLECTION"] = "lean_test"
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
        self.assertIn("delete_note", names)
        self.assertIn("place_note", names)
        self.assertIn("git_sync", names)

    def test_lean_mode_hides_admin(self):
        names = _list_tool_names(lean=True)
        self.assertFalse(names & _ADMIN, msg=f"admin still listed: {names & _ADMIN}")
        self.assertIn("search_notes", names)
        self.assertIn("filter_notes", names)
        self.assertIn("append_note", names)
        self.assertIn("place_note", names)
        self.assertIn("history", names)
        self.assertIn("patch_note", names)
        self.assertNotIn("patch_notes", names)
        self.assertNotIn("move_note", names)
        self.assertNotIn("send_note", names)
        self.assertNotIn("git_sync", names)
        self.assertNotIn("recent_activity", names)
        self.assertNotIn("delete_note", names)
        self.assertIn("vault", names)
        self.assertIn("session_stats", names)
        self.assertIn("active_session", names)
        self.assertEqual(len(names), 13)

    def test_full_mode_tool_count(self):
        names = _list_tool_names(lean=False)
        self.assertEqual(len(names), 20)
        self.assertIn("vault", names)
        self.assertTrue(_ADMIN <= names)
        self.assertNotIn("patch_notes", names)
        self.assertNotIn("move_note", names)
        self.assertNotIn("send_note", names)

    def test_lean_is_default_when_unset(self):
        names = _list_tool_names(lean=None)
        self.assertFalse(names & _ADMIN, msg=f"admin listed with unset lean: {names & _ADMIN}")
        self.assertEqual(names, _list_tool_names(lean=True))


if __name__ == "__main__":
    unittest.main()
