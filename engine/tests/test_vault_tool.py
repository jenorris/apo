"""vault tool — list / contracts / describe + system/contracts discovery."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from apo_engine import ops, rpc, vault_contracts


class ContractIdTest(unittest.TestCase):
    def test_id_from_schema_yaml(self):
        self.assertEqual(
            vault_contracts.contract_id_from_name("okf-contract.schema.yaml"),
            "okf-contract",
        )
        self.assertEqual(
            vault_contracts.contract_id_from_name("usage.yaml"),
            "usage",
        )


class DiscoverContractsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-vcontracts-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefers_system_contracts_over_legacy(self):
        legacy = self.vault / "system" / "config"
        legacy.mkdir(parents=True)
        (legacy / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: legacy\n", encoding="utf-8"
        )
        preferred = self.vault / "system" / "contracts"
        preferred.mkdir(parents=True)
        (preferred / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: contracts\n", encoding="utf-8"
        )
        found = vault_contracts.discover_contracts(self.vault)
        self.assertIn("okf-contract", found)
        self.assertEqual(found["okf-contract"]["source"], "contracts")
        self.assertEqual(found["okf-contract"]["data"].get("from"), "contracts")

    def test_legacy_git_and_okf_profile(self):
        cfg = self.vault / "system" / "config"
        cfg.mkdir(parents=True)
        (cfg / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: 'https://x.git'\n",
            encoding="utf-8",
        )
        (cfg / "okf-profile.schema.yaml").write_text(
            "okf_version: '0.1'\n", encoding="utf-8"
        )
        found = vault_contracts.discover_contracts(self.vault)
        self.assertEqual(set(found), {"git-contract", "okf-profile"})
        self.assertTrue(all(e["source"] == "legacy" for e in found.values()))
        self.assertTrue(all(e["ok"] for e in found.values()))

    def test_invalid_yaml_marks_entry_not_ok(self):
        cdir = self.vault / "system" / "contracts"
        cdir.mkdir(parents=True)
        (cdir / "broken.schema.yaml").write_text(":\n  - bad\n", encoding="utf-8")
        found = vault_contracts.discover_contracts(self.vault)
        self.assertFalse(found["broken"]["ok"])
        self.assertIn("error", found["broken"])


class VaultOpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-vault-op-"))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        self.a.mkdir()
        self.b.mkdir()
        (self.a / "inbox").mkdir()
        cdir = self.a / "system" / "contracts"
        cdir.mkdir(parents=True)
        (cdir / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\npurpose: test\n",
            encoding="utf-8",
        )
        cfg = self.b / "system" / "config"
        cfg.mkdir(parents=True)
        (cfg / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: 'https://b.git'\n",
            encoding="utf-8",
        )
        registry = {
            "default": "alpha",
            "vaults": {
                "alpha": {
                    "root": str(self.a),
                    "index": str(self.tmp / "a.db"),
                    "collection": "alpha",
                },
                "beta": {
                    "root": str(self.b),
                    "index": str(self.tmp / "b.db"),
                    "collection": "beta",
                },
            },
        }
        self.reg_path = self.tmp / "vaults.json"
        self.reg_path.write_text(json.dumps(registry), encoding="utf-8")
        self._env = unittest.mock.patch.dict(
            os.environ,
            {
                "APO_VAULTS": str(self.reg_path),
                "APO_NOTES_ROOT": str(self.a),
                "APO_INDEX": str(self.tmp / "legacy.db"),
                "APO_COLLECTION": "legacy",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list(self):
        out = ops.vault_op("list")
        self.assertTrue(out["ok"])
        self.assertEqual(out["default_vault"], "alpha")
        self.assertEqual(set(out["vaults"]), {"alpha", "beta"})
        self.assertTrue(out["vaults"]["alpha"]["default"])
        self.assertIn("inbox", out["vaults"]["alpha"]["top_level_dirs"])
        ids = {c["id"] for c in out["vaults"]["alpha"]["contracts"]}
        self.assertEqual(ids, {"usage-contract"})
        ids_b = {c["id"] for c in out["vaults"]["beta"]["contracts"]}
        self.assertEqual(ids_b, {"git-contract"})

    def test_contracts_all_and_one(self):
        all_out = ops.vault_op("contracts")
        self.assertTrue(all_out["ok"])
        self.assertIn("alpha", all_out["vaults"])
        self.assertIn(
            "usage-contract", all_out["vaults"]["alpha"]["contracts"]
        )
        one = ops.vault_op("contracts", vault="beta")
        self.assertEqual(one["vault"], "beta")
        self.assertIn("git-contract", one["contracts"])
        self.assertEqual(
            one["contracts"]["git-contract"]["data"]["remote"],
            "https://b.git",
        )

    def test_describe_default(self):
        out = ops.vault_op("describe")
        self.assertTrue(out["ok"])
        self.assertEqual(out["vault"], "alpha")
        self.assertTrue(out["default"])
        self.assertEqual(out["contract_ids"], ["usage-contract"])
        self.assertEqual(
            out["contracts"]["usage-contract"]["data"]["purpose"], "test"
        )

    def test_bad_action(self):
        out = ops.vault_op("merge")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_action")


class VaultRpcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-vault-rpc-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._env = unittest.mock.patch.dict(
            os.environ,
            {
                "APO_VAULTS": "",
                "APO_NOTES_ROOT": str(self.vault),
                "APO_INDEX": str(self.tmp / "index.db"),
                "APO_COLLECTION": "vault_rpc_test",
            },
            clear=False,
        )
        self._env.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), rpc.RpcHandler)
        self.httpd.RequestHandlerClass.rpc_token = ""
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_rpc_list(self):
        out = self._post("/v1/vault", {"action": "list"})
        self.assertTrue(out["ok"])
        self.assertIn("default", out["vaults"])


if __name__ == "__main__":
    unittest.main()
