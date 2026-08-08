"""Unit tests for ToolMetricsMiddleware vault/collection resolution."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from apo_engine import tool_metrics
from apo_engine.tool_metrics_middleware import ToolMetricsMiddleware


class ToolMetricsMiddlewareResolveTest(unittest.TestCase):
    def test_resolve_prefers_registry_collection(self) -> None:
        mw = ToolMetricsMiddleware(
            collection="default",
            vault_resolver=lambda _args: ("workbench", Path("/tmp/wb"), "workbench"),
        )
        vid, root, coll = mw._resolve_vault({"vault": "workbench"})
        self.assertEqual(vid, "workbench")
        self.assertEqual(root, Path("/tmp/wb"))
        self.assertEqual(coll, "workbench")

    def test_resolve_falls_back_for_legacy_2tuple(self) -> None:
        mw = ToolMetricsMiddleware(
            collection="default",
            vault_resolver=lambda _args: ("work", Path("/tmp/work")),
        )
        vid, root, coll = mw._resolve_vault({})
        self.assertEqual(vid, "work")
        self.assertEqual(root, Path("/tmp/work"))
        self.assertEqual(coll, "default")

    def test_record_uses_resolved_collection(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "metrics.duckdb"
                # Patch get_backend path via record_call path= kw — middleware
                # calls record_call without path, so set APO metrics path via
                # recording through a thin wrapper by monkeypatching record_call.
                recorded: list[dict] = []

                def _capture(**kwargs):  # type: ignore[no-untyped-def]
                    recorded.append(kwargs)

                orig = tool_metrics.record_call
                tool_metrics.record_call = _capture  # type: ignore[assignment]
                try:
                    mw = ToolMetricsMiddleware(
                        collection="default",
                        vault_resolver=lambda _a: (
                            "workbench",
                            Path("/tmp/wb"),
                            "workbench",
                        ),
                    )
                    ctx = MagicMock()
                    ctx.message = MagicMock()
                    ctx.message.name = "search_notes"
                    ctx.message.arguments = {
                        "vault": "workbench",
                        "folder": "docs",
                        "query": "x",
                    }
                    call_next = AsyncMock(return_value={"ok": True, "hits": []})
                    out = await mw.on_call_tool(ctx, call_next)
                    self.assertEqual(out, {"ok": True, "hits": []})
                    self.assertEqual(len(recorded), 1)
                    self.assertEqual(recorded[0]["collection"], "workbench")
                    self.assertEqual(recorded[0]["vault_id"], "workbench")
                    self.assertEqual(recorded[0]["tool"], "search_notes")
                finally:
                    tool_metrics.record_call = orig  # type: ignore[assignment]

        import asyncio

        asyncio.run(_run())


class RemapDefaultCollectionsTest(unittest.TestCase):
    def test_remap_default_to_vault_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metrics.duckdb"
            tool_metrics.record_call(
                collection="default",
                tool="search_notes",
                ok=True,
                vault_id="workbench",
                path=db_path,
            )
            tool_metrics.record_call(
                collection="default",
                tool="vault",
                ok=True,
                vault_id="work",
                path=db_path,
            )
            tool_metrics.record_call(
                collection="default",
                tool="search_notes",
                ok=True,
                vault_id="",
                path=db_path,
            )
            out = tool_metrics.remap_default_collections_by_vault_id(path=db_path)
            self.assertTrue(out["ok"])
            self.assertEqual(out["updated"], 2)
            wb = tool_metrics.read_events("workbench", path=db_path)
            work = tool_metrics.read_events("work", path=db_path)
            still_default = tool_metrics.read_events("default", path=db_path)
            self.assertEqual(len(wb), 1)
            self.assertEqual(len(work), 1)
            self.assertEqual(len(still_default), 1)


if __name__ == "__main__":
    unittest.main()
