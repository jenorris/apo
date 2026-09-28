"""Mermaid parse coverage — flowchart + sequence fixtures."""

from __future__ import annotations

import unittest
from pathlib import Path

from apo_engine.mermaid_parse import parse_mermaid

FIXTURES = Path(__file__).parent / "fixtures"
FIX = FIXTURES / "standard-data-flow.mmd"


def _parse_fixture(name: str):
    return parse_mermaid((FIXTURES / name).read_text(encoding="utf-8"))


class MermaidParseTest(unittest.TestCase):
    def test_flowchart_fixture(self):
        text = FIX.read_text(encoding="utf-8")
        d = parse_mermaid(text)
        self.assertTrue(d.ok)
        self.assertEqual(len(d.nodes), 7)
        self.assertEqual(len(d.edges), 6)
        self.assertIn("GradGuard", d.subgraphs)
        stripe = next(n for n in d.nodes if n.node_id == "P")
        self.assertEqual(stripe.label, "Stripe")

    def test_sequence_participants(self):
        text = """sequenceDiagram
    participant A as Client
    participant B as Stripe
    A->>B: Pay
"""
        d = parse_mermaid(text)
        self.assertTrue(d.ok)
        self.assertEqual(len(d.nodes), 2)
        self.assertEqual(len(d.edges), 1)

    def test_empty_is_error(self):
        d = parse_mermaid("")
        self.assertFalse(d.ok)


class RealFenceRegressionTest(unittest.TestCase):
    """Fixtures pulled from real hand-authored vault diagrams that the
    pre-fix parser dropped, mislabeled, or garbled. Each one regressions a
    specific gap named in the mermaid-indexing review.
    """

    def test_inline_shaped_edges_and_labeled_branch(self):
        # `A[Label] --> B[Label]` inline edges, plus `D -->|no| A` labeled
        # bare-id branches feeding back into an earlier node.
        d = _parse_fixture("real-ref-pinning.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertEqual(len(d.nodes), 10)
        self.assertEqual(len(d.edges), 10)
        by_id = {n.node_id: n.label for n in d.nodes}
        self.assertEqual(by_id["A"], "Branch: edit modules/apps/X")
        self.assertEqual(by_id["D"], "OK?")
        self.assertIn(("D", "A", "no"), [(e.from_id, e.to_id, e.label) for e in d.edges])
        self.assertIn(("D", "E", "yes"), [(e.from_id, e.to_id, e.label) for e in d.edges])

    def test_bare_id_inline_edges_no_subgraph(self):
        # Previously produced zero nodes/edges (old _SIMPLE_EDGE_RE anchors
        # full-line, no shapes at all here but a fan-out from one node).
        d = _parse_fixture("real-rds-proxy.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertEqual(len(d.nodes), 5)
        self.assertEqual(len(d.edges), 4)

    def test_labels_with_equals_and_angle_brackets_dont_break_arrow_scan(self):
        # `EXPLAIN FORMAT=JSON` and `>= row threshold?` inside quoted labels
        # must not be mistaken for arrow operators once diamond/rect shapes
        # sit on the *target* side of an edge line.
        d = _parse_fixture("real-explain-gate.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertEqual(len(d.nodes), 10)
        self.assertEqual(len(d.edges), 9)
        by_id = {n.node_id: n.label for n in d.nodes}
        self.assertEqual(by_id["G"], "EXPLAIN FORMAT=JSON")
        self.assertEqual(by_id["H"], "access_type ALL on table >= row threshold?")
        edge_pairs = [(e.from_id, e.to_id, e.label) for e in d.edges]
        self.assertIn(("F", "G", ""), edge_pairs)
        self.assertIn(("G", "H", ""), edge_pairs)
        self.assertIn(("H", "I", "yes"), edge_pairs)
        self.assertIn(("H", "J", "no"), edge_pairs)

    def test_unquoted_subgraph_label_and_chained_edges(self):
        # `subgraph p1 [Phase 1 — now]` (unquoted bracket label) and
        # `F1 --> H1 --> E1` (a 3-node chain meaning 2 edges).
        d = _parse_fixture("real-floci-phases.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertEqual(d.subgraphs, ["Phase 1 — now", "Phase 2", "Phase 3"])
        by_id = {n.node_id: n for n in d.nodes}
        self.assertEqual(by_id["F1"].label, "Floci")
        self.assertEqual(by_id["F1"].subgraph, "Phase 1 — now")
        edge_pairs = [(e.from_id, e.to_id) for e in d.edges]
        self.assertIn(("F1", "H1"), edge_pairs)
        self.assertIn(("H1", "E1"), edge_pairs)

    def test_quoted_subgraph_em_dash_and_labeled_edge(self):
        d = _parse_fixture("real-sso-complementary.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertIn("Authoritative student record (SIS / integration initiatives)", d.subgraphs)
        by_id = {n.node_id: n.label for n in d.nodes}
        self.assertEqual(by_id["SIS"], "SIS — Banner, Colleague, etc.")
        edge = next(e for e in d.edges if e.from_id == "GGSSO" and e.to_id == "GGREC")
        self.assertEqual(edge.label, "join on canonical keys only")

    def test_dotted_labeled_edge_and_chain(self):
        # `-.->|"canonical student_id"|` — dotted arrow with a quoted label.
        d = _parse_fixture("real-sso-roster-vs-sso.mmd")
        self.assertTrue(d.ok, d.parse_error)
        dotted = next(e for e in d.edges if e.from_id == "SIS2" and e.to_id == "SSO2")
        self.assertEqual(dotted.label, "canonical student_id")
        chain_pairs = [(e.from_id, e.to_id) for e in d.edges]
        self.assertIn(("SIS2", "Batch"), chain_pairs)
        self.assertIn(("Batch", "Covered"), chain_pairs)

    def test_unquoted_subgraph_bracket_labels_three_groups(self):
        d = _parse_fixture("real-slack-status-bot.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertEqual(d.subgraphs, ["Inputs", "slack-status-bot.py", "Outputs"])
        self.assertEqual(len(d.nodes), 11)
        self.assertEqual(len(d.edges), 11)

    def test_sequence_arrow_with_no_space_before_arrowhead(self):
        # `API-->>MCP` (no space before the arrow) must yield participant
        # `API`, not `API-` — the old greedy `\S+` id match ate one dash.
        d = _parse_fixture("real-mcp-seq.mmd")
        self.assertTrue(d.ok, d.parse_error)
        ids = {n.node_id for n in d.nodes}
        self.assertIn("API", ids)
        self.assertNotIn("API-", ids)
        reply = next(e for e in d.edges if e.from_id == "API" and e.to_id == "MCP")
        self.assertEqual(reply.label, "Set-Cookie XSRF-TOKEN, api_v2")

    def test_xychart_beta_is_file_only(self):
        # Non-flow diagram type: no per-node parsing of bar/axis data,
        # but still a valid (non-error) diagram at file level.
        d = _parse_fixture("real-xychart.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertEqual(d.diagram_type, "xychart-beta")
        self.assertEqual(d.nodes, [])
        self.assertEqual(d.edges, [])

    def test_gitgraph_is_file_only(self):
        d = _parse_fixture("real-gitgraph.mmd")
        self.assertTrue(d.ok, d.parse_error)
        self.assertEqual(d.diagram_type, "gitgraph")
        self.assertEqual(d.nodes, [])
        self.assertEqual(d.edges, [])


if __name__ == "__main__":
    unittest.main()
