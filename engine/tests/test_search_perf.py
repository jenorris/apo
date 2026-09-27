"""Search performance regressions (hybrid=False routing, embed cache)."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops, ranking, vaults


_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


class SearchPerfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        thread = self.vault / "areas" / "threads"
        thread.mkdir(parents=True)
        (thread / "alpha-note.md").write_text(
            "# Alpha\n\nalpha widget keyword content\n", encoding="utf-8"
        )
        (thread / "beta-note.md").write_text(
            "# Beta\n\nbeta unrelated content\n", encoding="utf-8"
        )
        (self.vault / "system" / "contracts").mkdir(parents=True)
        (self.vault / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "layout:\n  areas: standing work areas\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "search_perf_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(config, "QUERY_EMBED_DISK_CACHE", True),
            mock.patch.object(core, "embed", _fake_embed),
        ]
        for patch in self._patches:
            patch.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        vaults._usage_layout_cache.clear()
        core.clear_query_embed_cache()
        ops._lint_sweep_cache.clear()
        ranking._frontmatter_boost_cache.clear()
        ranking._backlink_count_cache.clear()
        note_lint = __import__("apo_engine.note_lint", fromlist=["note_lint"])
        note_lint._wiki_index_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hybrid_false_skips_query_embed(self):
        with mock.patch.object(core, "query_embed", side_effect=AssertionError("embed called")):
            out = ops.search("alpha widget", folder="areas/threads", hybrid=False, limit=3)
        self.assertTrue(out["ok"])
        self.assertTrue(out["results"])
        self.assertEqual(out["results"][0]["source"], "areas/threads/alpha-note.md")

    def test_query_embed_disk_cache_roundtrip(self):
        core.clear_query_embed_cache()
        with mock.patch.object(core, "embed", wraps=_fake_embed) as emb:
            first = core.query_embed("cache me roundtrip")
            with core._query_embed_lock:
                core._query_embed_cache.clear()
            second = core.query_embed("cache me roundtrip")
        self.assertEqual(first, second)
        self.assertEqual(emb.call_count, 1)

    def test_prefetch_path_boost_data_warms_caches(self):
        path = "areas/threads/alpha-note.md"
        ranking._frontmatter_boost_cache.clear()
        ranking._backlink_count_cache.clear()
        ranking._prefetch_path_boost_data([path])
        with mock.patch.object(core, "reader_connect") as rc:
            ranking._frontmatter_boost_fields(path)
            ranking._backlink_count(path)
            rc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
