"""MCP call_tool surfaces rewritten ValidationError hints (not raw pydantic)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import tool_metrics
from apo_engine.agent_validation import AgentValidationMiddleware
from apo_engine.tool_metrics_middleware import ToolMetricsMiddleware

_ENGINE = Path(__file__).resolve().parents[1]
_SERVER = _ENGINE / "mcp" / "server.py"


def _load_server(vault: Path, tmp: Path):
    env_keys = {
        "APO_MCP_LEAN": "1",
        "APO_NOTES_ROOT": str(vault),
        "APO_INDEX": str(tmp / "index.db"),
        "APO_COLLECTION": "hint_test",
        "APO_TOOL_METRICS": "1",
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
    def test_metrics_middleware_is_outer(self):
        with tempfile.TemporaryDirectory(prefix="apo-hint-") as tmp_s:
            tmp = Path(tmp_s)
            vault = tmp / "vault"
            vault.mkdir()
            mod = _load_server(vault, tmp)
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
            metrics_path = tool_metrics.DEFERRED_DIR / "tool-metrics-hint_test.jsonl"
            self.assertTrue(metrics_path.is_file(), f"missing metrics at {metrics_path}")
            rows = [
                json.loads(line)
                for line in metrics_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            patch_errs = [
                r for r in rows if r.get("tool") == "patch_note" and r.get("ok") is False
            ]
            self.assertEqual(len(patch_errs), 1)
            row = patch_errs[0]
            self.assertEqual(row["error"], "validation_error")
            self.assertGreater(row.get("resp_bytes", 0), 0)
            shapes = row.get("error_shape") or []
            self.assertTrue(
                any("ops" in str(s) or "op" in str(s) for s in shapes),
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
