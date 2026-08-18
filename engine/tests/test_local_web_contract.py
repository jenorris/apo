"""local-web-contract loader — per-vault ``just serve`` config discovery."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from apo_engine import local_web_contract


class LocalWebContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-local-web-contract-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_contract_returns_none(self):
        self.assertIsNone(local_web_contract.resolve_local_web_contract_path(self.vault))
        self.assertIsNone(local_web_contract.load_local_web_contract(self.vault))

    def test_loads_from_system_contracts(self):
        cdir = self.vault / "system" / "contracts"
        cdir.mkdir(parents=True)
        (cdir / "local-web-contract.schema.yaml").write_text(
            "local_web_version: '0.2'\nbind: 127.0.0.1\nport: 7432\nmode: adaptive\n",
            encoding="utf-8",
        )
        path = local_web_contract.resolve_local_web_contract_path(self.vault)
        self.assertEqual(path, cdir / "local-web-contract.schema.yaml")
        data = local_web_contract.load_local_web_contract(self.vault)
        self.assertEqual(data["port"], 7432)
        self.assertEqual(data["mode"], "adaptive")

    def test_legacy_system_config_path_still_loads(self):
        cdir = self.vault / "system" / "config"
        cdir.mkdir(parents=True)
        (cdir / "local-web-contract.schema.yaml").write_text(
            "local_web_version: '0.2'\nport: 9000\n", encoding="utf-8"
        )
        data = local_web_contract.load_local_web_contract(self.vault)
        self.assertEqual(data["port"], 9000)

    def test_system_contracts_wins_over_legacy(self):
        preferred = self.vault / "system" / "contracts"
        preferred.mkdir(parents=True)
        (preferred / "local-web-contract.schema.yaml").write_text(
            "port: 1111\n", encoding="utf-8"
        )
        legacy = self.vault / "system" / "config"
        legacy.mkdir(parents=True)
        (legacy / "local-web-contract.schema.yaml").write_text(
            "port: 2222\n", encoding="utf-8"
        )
        data = local_web_contract.load_local_web_contract(self.vault)
        self.assertEqual(data["port"], 1111)


if __name__ == "__main__":
    unittest.main()
