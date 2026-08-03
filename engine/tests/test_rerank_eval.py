"""Cross-encoder rerank hook + search-eval harness (no Ollama, no fastembed)."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops, rerank, search_eval

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


class _StubEncoder:
    """Scores docs by count of a marker token — deterministic reorder."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def rerank(self, query: str, docs: list[str]):
        return [float(d.lower().count(self.marker)) for d in docs]


class RerankVaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-rerank-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        # Both notes mention "alpha"; only the second contains the stub marker.
        (self.vault / "first.md").write_text(
            "# First\n\nalpha alpha alpha common words\n", encoding="utf-8"
        )
        (self.vault / "second.md").write_text(
            "# Second\n\nalpha zebra marker body\n", encoding="utf-8"
        )
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            mock.patch.object(config, "COLLECTION", "rerank_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        core.writer_close()
        core.reader_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rerank_reorders_and_flags_response(self):
        with (
            mock.patch.object(config, "RERANK", True),
            mock.patch.object(rerank, "_get_encoder", lambda: (_StubEncoder("zebra"), None)),
        ):
            out = ops.search("alpha", limit=2)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out.get("reranked"), out)
        self.assertEqual(out["results"][0]["source"], "second.md")
        self.assertEqual(out["results"][0]["score"], 1.0)

    def test_rerank_missing_dep_falls_back_with_warning(self):
        with (
            mock.patch.object(config, "RERANK", True),
            mock.patch.object(
                rerank, "_get_encoder", lambda: (None, "fastembed not installed — pip install …")
            ),
        ):
            out = ops.search("alpha", limit=2)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("reranked", out)
        self.assertIn("rerank unavailable", out.get("warning", ""))
        self.assertTrue(out["results"])  # fused order still returned

    def test_rerank_off_by_default(self):
        out = ops.search("alpha", limit=2)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("reranked", out)

    def test_default_exclude_applies_to_unscoped_search_only(self):
        (self.vault / "noise").mkdir()
        (self.vault / "noise" / "log.md").write_text(
            "# Log\n\nalpha alpha alpha alpha noise entry\n", encoding="utf-8"
        )
        core.index_vault(verbose=False)
        with mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", ["noise/*"]):
            unscoped = ops.search("alpha", limit=5)
            scoped = ops.search("alpha", folder="noise", limit=5)
            explicit = ops.search("alpha", limit=5, exclude=["first*"])
        self.assertEqual(unscoped.get("default_exclude"), ["noise/*"])
        self.assertNotIn("noise/log.md", [r["source"] for r in unscoped["results"]])
        self.assertIn("noise/log.md", [r["source"] for r in scoped["results"]])
        self.assertNotIn("default_exclude", scoped)
        self.assertNotIn("default_exclude", explicit)
        self.assertIn("noise/log.md", [r["source"] for r in explicit["results"]])

    def test_search_eval_reports_hit_and_miss(self):
        eval_file = self.tmp / "eval.yaml"
        eval_file.write_text(
            textwrap.dedent(
                """
                k: 3
                queries:
                  - query: "zebra marker"
                    expect: ["second.md"]
                  - query: "alpha"
                    expect: ["does-not-exist.md"]
                """
            ),
            encoding="utf-8",
        )
        report = search_eval.run_eval(eval_file)
        self.assertTrue(report["ok"])
        self.assertEqual(report["queries"], 2)
        self.assertEqual(report["hit_at_k"], 0.5)
        self.assertGreater(report["mrr_at_k"], 0.0)
        text = search_eval.format_report(report)
        self.assertIn("MISS", text)


if __name__ == "__main__":
    unittest.main()
