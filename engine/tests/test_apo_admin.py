"""apo_admin meta-tool replaces top-level admin MCP tools."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from apo_engine import apo_admin

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"
_SRC = _ENGINE / "src"

_ADMIN_CAPABILITIES = frozenset({
    "reload_config",
    "memory_status",
    "telemetry",
    "reindex_deferred",
    "reindex",
    "delete_note",
    "git_sync",
})

_TOP_LEVEL = frozenset({
    "append_note",
    "apo_admin",
    "backlinks",
    "expand_chunk",
    "expand_section",
    "filter_notes",
    "history",
    "patch_note",
    "place_note",
    "read_note",
    "search_notes",
    "telemetry",
    "vault",
    "write_note",
})


def _list_tool_names() -> set[str]:
    with tempfile.TemporaryDirectory(prefix="apo-admin-") as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        env = os.environ.copy()
        env.pop("APO_MCP_LEAN", None)
        env["APO_NOTES_ROOT"] = str(vault)
        env["APO_INDEX"] = str(Path(tmp) / "index.db")
        env["APO_COLLECTION"] = "admin_test"
        script = r"""
import asyncio, importlib.util, sys
from pathlib import Path
src = Path(sys.argv[2])
if str(src) not in sys.path:
    sys.path.insert(0, str(src))
spec = importlib.util.spec_from_file_location("apo_mcp_admin", Path(sys.argv[1]))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

async def main():
    tools = await mod.mcp.list_tools()
    print("\n".join(sorted(t.name for t in tools)))

asyncio.run(main())
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, str(_SERVER), str(_SRC)],
            cwd=str(_ENGINE),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"list_tools failed rc={proc.returncode}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        return {line for line in proc.stdout.splitlines() if line.strip()}


class ApoAdminCatalogTest(unittest.TestCase):
    def test_admin_list_covers_capabilities(self):
        out = apo_admin.admin_list()
        self.assertTrue(out["ok"])
        names = {c["name"] for c in out["capabilities"]}
        self.assertEqual(names, _ADMIN_CAPABILITIES)

    def test_admin_describe_unknown(self):
        out = apo_admin.admin_describe("nope")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_name")

    def test_admin_invoke_requires_confirm_for_delete(self):
        out = apo_admin.admin_invoke(
            "delete_note",
            parameters={"path": "x.md"},
            confirm=False,
            handlers={"delete_note": lambda *_a, **_k: {"ok": True}},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "confirm_required")


class ApoAdminMcpSurfaceTest(unittest.TestCase):
    def test_tool_count_and_names(self):
        names = _list_tool_names()
        self.assertEqual(names, _TOP_LEVEL)
        self.assertEqual(len(names), 14)
        self.assertIn("apo_admin", names)
        self.assertIn("telemetry", names)
        self.assertEqual(names & _ADMIN_CAPABILITIES, {"telemetry"})

    def test_apo_admin_registered(self):
        names = _list_tool_names()
        self.assertNotIn("delete_note", names)
        self.assertNotIn("memory_status", names)
        self.assertNotIn("git_sync", names)


if __name__ == "__main__":
    unittest.main()
