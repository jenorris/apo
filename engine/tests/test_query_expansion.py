"""Typed query expansion (lex/vec/hyde) — adapted from qmd (github.com/tobi/qmd).

core.search_lex_only / search_vector_only are the exclusive-routing primitives;
core.expand_query / search_expanded build typed sub-query RRF fusion on top; ops.search
exposes it via expand=.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops

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


class _OllamaResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class QueryExpansionUnitTest(unittest.TestCase):
    """expand_query() in isolation — no index needed."""

    def test_disabled_returns_base_two_entries(self):
        with mock.patch.object(config, "QUERY_EXPAND", False):
            out = core.expand_query("database timeout")
        self.assertEqual(
            out,
            [
                {"type": "lex", "query": "database timeout", "weight": 2.0},
                {"type": "vec", "query": "database timeout", "weight": 2.0},
            ],
        )

    def test_enabled_adds_llm_typed_entries(self):
        # qmd-query-expansion-1.7B's native output: plain lex:/vec:/hyde: lines,
        # possibly several of the same type, sometimes a stray <think> line.
        payload = {
            "response": (
                "<think>\n"
                '</think>\n\n'
                'lex: "connection pool" timeout -redis\n'
                "lex: db connection timeout\n"
                "vec: why do database connections time out under load\n"
                "hyde: Database connections time out when the pool is exhausted.\n"
            )
        }
        with (
            mock.patch.object(config, "QUERY_EXPAND", True),
            mock.patch("apo_engine.core.urllib.request.urlopen", return_value=_OllamaResponse(payload)),
        ):
            out = core.expand_query("database timeout")
        types = [sq["type"] for sq in out]
        self.assertEqual(types, ["lex", "vec", "lex", "lex", "vec", "hyde"])
        self.assertEqual(out[2]["query"], '"connection pool" timeout -redis')
        self.assertEqual(out[2]["weight"], 1.0)

    def test_enabled_handles_stray_unclosed_think_tag(self):
        # Observed live: a <think> line with no matching close, output following
        # directly on the next lines anyway.
        payload = {"response": "<think>\nlex: db timeout\nvec: why does the db time out\n"}
        with (
            mock.patch.object(config, "QUERY_EXPAND", True),
            mock.patch("apo_engine.core.urllib.request.urlopen", return_value=_OllamaResponse(payload)),
        ):
            out = core.expand_query("database timeout")
        self.assertEqual(len(out), 4)
        self.assertEqual(out[2], {"type": "lex", "query": "db timeout", "weight": 1.0})

    def test_enabled_falls_back_on_backend_failure(self):
        with (
            mock.patch.object(config, "QUERY_EXPAND", True),
            mock.patch("apo_engine.core.urllib.request.urlopen", side_effect=OSError("connection refused")),
        ):
            out = core.expand_query("database timeout")
        self.assertEqual(len(out), 2)
        self.assertEqual({sq["type"] for sq in out}, {"lex", "vec"})

    def test_enabled_falls_back_when_no_typed_lines_present(self):
        with (
            mock.patch.object(config, "QUERY_EXPAND", True),
            mock.patch(
                "apo_engine.core.urllib.request.urlopen",
                return_value=_OllamaResponse({"response": "no typed lines here at all"}),
            ),
        ):
            out = core.expand_query("database timeout")
        self.assertEqual(len(out), 2)


class SearchExpandedIndexedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        # gribblefrotz: keyword-only match (BM25 should find it, embed vector won't
        # unless the exact token is present — _fake_embed is token-hash based).
        (self.vault / "kw.md").write_text(
            "---\ntitle: KW\n---\n\n# KW\n\ngribblefrotz appears here only.\n",
            encoding="utf-8",
        )
        (self.vault / "other.md").write_text(
            "---\ntitle: Other\n---\n\n# Other\n\nunrelated content about widgets.\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "query_expansion_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(config, "QUERY_EXPAND", False),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for patch in self._patches:
            patch.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_search_lex_only_finds_keyword_hit(self):
        hits = core.search_lex_only("gribblefrotz")
        self.assertTrue(any(h.path == "kw.md" for h in hits))

    def test_search_vector_only_returns_hits(self):
        qvec = core.query_embed("gribblefrotz")
        hits = core.search_vector_only(qvec)
        self.assertTrue(hits)

    def test_search_expanded_default_finds_keyword_hit(self):
        # QUERY_EXPAND=False → base lex+vec on the raw query, no LLM call.
        hits = core.search_expanded("gribblefrotz", k=5)
        self.assertTrue(any(h.path == "kw.md" for h in hits))

    def test_ops_search_expand_true_end_to_end(self):
        out = ops.search("gribblefrotz", expand=True, limit=5)
        self.assertTrue(out["results"])
        self.assertTrue(any(r["source"] == "kw.md" for r in out["results"]))

    def test_ops_search_expand_and_ref_rejected(self):
        out = ops.search("gribblefrotz", expand=True, ref="HEAD")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
