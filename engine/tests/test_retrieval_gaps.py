"""P0/P1 retrieval gaps: explain, path_context, slug boost, graph_neighbors."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops, vaults

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


class RetrievalGapsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        threads = self.vault / "areas" / "threads"
        threads.mkdir(parents=True)
        (threads / "itops-713-rippling-stripe-scim.md").write_text(
            "---\ntitle: ITOPS-713 Rippling SCIM\nokf_type: Thread\n---\n\n# ITOPS-713\n\nscim stripe rippling integration\n",
            encoding="utf-8",
        )
        (threads / "core-api-rippling-scim.md").write_text(
            "---\ntitle: Core API Rippling SCIM\n---\n\n# Core API\n\nscim rippling feasibility\n",
            encoding="utf-8",
        )
        (threads / "apo-qmd-retrieval-pilot.md").write_text(
            "---\ntitle: Pilot\n---\n\n# Pilot\n\n| Query | Expected |\n| itops 713 | itops-713-rippling-stripe-scim.md |\n",
            encoding="utf-8",
        )
        (threads / "dv-2295-frontend-bundle-bloat.md").write_text(
            "---\ntitle: DV-2295 bundle\n---\n\n# DV-2295\n\nfrontend bundle bloat ticket\n",
            encoding="utf-8",
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
            mock.patch.object(config, "COLLECTION", "retrieval_gaps_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for patch in self._patches:
            patch.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        vaults._usage_layout_cache.clear()
        core._backlink_count_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_slug_boost_ranks_ticket_thread_first(self):
        out = ops.search(
            "itops 713 rippling stripe scim integration",
            folder="areas/threads",
            hybrid=False,
            limit=5,
        )
        self.assertTrue(out["ok"])
        tops = [r["source"] for r in out["results"]]
        self.assertEqual(tops[0], "areas/threads/itops-713-rippling-stripe-scim.md")

    def test_explain_and_path_context_on_hits(self):
        out = ops.search(
            "itops 713 rippling stripe scim",
            folder="areas/threads",
            hybrid=False,
            limit=3,
            explain=True,
        )
        hit = out["results"][0]
        self.assertIn("path_context", hit)
        self.assertGreaterEqual(len(hit["path_context"]), 2)
        self.assertIn("explain", hit)
        self.assertIn("fused", hit["explain"])

    def test_graph_neighbors_out_and_in(self):
        target = self.vault / "areas" / "threads" / "core-api-rippling-scim.md"
        target.write_text(
            target.read_text(encoding="utf-8")
            + "\nSee also [[areas/threads/itops-713-rippling-stripe-scim]].\n",
            encoding="utf-8",
        )
        core.index_files([target], verbose=False)
        out = ops.graph_neighbors(
            "areas/threads/core-api-rippling-scim.md",
            depth=1,
            direction="both",
        )
        self.assertTrue(out["ok"])
        tos = {e["to"] for e in out["edges"]}
        self.assertIn("areas/threads/itops-713-rippling-stripe-scim.md", tos)

    def test_build_path_context_tree(self):
        tree = vaults.build_path_context(
            "areas/threads/foo.md",
            {"areas": "standing work areas"},
        )
        self.assertEqual(tree[0]["label"], "standing work areas")
        self.assertEqual(tree[-1]["segment"], "foo.md")

    def test_basename_wikilink_resolves_unique_stem(self):
        target = self.vault / "areas" / "threads" / "unique-target-thread.md"
        target.write_text("---\ntitle: Target\n---\n\n# Target\n", encoding="utf-8")
        source = self.vault / "areas" / "threads" / "linker-note.md"
        source.write_text(
            "---\ntitle: Linker\n---\n\nSee [[unique-target-thread]].\n",
            encoding="utf-8",
        )
        core.index_files([target, source], verbose=False)
        out = ops.backlinks("areas/threads/unique-target-thread.md")
        self.assertTrue(out["ok"])
        paths = [b["path"] for b in out["backlinks"]]
        self.assertIn("areas/threads/linker-note.md", paths)

    def test_search_expanded_applies_slug_boost(self):
        with mock.patch.object(
            core,
            "expand_query",
            return_value=[{"type": "lex", "query": "itops 713 rippling stripe scim"}],
        ):
            out = ops.search(
                "itops 713 rippling stripe scim",
                folder="areas/threads",
                expand=True,
                hybrid=False,
                limit=5,
            )
        self.assertTrue(out["ok"])
        tops = [r["source"] for r in out["results"]]
        self.assertEqual(tops[0], "areas/threads/itops-713-rippling-stripe-scim.md")


if __name__ == "__main__":
    unittest.main()
