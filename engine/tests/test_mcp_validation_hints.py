"""MCP call_tool surfaces rewritten ValidationError hints (not raw pydantic)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import tool_metrics

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"

# Snapshot pre-test state so tearDownModule can restore it exactly.
# _load_server both (a) mutates os.environ directly with no per-call cleanup
# and (b) deletes+re-execs every already-imported apo_engine.*/apo_mcp*
# sys.modules entry to force a fresh env-driven load. (b) is the sharper bug:
# later test files' own `from apo_engine import vaults` name bindings (taken
# at collection time) still point at the *original* module objects, but a
# string-target `mock.patch("apo_engine.vaults.config")` resolves fresh
# through sys.modules — so it silently patches this file's reloaded copy
# instead of the object the test actually calls, and the mock has no effect
# (see test_multi_vault.py::test_legacy_single_vault, which read the config
# default "notes_global" instead of its patched "coll_a" when run after this
# file, with no exception raised to hint at the mismatch).
import sys as _sys

_ENV_KEYS = ("APO_NOTES_ROOT", "APO_INDEX", "APO_COLLECTION", "APO_TOOL_METRICS")
_ORIG_ENV = {k: os.environ.get(k) for k in _ENV_KEYS}
_ORIG_MODULES = {
    k: v for k, v in _sys.modules.items() if k == "apo_engine" or k.startswith("apo_engine.")
}


def tearDownModule():
    for k, orig in _ORIG_ENV.items():
        if orig is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig
    for name in [k for k in _sys.modules if k == "apo_engine" or k.startswith("apo_engine.")]:
        if name in _ORIG_MODULES:
            _sys.modules[name] = _ORIG_MODULES[name]
        else:
            del _sys.modules[name]


def _load_server(vault: Path, tmp: Path):
    import sys

    env_keys = {
        "APO_NOTES_ROOT": str(vault),
        "APO_INDEX": str(tmp / "index.db"),
        "APO_COLLECTION": "hint_test",
        "APO_TOOL_METRICS": "1",
    }
    for k, v in env_keys.items():
        os.environ[k] = v
    # Fresh import each time — env is read at module load.
    for name in list(sys_modules_apo()):
        del sys.modules[name]
    src = str(_ENGINE / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    for name in list(sys.modules):
        if name == "apo_engine" or name.startswith("apo_engine."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("apo_mcp_hints", _SERVER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sys_modules_apo():
    import sys

    return [k for k in sys.modules if k.startswith("apo_mcp")]


class McpValidationHintTest(unittest.TestCase):
    def test_metrics_middleware_is_outer(self):
        with tempfile.TemporaryDirectory(prefix="apo-hint-") as tmp_s:
            tmp = Path(tmp_s)
            vault = tmp / "vault"
            vault.mkdir()
            mod = _load_server(vault, tmp)
            from apo_engine.agent_validation import AgentValidationMiddleware
            from apo_engine.tool_metrics_middleware import ToolMetricsMiddleware

            chain = list(mod.mcp.middleware)
            # FastMCP may prepend DereferenceRefsMiddleware; among our two,
            # metrics must be listed before validation (first = outermost).
            tm_idx = next(
                i for i, m in enumerate(chain) if isinstance(m, ToolMetricsMiddleware)
            )
            av_idx = next(
                i
                for i, m in enumerate(chain)
                if isinstance(m, AgentValidationMiddleware)
            )
            self.assertLess(tm_idx, av_idx)

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

            # conftest redirects tool_metrics.DEFERRED_DIR into an isolated tmp.
            metrics_db = tool_metrics.metrics_db_path()
            self.assertTrue(metrics_db.is_file(), f"missing metrics db at {metrics_db}")
            stats = tool_metrics.tool_stats("hint_test", days=None, path=metrics_db)
            patch_errs = [
                t
                for t in stats.get("by_tool", [])
                if t.get("tool") == "patch_note" and t.get("error", 0) > 0
            ]
            self.assertEqual(len(patch_errs), 1)
            row = patch_errs[0]
            shapes = row.get("by_error_shape") or {}
            self.assertTrue(
                any("ops" in str(k) or "op" in str(k) for k in shapes),
                f"expected ops/op shape in {shapes!r}",
            )

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
