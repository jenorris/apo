"""Pure-function tests for Mermaid flatten / search stringification."""

from __future__ import annotations

import unittest

from apo_engine import mermaid_index as mi
from apo_engine import mermaid_parse as mp
from apo_engine.search_eval import _score_hit


class NodeFlattenTest(unittest.TestCase):
    def test_contract_template_shape(self):
        flat = mi.node_flatten_text(
            "DFD — Cardholder",
            "PAY",
            "STR",
            "Stripe — card",
            template="{title} > {subgraph} > {node} — {label}",
        )
        self.assertEqual(flat, "DFD — Cardholder > PAY > STR — Stripe — card")

    def test_empty_subgraph_collapses(self):
        flat = mi.node_flatten_text(
            "Title",
            "",
            "N1",
            "Label",
            template="{title} > {subgraph} > {node} — {label}",
        )
        self.assertNotIn(" >  > ", flat)
        self.assertEqual(flat, "Title > N1 — Label")

    def test_no_relational_context_leaves_base_text_unchanged(self):
        flat = mi.node_flatten_text("Title", "PAY", "STR", "Stripe — card")
        self.assertNotIn(" · ", flat)

    def test_appends_in_and_out_relational_labels(self):
        flat = mi.node_flatten_text(
            "DFD — Cardholder",
            "API",
            "SKY",
            "skypad — renters",
            in_labels=["Core API"],
            out_labels=["Stripe — card", "Authorize.net — ACH"],
        )
        self.assertTrue(flat.startswith("DFD — Cardholder > API > SKY — skypad — renters"))
        self.assertIn("· from: Core API", flat)
        self.assertIn("· to: Stripe — card, Authorize.net — ACH", flat)

    def test_in_only_omits_to_segment(self):
        flat = mi.node_flatten_text(
            "Title", "", "N1", "Label", in_labels=["Upstream"], out_labels=[]
        )
        self.assertIn("from: Upstream", flat)
        self.assertNotIn("to:", flat)

    def test_relational_context_applies_with_custom_template_too(self):
        flat = mi.node_flatten_text(
            "Title",
            "PAY",
            "STR",
            "Stripe — card",
            template="{title} > {subgraph} > {node} — {label}",
            in_labels=["skypad — renters"],
            out_labels=[],
        )
        self.assertEqual(flat, "Title > PAY > STR — Stripe — card · from: skypad — renters")


class NodeRelationsTest(unittest.TestCase):
    def test_derives_labels_and_1hop_in_out(self):
        diagram = mp.MermaidDiagram(
            diagram_type="flowchart",
            nodes=[
                mp.MermaidNode("API1", "Core API", "API"),
                mp.MermaidNode("SKY", "skypad — renters", "API"),
                mp.MermaidNode("STR", "Stripe — card", "PAY"),
                mp.MermaidNode("AUTH", "Authorize.net — ACH", "PAY"),
            ],
            edges=[
                mp.MermaidEdge("API1", "SKY", ""),
                mp.MermaidEdge("SKY", "STR", ""),
                mp.MermaidEdge("SKY", "AUTH", ""),
            ],
        )
        label_by_id, in_map, out_map = mi.node_relations(diagram)
        self.assertEqual(label_by_id["SKY"], "skypad — renters")
        self.assertEqual(in_map["SKY"], ["Core API"])
        self.assertEqual(out_map["SKY"], ["Stripe — card", "Authorize.net — ACH"])
        self.assertNotIn("API1", in_map)  # nothing feeds API1 in this fixture

    def test_dangling_edge_falls_back_to_raw_id(self):
        diagram = mp.MermaidDiagram(
            diagram_type="flowchart",
            nodes=[mp.MermaidNode("A", "Alpha")],
            edges=[mp.MermaidEdge("A", "GHOST", "")],
        )
        label_by_id, _in_map, out_map = mi.node_relations(diagram)
        self.assertEqual(out_map["A"], ["GHOST"])
        self.assertNotIn("GHOST", label_by_id)


