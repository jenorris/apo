"""Optima contract loader + Stage B merge settings."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from apo_engine import optima_contract


def _write_optima_contract(vault: Path, body: dict) -> None:
    path = vault / optima_contract.OPTIMA_CONTRACT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


class OptimaContractTest(unittest.TestCase):
    def tearDown(self) -> None:
        optima_contract.clear_optima_contract_cache()

    def test_missing_contract_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(optima_contract.load_optima_contract(root))
            self.assertFalse(optima_contract.merge_enabled(root))
            settings = optima_contract.merge_settings(root)
            self.assertFalse(settings.enabled)

    def test_watch_enabled_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_optima_contract(
                root,
                {
                    "optima_contract_version": "0.9.0",
                    "vault_id": "optima",
                    "known_paths": {"current": "current.yaml"},
                    "refresh": {
                        "opt_out_env": "OPTIMA_SYNC",
                        "watch": {"enabled": True, "interval_seconds": 45},
                        "sources": [
                            {
                                "id": "work_theme",
                                "vault": "work",
                                "path": "areas/schedule/current.md",
                                "role": "work_theme",
                                "if_missing": "skip",
                            },
                            {
                                "id": "life_presence",
                                "vault": "atlas",
                                "path": "areas/schedule/current.yaml",
                                "role": "life_presence",
                                "if_missing": "skip",
                            },
                        ],
                        "local": {"override": "override.yaml", "if_missing": "skip"},
                        "on_all_sources_missing": "degrade_to_free_or_habit",
                        "output": {"current": "current.yaml"},
                        "reachability_rules": "system/config/reachability-rules.yaml",
                    },
                },
            )
            settings = optima_contract.merge_settings(root)
            self.assertTrue(settings.enabled)
            self.assertEqual(settings.interval_seconds, 45.0)
            self.assertEqual(len(settings.sources), 2)
            self.assertEqual(settings.sources[0].vault, "work")
            self.assertTrue(optima_contract.merge_enabled(root))

    def test_opt_out_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_optima_contract(
                root,
                {
                    "refresh": {
                        "watch": {"enabled": True},
                        "opt_out_env": "OPTIMA_SYNC",
                    }
                },
            )
            self.assertTrue(optima_contract.merge_enabled(root))
            import os

            os.environ["OPTIMA_SYNC"] = "0"
            try:
                self.assertFalse(optima_contract.merge_enabled(root))
            finally:
                del os.environ["OPTIMA_SYNC"]

    def test_mtime_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_optima_contract(root, {"refresh": {"watch": {"enabled": False}}})
            a = optima_contract.load_optima_contract(root)
            b = optima_contract.load_optima_contract(root)
            self.assertEqual(a, b)
            # Second load should be cache hit (same content)
            self.assertIsNotNone(a)


if __name__ == "__main__":
    unittest.main()
