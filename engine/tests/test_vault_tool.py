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
            "usage_contract_version: '0.1'\n"
            "purpose: test\n"
            "contribution:\n"
            "  dialect: obsidian-ofm\n"
            "  features:\n"
            "    callouts: preferred\n"
            "  surfaces:\n"
            "    session_log:\n"
            "      dialect: gfm\n"
            "      callouts: never\n"
            "  render:\n"
            "    profile: htmlize\n"
            "  pointers:\n"
            "    - alpha:system/config/obsidian-callouts\n",
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
        self.assertFalse(all_out["full"])
        self.assertIn("alpha", all_out["vaults"])
        alpha_c = all_out["vaults"]["alpha"]["contracts"]["usage-contract"]
        self.assertNotIn("data", alpha_c)
        self.assertEqual(alpha_c["path"], "system/contracts/usage-contract.schema.yaml")
        one = ops.vault_op("contracts", vault="beta")
        self.assertEqual(one["vault"], "beta")
        self.assertIn("git-contract", one["contracts"])
        self.assertNotIn("data", one["contracts"]["git-contract"])
        full = ops.vault_op("contracts", vault="beta", full=True)
        self.assertTrue(full["full"])
        self.assertEqual(
            full["contracts"]["git-contract"]["data"]["remote"],
            "https://b.git",
        )

    def test_describe_default(self):
        out = ops.vault_op("describe")
        self.assertTrue(out["ok"])
        self.assertEqual(out["vault"], "alpha")
        self.assertTrue(out["default"])
        self.assertEqual(out["contract_ids"], ["usage-contract"])
        self.assertNotIn("data", out["contracts"]["usage-contract"])
        full = ops.vault_op("describe", full=True)
        self.assertEqual(
            full["contracts"]["usage-contract"]["data"]["purpose"], "test"
        )

    def test_bad_action(self):
        out = ops.vault_op("explode")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_action")

    def test_project_dry_and_write(self):
        desk = self.tmp / "desk.yaml"
        desk.write_text(
            "desk_version: '0.1'\n"
            "citations: absolute_markdown\n"
            "workspace: '/tmp/desk.code-workspace'\n"
            "dual_write:\n"
            "  session_vault: sessions\n"
            "vault_roles:\n"
            "  alpha: pkb\n"
            "  beta: employer\n"
            "pointers:\n"
            "  memory_policy: 'alpha:system/config/policy'\n"
            "habits:\n"
            "  new_durable_facts: true\n",
            encoding="utf-8",
        )
        out_dir = self.tmp / "projected"
        cursor_out = out_dir / "apo-desk.mdc"
        claude_out = out_dir / "claude" / "SKILL.md"
        with unittest.mock.patch.dict(
            os.environ,
            {
                "APO_DESK_CONFIG": str(desk),
                "APO_PROJECT_CURSOR": str(cursor_out),
                "APO_PROJECT_CLAUDE": str(claude_out),
            },
        ):
            dry = ops.vault_op("project", host="cursor", write=False)
            self.assertTrue(dry["ok"])
            text = dry["files"]["cursor"]["text"]
            self.assertIn("alwaysApply: true", text)
            self.assertIn("`alpha`", text)
            self.assertIn("pkb", text)
            self.assertIn("/tmp/desk.code-workspace", text)
            self.assertIn("Do not hand-edit", text)
            # pointer expanded using alpha root
            self.assertIn(str(self.a), text)
            # contribution from usage-contract body
            self.assertIn("## Contribution", text)
            self.assertIn("obsidian-ofm", text)
            self.assertIn("callouts preferred", text)
            self.assertIn("session_log=gfm", text)
            self.assertIn("render `htmlize`", text)
            self.assertIn("`alpha`: `obsidian-ofm`", text)

            written = ops.vault_op("project", host="both", write=True)
            self.assertTrue(written["ok"])
            self.assertTrue(written["files"]["cursor"]["written"])
            self.assertTrue(written["files"]["claude"]["written"])
            self.assertTrue(cursor_out.is_file())
            self.assertTrue(claude_out.is_file())
            claude_body = claude_out.read_text(encoding="utf-8")
            self.assertIn("name: apo-desk", claude_body)
            self.assertNotIn("alwaysApply", claude_body)

            hermes_out = out_dir / "hermes" / "SKILL.md"
            with unittest.mock.patch.dict(
                os.environ,
                {"APO_PROJECT_HERMES": str(hermes_out)},
            ):
                hermes = ops.vault_op("project", host="hermes", write=True)
            self.assertTrue(hermes["ok"], hermes)
            self.assertTrue(hermes["files"]["hermes"]["written"])
            hermes_body = hermes_out.read_text(encoding="utf-8")
            self.assertIn("name: apo-desk", hermes_body)
            self.assertIn("Mnemosyne", hermes_body)
            self.assertNotIn("alwaysApply", hermes_body)

            all_hosts = ops.vault_op("project", host="all", write=False)
            self.assertTrue(all_hosts["ok"], all_hosts)
            self.assertEqual(
                set(all_hosts["files"]), {"cursor", "claude", "hermes"}
            )

    def test_merge_with_desk_file(self):
        desk = self.tmp / "desk.yaml"
        desk.write_text(
            "desk_version: '0.1'\n"
            "cross_pollinate_contracts: false\n"
            "citations: absolute_markdown\n"
            "dual_write:\n"
            "  session_vault: sessions\n"
            "vault_roles:\n"
            "  alpha: pkb\n"
            "  beta: employer\n",
            encoding="utf-8",
        )
        with unittest.mock.patch.dict(os.environ, {"APO_DESK_CONFIG": str(desk)}):
            out = ops.vault_op("merge")
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "merge")
        self.assertFalse(out["full"])
        self.assertEqual(out["desk"]["citations"], "absolute_markdown")
        self.assertFalse(out["merge_rules"]["cross_pollinate_contracts"])
        self.assertEqual(out["vaults"]["alpha"]["role"], "pkb")
        self.assertEqual(out["vaults"]["beta"]["role"], "employer")
        self.assertIn("usage-contract", out["vaults"]["alpha"]["contract_ids"])
        self.assertNotIn("data", out["vaults"]["alpha"]["contracts"]["usage-contract"])
        self.assertEqual(out["desk_meta"]["source"], "file")
        self.assertIn("habits", out["desk"])
        self.assertIn("pointers", out["merge_rules"]["desk_overlay_keys"])
        full = ops.vault_op("merge", full=True)
        self.assertTrue(full["full"])
        self.assertEqual(
            full["vaults"]["alpha"]["contracts"]["usage-contract"]["data"]["purpose"],
            "test",
        )

    def test_merge_defaults_without_desk(self):
        missing = self.tmp / "no-such-desk.yaml"
        with unittest.mock.patch.dict(os.environ, {"APO_DESK_CONFIG": str(missing)}):
            # resolve_desk_path returns None when explicit path missing
            with unittest.mock.patch(
                "apo_engine.vault_desk.resolve_desk_path", return_value=None
            ):
                out = ops.vault_op("merge")
        self.assertTrue(out["ok"])
        self.assertEqual(out["desk_meta"]["source"], "defaults")
        self.assertEqual(out["desk"]["dual_write"]["session_vault"], "sessions")
        self.assertNotIn("role", out["vaults"]["alpha"])


class PreferContractsDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-prefer-contracts-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_okf_and_git_prefer_contracts_dir(self):
        from apo_engine import git_contract, okf

        legacy = self.vault / "system" / "config"
        preferred = self.vault / "system" / "contracts"
        legacy.mkdir(parents=True)
        preferred.mkdir(parents=True)
        (legacy / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: legacy\n", encoding="utf-8"
        )
        (preferred / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: contracts\n", encoding="utf-8"
        )
        (legacy / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: legacy\n", encoding="utf-8"
        )
        (preferred / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: contracts\n", encoding="utf-8"
        )
        self.assertEqual(
            okf.resolve_contract_path(self.vault),
            preferred / "okf-contract.schema.yaml",
        )
        self.assertEqual(
            git_contract.resolve_git_contract_path(self.vault),
            preferred / "git-contract.schema.yaml",
        )


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
