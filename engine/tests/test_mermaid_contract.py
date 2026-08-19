"""Mermaid contract catalog join."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine.mermaid_contract import catalog_entry_for, chunk_strategy_for
from apo_engine.mermaid_index import merge_frontmatter_for_mmd


class MermaidContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        cat_dir = self.vault / "diagrams/mermaid-catalog/foo"
        cat_dir.mkdir(parents=True)
        (cat_dir / "diagram.mmd").write_text("flowchart LR\n  A --> B\n", encoding="utf-8")
        (self.vault / "diagrams/mermaid-catalog/catalog.yaml").write_text(
            yaml.dump({"diagrams": [{"slug": "foo", "title": "Foo DFD", "type": "flowchart"}]}),
            encoding="utf-8",
        )
        contract = self.vault / "system/contracts/mermaid-contract.schema.yaml"
        contract.parent.mkdir(parents=True)
        contract.write_text(
            (Path(__file__).resolve().parents[2] / "docs/contracts/mermaid-contract.schema.yaml").read_text(),
            encoding="utf-8",
        )
        self._patch = mock.patch("apo_engine.vaults.notes_root", lambda: self.vault)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_diagram_id_equals_slug(self):
        entry = catalog_entry_for(self.vault, "diagrams/mermaid-catalog/foo/diagram.mmd")
        self.assertEqual(entry.get("diagram_id"), "foo")

    def test_merge_frontmatter(self):
        fm = merge_frontmatter_for_mmd("flowchart LR\n  A --> B\n", "diagrams/mermaid-catalog/foo/diagram.mmd")
        self.assertEqual(fm.get("diagram_id"), "foo")
        self.assertEqual(fm.get("okf_type"), "Diagram")

    def test_default_strategy(self):
        self.assertEqual(
            chunk_strategy_for(self.vault, "diagrams/mermaid-catalog/foo/diagram.mmd"),
            "nodes_and_edges",
        )


if __name__ == "__main__":
    unittest.main()
