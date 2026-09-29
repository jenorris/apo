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
_SRC = _ENGINE / "src"
_SERVER = _SRC / "apo_engine" / "mcp" / "server.py"

_ADMIN_CAPABILITIES = frozenset({
    "reload_config",
    "memory_status",
    "index_health",
    "reindex",
    "delete_note",
    "git_sync",
    "list_refs",
})

_TOP_LEVEL = frozenset({
    "append_note",
    "apo_admin",
    "backlinks",
    "filter_notes",
    "graph_neighbors",
    "history",
    "patch_note",
    "patch_table",
    "read_note",
    "search_notes",
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

    def test_admin_invoke_requires_confirm_for_git_sync_rebase(self):
        out = apo_admin.admin_invoke(
            "git_sync",
            parameters={"action": "rebase"},
            confirm=False,
            handlers={"git_sync": lambda *_a, **_k: {"ok": True}},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "confirm_required")

    def test_admin_invoke_allows_git_sync_status_without_confirm(self):
        out = apo_admin.admin_invoke(
            "git_sync",
            parameters={"action": "status"},
            confirm=False,
            handlers={"git_sync": lambda *_a, **_k: {"ok": True}},
        )
        self.assertTrue(out["ok"])

    def test_admin_invoke_requires_confirm_for_reindex_rebuild(self):
        """mode=rebuild can run inline (a direct index.db write) when no watcher
        is live — gate on the mode itself, not just force=true, so this can't
        slip through unconfirmed via the no-watcher path."""
        out = apo_admin.admin_invoke(
            "reindex",
            parameters={"mode": "rebuild"},
            confirm=False,
            handlers={"reindex": lambda *_a, **_k: {"ok": True}},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "confirm_required")

    def test_admin_invoke_allows_reindex_flush_without_confirm(self):
        out = apo_admin.admin_invoke(
            "reindex",
            parameters={"mode": "flush"},
            confirm=False,
            handlers={"reindex": lambda *_a, **_k: {"ok": True}},
        )
        self.assertTrue(out["ok"])

    def test_resolve_action_infers_invoke_for_delete_with_confirm(self):
        # Omitted action + name + parameters + confirm=true must resolve to
        # invoke, not silently fall back to the list-capabilities default.
        act = apo_admin.resolve_action(
            None, "delete_note", {"path": "x.md"}, True
        )
        self.assertEqual(act, "invoke")

    def test_resolve_action_infers_invoke_for_name_and_parameters_only(self):
        act = apo_admin.resolve_action(
            None, "reindex", {"mode": "flush"}, False
        )
        self.assertEqual(act, "invoke")

    def test_resolve_action_infers_invoke_for_name_and_confirm_only(self):
        act = apo_admin.resolve_action(None, "delete_note", None, True)
        self.assertEqual(act, "invoke")

    def test_resolve_action_no_arguments_defaults_to_list(self):
        act = apo_admin.resolve_action(None, None, None, False)
        self.assertEqual(act, "list")

    def test_resolve_action_bare_name_still_defaults_to_list(self):
        # Narrow fix: a bare name with no parameters/confirm signal is left
        # exactly as before (still "list") to avoid widening the inference
        # beyond the reported invoke-with-confirm case.
        act = apo_admin.resolve_action(None, "delete_note", None, False)
        self.assertEqual(act, "list")

    def test_resolve_action_explicit_action_always_wins(self):
        self.assertEqual(
            apo_admin.resolve_action(
                "list", "delete_note", {"path": "x.md"}, True
            ),
            "list",
        )
        self.assertEqual(
            apo_admin.resolve_action("describe", "delete_note", None, False),
            "describe",
        )

    def test_omitted_action_invoke_inference_still_requires_confirm(self):
        # Confirmation safety must survive the inference: parameters alone
        # (no confirm) infers invoke, but the destructive handler must not
        # actually run without confirm=true.
        act = apo_admin.resolve_action(None, "delete_note", {"path": "x.md"}, False)
        self.assertEqual(act, "invoke")
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
        self.assertEqual(len(names), 12)
        self.assertIn("apo_admin", names)
        self.assertIn("vault", names)
        self.assertNotIn("scratchpad", names)  # retired 0.33.0
        self.assertEqual(names & _ADMIN_CAPABILITIES, set())

    def test_apo_admin_registered(self):
        names = _list_tool_names()
        self.assertNotIn("delete_note", names)
        self.assertNotIn("memory_status", names)
        self.assertNotIn("telemetry", names)
        self.assertNotIn("expand_section", names)
        self.assertNotIn("place_note", names)




def test_apo_admin_tool_invokes_delete_note_without_explicit_action(tmp_path, monkeypatch):
    """End-to-end regression for the reported bug: the live MCP ``apo_admin``
    tool (not just the pure resolver) must dispatch to delete_note when the
    caller supplies name/parameters/confirm but omits action, instead of
    silently defaulting to action=list and returning the capabilities
    catalog. The real delete_note handler is stubbed out here — this must
    never exercise an actual vault delete.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("APO_NOTES_ROOT", str(vault))
    monkeypatch.setenv("APO_INDEX", str(tmp_path / "index.db"))
    monkeypatch.setenv("APO_COLLECTION", "admin_e2e_test")
    monkeypatch.delenv("APO_MCP_LEAN", raising=False)

    spec = importlib.util.spec_from_file_location("apo_mcp_admin_e2e", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = []

    def fake_delete_note(params, *, vault=""):
        calls.append((dict(params), vault))
        return {"ok": True, "deleted": params.get("path")}

    monkeypatch.setitem(mod._ADMIN_HANDLERS, "delete_note", fake_delete_note)

    async def run():
        return await mod.mcp.call_tool(
            "apo_admin",
            {"name": "delete_note", "parameters": {"path": "x.md"}, "confirm": True},
        )

    result = asyncio.run(run())
    out = result.structured_content

    assert out["ok"] is True
    assert out["admin_capability"] == "delete_note"
    assert "capabilities" not in out
    assert calls == [({"path": "x.md"}, "")]


def test_apo_admin_tool_no_arguments_still_lists(tmp_path, monkeypatch):
    """Documented no-argument apo_admin() behavior must be unchanged."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("APO_NOTES_ROOT", str(vault))
    monkeypatch.setenv("APO_INDEX", str(tmp_path / "index.db"))
    monkeypatch.setenv("APO_COLLECTION", "admin_e2e_test")
    monkeypatch.delenv("APO_MCP_LEAN", raising=False)

    spec = importlib.util.spec_from_file_location("apo_mcp_admin_e2e_list", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    async def run():
        return await mod.mcp.call_tool("apo_admin", {})

    result = asyncio.run(run())
    out = result.structured_content

    assert out["ok"] is True
    assert out["action"] == "list"
    assert {c["name"] for c in out["capabilities"]} == _ADMIN_CAPABILITIES


if __name__ == "__main__":
    unittest.main()
