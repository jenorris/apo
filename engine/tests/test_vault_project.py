"""First-class coverage for apo_engine.vault_project's pure logic and
change-detection helpers that aren't already exercised by test_vault_tool.py
(rendering) or test_watch_idle_cost.py (maybe_reproject's debounce timing,
with _contracts_signature mocked out).

Covers: is_contracts_rel, _pointer_vault_id/_abs_pointer, scope_desk_overlay,
and _contracts_signature/_desk_mtime against a real filesystem (not mocked).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from apo_engine import vault_project


class IsContractsRelTest(unittest.TestCase):
    def test_matches_dir_and_children(self):
        self.assertTrue(vault_project.is_contracts_rel("system/contracts"))
        self.assertTrue(vault_project.is_contracts_rel("system/contracts/git-contract.schema.yaml"))
        self.assertTrue(vault_project.is_contracts_rel("/system/contracts/x.yaml"))

    def test_normalizes_backslashes(self):
        self.assertTrue(vault_project.is_contracts_rel("system\\contracts\\x.yaml"))

    def test_rejects_siblings_and_prefixed_lookalikes(self):
        self.assertFalse(vault_project.is_contracts_rel("system/config/x.yaml"))
        self.assertFalse(vault_project.is_contracts_rel("system/contracts-old/x.yaml"))
        self.assertFalse(vault_project.is_contracts_rel(""))
        self.assertFalse(vault_project.is_contracts_rel("areas/threads/system/contracts/x.yaml"))


class PointerHelpersTest(unittest.TestCase):
    def test_pointer_vault_id_extracts_prefix(self):
        self.assertEqual(vault_project._pointer_vault_id("meta:policy/x.md"), "meta")

    def test_pointer_vault_id_none_for_urls_and_bare_paths(self):
        self.assertIsNone(vault_project._pointer_vault_id("https://example.com/x"))
        self.assertIsNone(vault_project._pointer_vault_id("/abs/path.md"))
        self.assertIsNone(vault_project._pointer_vault_id("no-colon-here"))
        self.assertIsNone(vault_project._pointer_vault_id(""))

    def test_abs_pointer_expands_known_vault_and_adds_md_suffix(self):
        vaults = {"meta": {"root": "/home/j/vault"}}
        self.assertEqual(
            vault_project._abs_pointer("meta:policy/agent-memory", vaults),
            "/home/j/vault/policy/agent-memory.md",
        )

    def test_abs_pointer_leaves_unknown_vault_and_urls_and_bare_paths_untouched(self):
        vaults = {"meta": {"root": "/home/j/vault"}}
        self.assertEqual(vault_project._abs_pointer("other:x.md", vaults), "other:x.md")
        self.assertEqual(
            vault_project._abs_pointer("https://example.com/x", vaults), "https://example.com/x"
        )
        self.assertEqual(vault_project._abs_pointer("/abs/x.md", vaults), "/abs/x.md")
        self.assertEqual(vault_project._abs_pointer("no-colon", vaults), "no-colon")


class ScopeDeskOverlayTest(unittest.TestCase):
    def test_keeps_only_role_notes_for_active_roles(self):
        desk = {
            "role_notes": {"personal": "note-a", "family": "note-b"},
            "pointers": {},
        }
        vaults = {"atlas": {"role": "personal"}}
        out = vault_project.scope_desk_overlay(desk, vaults)
        self.assertEqual(out["role_notes"], {"personal": "note-a"})

    def test_keeps_only_pointers_for_active_vaults_and_non_vault_pointers(self):
        desk = {
            "role_notes": {},
            "pointers": {
                "in_scope": "atlas:policy/x.md",
                "out_of_scope": "keith:family/y.md",
                "absolute": "/abs/z.md",
                "url": "https://example.com/z",
            },
        }
        vaults = {"atlas": {"role": "personal"}}
        out = vault_project.scope_desk_overlay(desk, vaults)
        self.assertEqual(
            out["pointers"],
            {
                "in_scope": "atlas:policy/x.md",
                "absolute": "/abs/z.md",
                "url": "https://example.com/z",
            },
        )

    def test_missing_role_notes_or_pointers_pass_through_unchanged(self):
        desk = {"other_key": "kept"}
        out = vault_project.scope_desk_overlay(desk, {})
        self.assertEqual(out, {"other_key": "kept"})


class DeskMtimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev = os.environ.get("APO_DESK_CONFIG")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev is None:
            os.environ.pop("APO_DESK_CONFIG", None)
        else:
            os.environ["APO_DESK_CONFIG"] = self._prev

    def test_none_when_no_desk_file_configured(self):
        os.environ["APO_DESK_CONFIG"] = str(Path(self.tmp.name) / "nope.yaml")
        self.assertIsNone(vault_project._desk_mtime())

    def test_returns_real_mtime_and_tracks_touch(self):
        desk = Path(self.tmp.name) / "desk.yaml"
        desk.write_text("vaults: {}\n", encoding="utf-8")
        os.environ["APO_DESK_CONFIG"] = str(desk)
        first = vault_project._desk_mtime()
        self.assertIsNotNone(first)
        self.assertAlmostEqual(first, desk.stat().st_mtime)

        # Bump mtime forward — a real edit must change what this reports.
        new_mtime = (desk.stat().st_mtime or 0) + 5
        os.utime(desk, (new_mtime, new_mtime))
        self.assertNotEqual(vault_project._desk_mtime(), first)


class ContractsSignatureTest(unittest.TestCase):
    """Exercises the real (unmocked) filesystem walk `maybe_reproject` relies on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "system" / "contracts").mkdir(parents=True)
        (self.root / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\nvault_id: sig_test\npurpose: sig test\n",
            encoding="utf-8",
        )
        self.vaults_file = Path(self.tmp.name) / "vaults.json"
        self.vaults_file.write_text(
            json.dumps(
                {
                    "default": "sig_test",
                    "vaults": {
                        "sig_test": {
                            "root": str(self.root),
                            "index": str(Path(self.tmp.name) / "sig.db"),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self._prev_apo_vaults = os.environ.get("APO_VAULTS")
        os.environ["APO_VAULTS"] = str(self.vaults_file)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev_apo_vaults is None:
            os.environ.pop("APO_VAULTS", None)
        else:
            os.environ["APO_VAULTS"] = self._prev_apo_vaults

    def test_empty_when_no_registered_vault_has_a_contracts_dir(self):
        empty_root = Path(self.tmp.name) / "no_contracts_vault"
        empty_root.mkdir()
        with unittest.mock.patch(
            "apo_engine.vaults.load_bindings",
            return_value=("x", {}),
        ):
            self.assertEqual(vault_project._contracts_signature(), "")

    def test_changes_when_a_contract_file_is_edited(self):
        before = vault_project._contracts_signature()
        self.assertIn("sig_test", before)
        self.assertIn("usage-contract.schema.yaml", before)

        contract = self.root / "system" / "contracts" / "usage-contract.schema.yaml"
        contract.write_text(contract.read_text(encoding="utf-8") + "extra: 1\n", encoding="utf-8")
        after = vault_project._contracts_signature()
        self.assertNotEqual(before, after)

    def test_stable_when_nothing_changed(self):
        first = vault_project._contracts_signature()
        second = vault_project._contracts_signature()
        self.assertEqual(first, second)

    def test_ignores_non_yaml_files_in_contracts_dir(self):
        (self.root / "system" / "contracts" / "README.md").write_text("n/a", encoding="utf-8")
        before = vault_project._contracts_signature()
        (self.root / "system" / "contracts" / "README.md").write_text("changed", encoding="utf-8")
        after = vault_project._contracts_signature()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