class EntityTokensTest(unittest.TestCase):
    def test_acronym_and_label_words(self):
        tokens = mi._entity_search_tokens("STR", "Stripe — card")
        self.assertIn("STR", tokens)
        self.assertIn("Stripe", tokens)
        self.assertIn("card", tokens)

    def test_tuition_program_yields_tuition_token(self):
        tokens = mi._entity_search_tokens("TProg", "Tuition Program")
        self.assertIn("Tuition", tokens)
        self.assertIn("Program", tokens)
        # expect_entity: Tuition must match flattened node text
        flat = (
            mi.node_flatten_text(
                "DFD — Standard Data Flow",
                "GradGuard",
                "TProg",
                "Tuition Program",
                template="{title} > {subgraph} > {node} — {label}",
            )
            + " · "
            + tokens
        )
        self.assertIn("tuition", flat.lower())


class CatalogPrefixTest(unittest.TestCase):
    def test_slug_token_expansion(self):
        prefix = mi.catalog_search_prefix(
            {
                "diagram_id": "cardholder-data-flow",
                "title": "DFD — Cardholder Data Flow",
                "type": "flowchart",
            }
        )
        self.assertIn("cardholder-data-flow", prefix)
        self.assertIn("cardholder", prefix)
        self.assertIn("data", prefix)
        self.assertIn("flow", prefix)
        self.assertIn("DFD — Cardholder Data Flow", prefix)
        self.assertIn("flowchart", prefix)


class FileHeaderEdgeFlattenTest(unittest.TestCase):
    def setUp(self):
        self.diagram = mp.MermaidDiagram(
            diagram_type="flowchart",
            direction="LR",
            nodes=[
                mp.MermaidNode("STR", "Stripe — card", "PAY"),
                mp.MermaidNode("SKY", "skypad — renters", "API"),
            ],
            edges=[mp.MermaidEdge("SKY", "STR", "")],
            subgraphs=["WEB", "PAY"],
        )

    def test_file_flatten_has_labels_not_raw_direction(self):
        flat = mi.file_flatten_text("DFD — Cardholder", self.diagram)
        self.assertIn("Stripe — card", flat)
        self.assertIn("flowchart", flat)
        self.assertNotIn("flowchart LR", flat)
        self.assertNotIn("subgraph", flat.lower())

    def test_header_flatten_lists_subgraphs(self):
        flat = mi.header_flatten_text("DFD — Cardholder", self.diagram)
        self.assertIn("subgraphs:", flat)
        self.assertIn("WEB", flat)
        self.assertIn("PAY", flat)
        self.assertNotIn("flowchart LR", flat)

    def test_edge_flatten_falls_back_to_raw_ids_without_label_map(self):
        flat = mi.edge_flatten_text("DFD — Cardholder", self.diagram.edges[0])
        self.assertEqual(flat, "DFD — Cardholder > SKY --> STR")

    def test_edge_flatten_resolves_node_labels(self):
        label_by_id, _in_map, _out_map = mi.node_relations(self.diagram)
        flat = mi.edge_flatten_text("DFD — Cardholder", self.diagram.edges[0], label_by_id)
        self.assertEqual(flat, "DFD — Cardholder > skypad — renters --> Stripe — card")


class SearchEvalRegressionTest(unittest.TestCase):
    def test_tuition_entity_matches_flattened_node(self):
        tokens = mi._entity_search_tokens("TProg", "Tuition Program")
        content = (
            mi.node_flatten_text(
                "DFD — Standard Data Flow",
                "GradGuard",
                "TProg",
                "Tuition Program",
            )
            + " · "
            + tokens
        )
        results = [
            {
                "source": "diagrams/mermaid-catalog/standard-data-flow/diagram.mmd",
                "chunk_kind": "mermaid_node",
                "content": content,
            }
        ]
        rank, _ = _score_hit(
            results,
            expect=["diagrams/mermaid-catalog/standard-data-flow/diagram.mmd"],
            expect_chunk_kind="mermaid_node",
            expect_entity="Tuition",
            cut=3,
        )
        self.assertEqual(rank, 1)

    def test_mermaid_header_satisfies_mermaid_file_expect(self):
        results = [
            {
                "source": "d.mmd",
                "chunk_kind": "mermaid_header",
                "content": "DFD > subgraphs: GradGuard, PROC",
            }
        ]
        rank, hit = _score_hit(
            results,
            expect=["d.mmd"],
            expect_chunk_kind="mermaid_file",
            cut=3,
        )
        self.assertEqual(rank, 1)
        self.assertEqual(hit["chunk_kind"], "mermaid_header")


if __name__ == "__main__":
    unittest.main()
