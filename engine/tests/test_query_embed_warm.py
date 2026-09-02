"""Query-embed cold-start: keep_alive, disk TTL, MCP warm hydration."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core


class QueryEmbedWarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "NOTES_ROOT", self.tmp / "vault"),
            mock.patch.object(config, "COLLECTION", "qemb_warm_test"),
            mock.patch.object(config, "QUERY_EMBED_DISK_CACHE", True),
            mock.patch.object(config, "QUERY_EMBED_DISK_TTL", 86400.0),
            mock.patch.object(config, "QUERY_EMBED_TTL", 600.0),
            mock.patch.object(config, "QUERY_EMBED_WARM_DISK_LIMIT", 8),
            mock.patch.object(config, "EMBED_BACKEND", "ollama"),
        ]
        for p in self._patches:
            p.start()
        (self.tmp / "vault").mkdir()
        with mock.patch.object(core, "embed", return_value=[[0.1] * 16]):
            core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        core.clear_query_embed_cache()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_query_embed_passes_keep_alive_to_ollama(self):
        core.clear_query_embed_cache()
        config.QUERY_EMBED_KEEP_ALIVE = "5m"
        with mock.patch.object(core, "_ollama_embed_request", return_value=[[0.1, 0.2]]) as req:
            vec = core.query_embed("hello world")
        self.assertEqual(vec, [0.1, 0.2])
        req.assert_called_once()
        self.assertEqual(req.call_args.kwargs.get("keep_alive"), "5m")

    def test_disk_ttl_outlasts_memory_ttl(self):
        core.clear_query_embed_cache()
        with mock.patch.object(core, "_ollama_embed_request", return_value=[[1.0, 2.0]]):
            core.query_embed("long lived query")
        with core._query_embed_lock:
            core._query_embed_cache.clear()
        config.QUERY_EMBED_TTL = 1.0
        time.sleep(1.1)
        with mock.patch.object(core, "_ollama_embed_request", side_effect=AssertionError("no ollama")):
            vec = core.query_embed("long lived query")
        self.assertEqual(vec, [1.0, 2.0])

    def test_hydrate_loads_recent_disk_entries(self):
        core.clear_query_embed_cache()
        db = core.writer_connect()
        payload = json.dumps(
            {
                "model": config.MODEL_NAME,
                "vec": [0.5, 0.6],
                "at": time.time(),
                "q": "hydrated query",
            }
        )
        key = core._query_embed_meta_key("hydrated query")
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, payload))
        db.commit()
        n = core._hydrate_query_embed_from_disk()
        self.assertEqual(n, 1)
        with mock.patch.object(core, "_ollama_embed_request", side_effect=AssertionError("no ollama")):
            vec = core.query_embed("hydrated query")
        self.assertEqual(vec, [0.5, 0.6])

    def test_warm_query_embed_preloads_ollama(self):
        core.clear_query_embed_cache()
        with mock.patch.object(core, "_ollama_embed_request", return_value=[[0.0]]) as req:
            stats = core.warm_query_embed(preload_ollama=True)
        self.assertTrue(stats["ollama_warmed"])
        req.assert_called_once()
        self.assertEqual(req.call_args.kwargs.get("keep_alive"), config.QUERY_EMBED_KEEP_ALIVE)


if __name__ == "__main__":
    unittest.main()
