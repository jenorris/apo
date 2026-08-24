"""Unit tests for mermaid catalog retrieval boost (scoped + vault-wide)."""

from __future__ import annotations

import unittest

from apo_engine.core import _catalog_retrieval_boost, _is_architecture_query


class ArchitectureQueryTest(unittest.TestCase):
    def test_positive(self):
        self.assertTrue(_is_architecture_query("CDE ECS container Stripe payment"))
        self.assertTrue(_is_architecture_query("cardholder data flow diagram"))
        self.assertTrue(_is_architecture_query("Authorize.net ACH payment processor"))

    def test_negative(self):
        self.assertFalse(_is_architecture_query("quarterly policy review schedule"))
        self.assertFalse(_is_architecture_query("KnowBe4 phishing training completion"))


class CatalogBoostScopedTest(unittest.TestCase):
    def test_diagram_mmd_boosted(self):
        b = _catalog_retrieval_boost(
            "diagrams/mermaid-catalog/cardholder-data-flow/diagram.mmd",
            "mermaid_node",
            "diagrams/mermaid-catalog",
        )
        self.assertGreater(b, 1.2)

    def test_pages_demoted(self):
        b = _catalog_retrieval_boost(
            "diagrams/mermaid-catalog/pages/agency-data-flow.md",
            "section",
            "diagrams/mermaid-catalog",
        )
        self.assertLess(b, 1.0)


class CatalogBoostVaultWideTest(unittest.TestCase):
    def test_no_boost_without_arch_query(self):
        b = _catalog_retrieval_boost(
            "diagrams/mermaid-catalog/cardholder-data-flow/diagram.mmd",
            "mermaid_node",
            "",
            query="quarterly policy review",
        )
        self.assertEqual(b, 1.0)

    def test_boost_with_arch_query(self):
        b = _catalog_retrieval_boost(
            "diagrams/mermaid-catalog/cardholder-data-flow/diagram.mmd",
            "mermaid_node",
            "",
            query="cardholder data flow Stripe",
        )
        self.assertGreater(b, 1.4)

    def test_pages_demoted_vault_wide(self):
        b = _catalog_retrieval_boost(
            "diagrams/mermaid-catalog/pages/agency-data-flow.md",
            "table_row",
            "",
            query="agency cortana data flow diagram",
        )
        self.assertLess(b, 0.5)

    def test_policy_path_untouched(self):
        b = _catalog_retrieval_boost(
            "policies/anti-malware-and-threat-detection.md",
            "section",
            "",
            query="CDE ECS container Stripe payment",
        )
        self.assertEqual(b, 1.0)


if __name__ == "__main__":
    unittest.main()
