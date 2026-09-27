"""Unit tests for mermaid catalog retrieval boost (scoped + vault-wide) and the
per-vault ``boost_vocab`` architecture-query mechanism that backs the vault-wide
branch — see ``apo_engine.ranking`` and ``apo_engine.search_contract``.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import config, ranking
from apo_engine.ranking import _catalog_retrieval_boost, _is_architecture_query

# Mirrors the vocabulary the old hardcoded _ARCH_QUERY_RE carried for the
# GradGuard ("Work") vault, now expected to live in that vault's own
# search-contract.schema.yaml under boost_vocab.
_ARCH_VOCAB = [
    "cde",
    "chd",
    "pci",
    "pan",
    "cardholder",
    "stripe",
    "ecs",
    "skypad",
    "faber",
    "cortana",
    "starrez",
    "authorize.net",
    "segmentation",
    "architecture",
    "data flow",
    "network diagram",
    "payment processor",
    "diagram.mmd",
    "mermaid",
]


def _write_contract(vault: Path, *, boost_vocab: list[str] | None = None) -> None:
    contract_dir = vault / "system" / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"search_contract_version": "0.1"}
    if boost_vocab is not None:
        data["boost_vocab"] = boost_vocab
    (contract_dir / "search-contract.schema.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


class _VaultVocabCase(unittest.TestCase):
    """Shared per-test tmp vault + NOTES_ROOT patch + vocab cache reset."""

    boost_vocab: list[str] | None = None
    write_contract = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-arch-vocab-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        if self.write_contract:
            _write_contract(self.vault, boost_vocab=self.boost_vocab)
        self._patch = mock.patch.object(config, "NOTES_ROOT", self.vault)
        self._patch.start()
        ranking.clear_architecture_vocab_cache()

    def tearDown(self):
        self._patch.stop()
        ranking.clear_architecture_vocab_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ArchitectureQueryTest(_VaultVocabCase):
    boost_vocab = _ARCH_VOCAB

    def test_positive(self):
        self.assertTrue(_is_architecture_query("CDE ECS container Stripe payment"))
        self.assertTrue(_is_architecture_query("cardholder data flow diagram"))
        self.assertTrue(_is_architecture_query("Authorize.net ACH payment processor"))

    def test_negative(self):
        self.assertFalse(_is_architecture_query("quarterly policy review schedule"))
        self.assertFalse(_is_architecture_query("KnowBe4 phishing training completion"))


class ArchitectureQueryNoVocabTest(unittest.TestCase):
    """A vault with no boost_vocab (or no contract at all) gets no architecture
    boost — not a crash, not a silent fallback to some other vault's vocabulary."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-arch-vocab-none-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._patch = mock.patch.object(config, "NOTES_ROOT", self.vault)
        self._patch.start()
        ranking.clear_architecture_vocab_cache()

    def tearDown(self):
        self._patch.stop()
        ranking.clear_architecture_vocab_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_contract_at_all_no_boost(self):
        self.assertFalse(_is_architecture_query("CDE ECS container Stripe payment"))
        self.assertFalse(_is_architecture_query("cardholder data flow diagram"))

    def test_contract_without_boost_vocab_no_boost(self):
        _write_contract(self.vault)  # contract present, no boost_vocab key
        ranking.clear_architecture_vocab_cache()
        self.assertFalse(_is_architecture_query("CDE ECS container Stripe payment"))

    def test_contract_with_empty_boost_vocab_no_boost(self):
        _write_contract(self.vault, boost_vocab=[])
        ranking.clear_architecture_vocab_cache()
        self.assertFalse(_is_architecture_query("CDE ECS container Stripe payment"))


class CatalogBoostScopedTest(unittest.TestCase):
    """Folder-scoped catalog boost doesn't consult boost_vocab at all — no vault
    fixture needed here."""

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


class CatalogBoostVaultWideTest(_VaultVocabCase):
    """Vault-wide catalog boost needs an architecture-query hit — give the fixture
    vault the same boost_vocab the old hardcoded regex carried."""

    boost_vocab = _ARCH_VOCAB

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


class CatalogBoostVaultWideNoVocabTest(unittest.TestCase):
    """Same vault-wide queries, but the vault has no boost_vocab — must never
    boost, unlike the old always-on hardcoded regex."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-catalog-novocab-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._patch = mock.patch.object(config, "NOTES_ROOT", self.vault)
        self._patch.start()
        ranking.clear_architecture_vocab_cache()

    def tearDown(self):
        self._patch.stop()
        ranking.clear_architecture_vocab_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_boost_even_for_old_arch_terms(self):
        b = _catalog_retrieval_boost(
            "diagrams/mermaid-catalog/cardholder-data-flow/diagram.mmd",
            "mermaid_node",
            "",
            query="cardholder data flow Stripe skypad faber",
        )
        self.assertEqual(b, 1.0)


if __name__ == "__main__":
    unittest.main()
