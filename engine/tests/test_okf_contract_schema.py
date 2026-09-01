"""OKF stamp must not mutate machine contract schema YAML files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apo_engine.note_format import is_contract_schema_path
from apo_engine.okf import contract as okf_contract
from apo_engine.okf.stamp import process_concept


class ContractSchemaOkfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        cdir = self.vault / "system" / "contracts"
        cdir.mkdir(parents=True)
        (cdir / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\ncore_required: [okf_type]\n",
            encoding="utf-8",
        )
        self.schema = cdir / "search-contract.schema.yaml"
        self.body = (
            'search_contract_version: "0.1"\n'
            "default_exclude:\n"
            "  - inbox/daily/*\n"
        )
        self.schema.write_text(self.body, encoding="utf-8")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_contract_schema_path(self):
        self.assertTrue(is_contract_schema_path("system/contracts/search-contract.schema.yaml"))
        self.assertFalse(is_contract_schema_path("areas/threads/foo.md"))

    def test_process_concept_skips_contract_schema_yaml(self):
        from apo_engine.okf.contract import OkfContract

        contract = OkfContract(path=self.vault / "system" / "contracts" / "okf-contract.schema.yaml")
        with unittest.mock.patch.object(okf_contract, "get_contract", return_value=contract):
            rel = "system/contracts/search-contract.schema.yaml"
            result = process_concept(
                vault_root=self.vault,
                rel_path=rel,
                content=self.body,
            )
        self.assertEqual(result.content, self.body)
        self.assertEqual(result.stamped, [])
        self.assertEqual(result.enforcement, "off")


if __name__ == "__main__":
    unittest.main()
