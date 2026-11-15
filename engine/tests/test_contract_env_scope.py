"""APO_*_CONTRACT env paths apply to a single configured vault only; read-only bundles keep their JSON key."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import okf, okf_cli, vaults
from apo_engine.git_contract import resolve_git_contract_path
from apo_engine.search_contract import resolve_search_contract_path

_DISCOVERY_ENV = (
    "APO_VAULTS",
    "APO_VAULT_PATHS",
    "APO_COLLECTION_ROOT",
    "APO_VAULTS_ROOT",
    "APO_DEFAULT_VAULT",
    "APO_NOTES_ROOT",
    "APO_SEARCH_CONTRACT",
    "APO_GIT_CONTRACT",
    "APO_OKF_CONTRACT",
    "APO_OKF_PROFILE",
)


def _make_vault(root: Path, vault_id: str, *, search_marker: str) -> Path:
    contracts = root / "system" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "usage-contract.schema.yaml").write_text(f"vault_id: {vault_id}\n", encoding="utf-8")
    (contracts / "search-contract.schema.yaml").write_text(
        f"# {search_marker}\nversion: 1\n", encoding="utf-8"
    )
    return contracts / "search-contract.schema.yaml"


class ContractEnvScopeTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in _DISCOVERY_ENV}
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.override = self.base / "override-search.yaml"
        self.override.write_text("# override\nversion: 1\n", encoding="utf-8")
        okf.clear_contract_cache()
        vaults.invalidate_bindings_cache()
        vaults._vault_id_cache.clear()

    def tearDown(self):
        vaults.invalidate_bindings_cache()
        vaults._vault_id_cache.clear()
        okf.clear_contract_cache()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _two_vaults(self) -> tuple[Path, Path, Path, Path]:
        a, b = self.base / "a", self.base / "b"
        own_a = _make_vault(a, "alpha", search_marker="alpha")
        own_b = _make_vault(b, "beta", search_marker="beta")
        os.environ["APO_VAULT_PATHS"] = f"{a}:{b}"
        os.environ["APO_DEFAULT_VAULT"] = "alpha"
        return a, b, own_a, own_b

    def test_two_vaults_ignore_the_env_override(self):
        a, b, own_a, own_b = self._two_vaults()
        os.environ["APO_SEARCH_CONTRACT"] = str(self.override)
        self.assertEqual(resolve_search_contract_path(a), own_a)
        self.assertEqual(resolve_search_contract_path(b), own_b)

    def test_two_vaults_ignore_okf_override(self):
        a, _b, _own_a, _own_b = self._two_vaults()
        okf_own = a / "system" / "contracts" / "okf-contract.schema.yaml"
        okf_own.write_text('okf_version: "0.1"\n', encoding="utf-8")
        os.environ["APO_OKF_CONTRACT"] = str(self.override)
        self.assertEqual(okf.resolve_contract_path(a), okf_own)

    def test_one_vault_still_honors_the_env_override(self):
        a = self.base / "a"
        _make_vault(a, "alpha", search_marker="alpha")
        os.environ["APO_VAULT_PATHS"] = str(a)
        os.environ["APO_SEARCH_CONTRACT"] = str(self.override)
        self.assertEqual(resolve_search_contract_path(a), self.override)

    def test_legacy_single_root_still_honors_the_env_override(self):
        a = self.base / "a"
        _make_vault(a, "alpha", search_marker="alpha")
        os.environ["APO_GIT_CONTRACT"] = str(self.override)
        self.assertEqual(resolve_git_contract_path(a), self.override)

    def test_one_vault_without_override_uses_its_own_contracts(self):
        a = self.base / "a"
        own = _make_vault(a, "alpha", search_marker="alpha")
        os.environ["APO_VAULT_PATHS"] = str(a)
        self.assertEqual(resolve_search_contract_path(a), own)


class ReadOnlyBundleKeyTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in _DISCOVERY_ENV}
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        vaults.invalidate_bindings_cache()
        vaults._vault_id_cache.clear()
        vaults._APO_VAULTS_WARNED = False

    def tearDown(self):
        vaults.invalidate_bindings_cache()
        vaults._vault_id_cache.clear()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _bundle(self) -> Path:
        bundle = self.base / "foreign"
        bundle.mkdir()
        (bundle / "index.md").write_text('---\nokf_version: "0.1"\n---\n\n# Root\n', encoding="utf-8")
        (bundle / "note.md").write_text(
            '---\nokf_type: Note\ndescription: d\ntimestamp: "2026-01-01T00:00:00Z"\n---\n\n# N\n',
            encoding="utf-8",
        )
        return bundle

    def test_ingested_bundle_without_usage_contract_keeps_its_json_key(self):
        bundle = self._bundle()
        self.assertFalse((bundle / "system").exists())
        vaults_file = self.base / "vaults.json"
        out = okf_cli.ingest_bundle(bundle, "foreign", vaults_file=vaults_file, force=True)
        self.assertTrue(out["ok"], out)
        os.environ["APO_VAULTS"] = str(vaults_file)
        default, bindings = vaults.load_bindings(force=True)
        self.assertEqual(sorted(bindings), ["foreign"])
        self.assertEqual(default, "foreign")
        self.assertTrue(bindings["foreign"].read_only)
        self.assertEqual(bindings["foreign"].root, bundle.resolve())

    def test_usage_contract_vault_id_still_wins_over_json_key(self):
        bundle = self._bundle()
        _make_vault(bundle, "real-id", search_marker="x")
        vaults_file = self.base / "vaults.json"
        vaults_file.write_text(
            json.dumps({"vaults": {"json-key": {"root": str(bundle), "read_only": True}}}),
            encoding="utf-8",
        )
        os.environ["APO_VAULTS"] = str(vaults_file)
        _default, bindings = vaults.load_bindings(force=True)
        self.assertEqual(sorted(bindings), ["real-id"])

    def test_apo_disabled_bundle_is_still_rejected(self):
        bundle = self._bundle()
        (bundle / "system" / "contracts").mkdir(parents=True)
        (bundle / "system" / "contracts" / ".apo-disabled").write_text("", encoding="utf-8")
        vaults_file = self.base / "vaults.json"
        vaults_file.write_text(
            json.dumps({"vaults": {"k": {"root": str(bundle), "read_only": True}}}),
            encoding="utf-8",
        )
        os.environ["APO_VAULTS"] = str(vaults_file)
        with self.assertRaises(ValueError):
            vaults.load_bindings(force=True)


if __name__ == "__main__":
    unittest.main()
