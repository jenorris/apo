"""Mermaid parse coverage — flowchart + sequence fixtures."""

from __future__ import annotations

import unittest
from pathlib import Path

from apo_engine.mermaid_parse import parse_mermaid

FIX = Path(__file__).parent / "fixtures" / "standard-data-flow.mmd"


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


if __name__ == "__main__":
    unittest.main()
