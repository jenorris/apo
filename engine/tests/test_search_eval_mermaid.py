"""search_eval chunk_kind + entity scoring."""

from __future__ import annotations

import unittest

from apo_engine.search_eval import _score_hit


class SearchEvalMermaidTest(unittest.TestCase):
    def test_path_only_backward_compat(self):
        results = [{"source": "a.md", "chunk_kind": "section"}]
        rank, _ = _score_hit(results, expect=["a.md"], cut=5)
        self.assertEqual(rank, 1)

    def test_chunk_kind_filter(self):
        results = [
            {"source": "d.mmd", "chunk_kind": "mermaid_file", "content": "summary"},
            {"source": "d.mmd", "chunk_kind": "mermaid_node", "content": "Stripe processor"},
        ]
        rank, hit = _score_hit(
            results,
            expect=["d.mmd"],
            expect_chunk_kind="mermaid_node",
            cut=5,
        )
        self.assertEqual(rank, 2)
        self.assertEqual(hit["chunk_kind"], "mermaid_node")

    def test_entity_filter(self):
        results = [
            {"source": "d.mmd", "chunk_kind": "mermaid_node", "content": "DFD > Stripe — Payment"},
        ]
        rank, _ = _score_hit(
            results,
            expect=["d.mmd"],
            expect_chunk_kind="mermaid_node",
            expect_entity="Stripe",
            cut=5,
        )
        self.assertEqual(rank, 1)


if __name__ == "__main__":
    unittest.main()
