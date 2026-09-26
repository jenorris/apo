"""mcp/server.py's Vault must carry every apo_vaults.VaultBinding field.

Vault used to hand-copy VaultBinding fields one by one into its own
constructor (name/root/collection/index_path) — silently dropping
`read_only` since that field didn't exist on Vault at all. Any future field
added to VaultBinding was one hand-copy away from the same silent drop. Vault
now wraps the binding whole (dataclasses.replace, not field-by-field copy),
so this can't regress.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
_SRC = _ENGINE / "src"
_SERVER = _SRC / "apo_engine" / "mcp" / "server.py"


def _load_server_module(*, collection: str):
    src = str(_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    saved = {}
    for name in list(sys.modules):
        if name == "apo_engine" or name.startswith("apo_engine."):
            saved[name] = sys.modules[name]
            del sys.modules[name]
    try:
        spec = importlib.util.spec_from_file_location(f"apo_mcp_{collection}", _SERVER)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.update(saved)


class VaultCarriesReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "system" / "contracts").mkdir(parents=True)
        (self.root / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\nvault_id: ro_mcp\npurpose: mcp read-only test\n",
            encoding="utf-8",
        )
        self.vaults_file = Path(self.tmp.name) / "vaults.json"
        self.vaults_file.write_text(
            json.dumps(
                {
                    "default": "ro_mcp",
                    "vaults": {
                        "ro_mcp": {
                            "root": str(self.root),
                            "index": str(Path(self.tmp.name) / "ro_mcp.db"),
                            "read_only": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self._env = {
            k: os.environ.get(k)
            for k in ("APO_VAULTS", "APO_NOTES_ROOT", "APO_INDEX", "APO_COLLECTION")
        }
        os.environ["APO_VAULTS"] = str(self.vaults_file)
        os.environ.pop("APO_NOTES_ROOT", None)
        os.environ.pop("APO_INDEX", None)
        os.environ["APO_COLLECTION"] = "ro_mcp_test"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_vault_read_only_matches_registry(self):
        mod = _load_server_module(collection="ro_mcp_test")
        v = mod.VAULTS["ro_mcp"]
        self.assertTrue(v.read_only)
        self.assertEqual(v.name, "ro_mcp")
        self.assertEqual(v.root, self.root.resolve())


if __name__ == "__main__":
    unittest.main()
