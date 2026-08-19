"""Mermaid chunk indexing + search (table analog)."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import config, core, ops

_DIM = 32
FIX = Path(__file__).parent / "fixtures" / "standard-data-flow.mmd"


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


class MermaidIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        catalog_dir = self.vault / "diagrams/mermaid-catalog/standard-data-flow"
        catalog_dir.mkdir(parents=True)
        (catalog_dir / "diagram.mmd").write_text(FIX.read_text(encoding="utf-8"), encoding="utf-8")
        (self.vault / "diagrams/mermaid-catalog/catalog.yaml").write_text(
            yaml.dump(
                {
                    "diagrams": [
                        {
                            "slug": "standard-data-flow",
                            "title": "DFD — Standard Data Flow",
                            "type": "flowchart",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.vault / "system/contracts").mkdir(parents=True, exist_ok=True)
        (self.vault / "system/contracts/mermaid-contract.schema.yaml").write_text(
            (Path(__file__).resolve().parents[2] / "docs/contracts/mermaid-contract.schema.yaml").read_text(
                encoding="utf-8"
            )
            if (Path(__file__).resolve().parents[2] / "docs/contracts/mermaid-contract.schema.yaml").is_file()
            else "mermaid_contract_version: '0.1'\ndiagrams:\n  - match: 'diagrams/mermaid-catalog/**/diagram.mmd'\n    chunk_strategy: nodes_and_edges\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "mermaid_index_test"),
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

    def _rows(self):
        db = core.reader_connect()
        return db.execute(
            "SELECT chunk_kind, row_key, text FROM chunks "
            "WHERE path='diagrams/mermaid-catalog/standard-data-flow/diagram.mmd' ORDER BY ord"
        ).fetchall()

    def test_emits_mermaid_chunks(self):
        kinds = [r[0] for r in self._rows()]
        self.assertIn("mermaid_file", kinds)
        self.assertIn("mermaid_node", kinds)
        self.assertGreaterEqual(kinds.count("mermaid_node"), 5)

    def test_node_flatten_contains_stripe(self):
        nodes = [r for r in self._rows() if r[0] == "mermaid_node" and r[1] == "P"]
        self.assertTrue(nodes)
        self.assertIn("Stripe", nodes[0][2])

    def test_search_finds_stripe_node(self):
        out = ops.search("Stripe payment processor standard data flow", limit=10)
        self.assertTrue(out["ok"], out)
        hits = [r for r in out["results"] if r.get("chunk_kind") == "mermaid_node"]
        self.assertTrue(hits, out["results"])


if __name__ == "__main__":
    unittest.main()
