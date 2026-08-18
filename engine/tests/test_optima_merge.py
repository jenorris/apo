"""Optima Stage B merge — build_merged + run_merge."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from apo_engine import optima_contract, optima_merge, vaults

ET = ZoneInfo("America/New_York")


def _write_usage(root: Path, vault_id: str) -> None:
    cdir = root / "system" / "contracts"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "usage-contract.schema.yaml").write_text(
        f"vault_id: {vault_id}\n", encoding="utf-8"
    )


def _write_optima_contract(vault: Path, *, enabled: bool = True) -> None:
    path = vault / optima_contract.OPTIMA_CONTRACT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "optima_contract_version": "0.9.0",
        "vault_id": "optima",
        "known_paths": {
            "current": "current.yaml",
            "override": "override.yaml",
            "reachability_rules": "system/config/reachability-rules.yaml",
        },
        "refresh": {
            "opt_out_env": "OPTIMA_SYNC",
            "watch": {"enabled": enabled, "interval_seconds": 60},
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
    }
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


class BuildMergedTest(unittest.TestCase):
    def test_work_meeting_wins(self) -> None:
        now = datetime(2026, 8, 17, 12, 0, tzinfo=ET)
        life = {
            "kind": "personal",
            "theme": "Lunch",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
        }
        work = {
            "kind": "meeting",
            "theme": "Standup",
            "threads": ["plat-1"],
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        }
        rules = {
            "by_kind": {
                "meeting": {
                    "slack": "defer",
                    "email": "ok",
                    "telegram": "ok",
                    "phone": "no",
                    "rationale": "meeting",
                }
            }
        }
        merged = optima_merge.build_merged(life, work, rules, now=now)
        self.assertEqual(merged["kind"], "meeting")
        self.assertEqual(merged["theme"], "Standup")
        self.assertEqual(merged["reachability"]["slack"], "defer")
        self.assertEqual(merged["writer"], "apo_engine")

    def test_degraded_free(self) -> None:
        merged = optima_merge.degraded_free()
        self.assertEqual(merged["kind"], "free")
        self.assertEqual(merged["schedule_role"], "current")


class RunMergeTest(unittest.TestCase):
    def tearDown(self) -> None:
        optima_contract.clear_optima_contract_cache()
        os.environ.pop("OPTIMA_SYNC", None)
        os.environ.pop("APO_VAULT_PATHS", None)
        os.environ.pop("APO_DEFAULT_VAULT", None)
        vaults._vault_id_cache.clear()

    def test_degrade_when_no_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "optima"
            root.mkdir()
            _write_usage(root, "optima")
            (root / "system" / "config").mkdir(parents=True, exist_ok=True)
            (root / "system" / "config" / "reachability-rules.yaml").write_text(
                "by_kind:\n  free:\n    slack: ok\n    email: ok\n    telegram: ok\n"
                "    phone: ok\n    rationale: free\n",
                encoding="utf-8",
            )
            _write_optima_contract(root)
            result = optima_merge.run_merge(root)
            self.assertTrue(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertTrue(result["wrote"])
            current = yaml.safe_load((root / "current.yaml").read_text(encoding="utf-8"))
            self.assertEqual(current["kind"], "free")
            self.assertEqual(current["writer"], "apo_engine")

    def test_merge_work_and_life(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            optima = base / "optima"
            work = base / "work"
            atlas = base / "atlas"
            for d, vid in ((optima, "optima"), (work, "work"), (atlas, "atlas")):
                d.mkdir()
                _write_usage(d, vid)
            (optima / "system" / "config").mkdir(parents=True, exist_ok=True)
            (optima / "system" / "config" / "reachability-rules.yaml").write_text(
                "by_kind:\n  focus:\n    slack: defer\n    email: ok\n    telegram: ok\n"
                "    phone: no\n    rationale: focus\n  personal:\n    slack: defer\n"
                "    email: ok\n    telegram: ok\n    phone: ok\n    rationale: personal\n"
                "  free:\n    slack: ok\n    email: ok\n    telegram: ok\n"
                "    phone: ok\n    rationale: free\n",
                encoding="utf-8",
            )
            _write_optima_contract(optima)

            now = datetime.now(ET)
            end = (now + timedelta(hours=1)).isoformat()
            start = now.isoformat()
            (work / "areas" / "schedule").mkdir(parents=True)
            (work / "areas" / "schedule" / "current.md").write_text(
                f"---\nkind: focus\ntheme: Deep work\nstart: {start}\nend: {end}\n"
                f"threads:\n  - stage-b\n---\n\n# current\n",
                encoding="utf-8",
            )
            (atlas / "areas" / "schedule").mkdir(parents=True)
            (atlas / "areas" / "schedule" / "current.yaml").write_text(
                f"kind: personal\ntheme: Errands\nplace: town\nstart: {start}\nend: {end}\n",
                encoding="utf-8",
            )

            os.environ["APO_VAULT_PATHS"] = f"{optima}:{work}:{atlas}"
            os.environ["APO_DEFAULT_VAULT"] = "optima"
            vaults._vault_id_cache.clear()

            result = optima_merge.run_merge(optima)
            self.assertTrue(result["ok"], result)
            self.assertFalse(result.get("degraded"))
            current = yaml.safe_load((optima / "current.yaml").read_text(encoding="utf-8"))
            # personal ranks above focus in KIND_PRIORITY
            self.assertEqual(current["kind"], "personal")
            self.assertEqual(current["theme"], "Errands")

    def test_opt_out_skips_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "optima"
            root.mkdir()
            _write_usage(root, "optima")
            (root / "system" / "config").mkdir(parents=True, exist_ok=True)
            (root / "system" / "config" / "reachability-rules.yaml").write_text(
                "by_kind: {}\n", encoding="utf-8"
            )
            _write_optima_contract(root)
            os.environ["OPTIMA_SYNC"] = "0"
            result = optima_merge.run_merge(root)
            self.assertTrue(result["ok"])
            self.assertTrue(result.get("skipped"))
            self.assertFalse((root / "current.yaml").is_file())

    def test_second_merge_skips_write_when_payload_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "optima"
            root.mkdir()
            _write_usage(root, "optima")
            (root / "system" / "config").mkdir(parents=True, exist_ok=True)
            (root / "system" / "config" / "reachability-rules.yaml").write_text(
                "by_kind:\n  free:\n    slack: ok\n    email: ok\n    telegram: ok\n"
                "    phone: ok\n    rationale: free\n",
                encoding="utf-8",
            )
            _write_optima_contract(root)
            first = optima_merge.run_merge(root)
            self.assertTrue(first["wrote"])
            text1 = (root / "current.yaml").read_text(encoding="utf-8")
            second = optima_merge.run_merge(root)
            self.assertFalse(second["wrote"], "timestamp churn must not force rewrite")
            self.assertEqual(text1, (root / "current.yaml").read_text(encoding="utf-8"))

    def test_path_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            optima = base / "optima"
            work = base / "work"
            for d, vid in ((optima, "optima"), (work, "work")):
                d.mkdir()
                _write_usage(d, vid)
            secret = base / "secret.yaml"
            secret.write_text("kind: meeting\ntheme: leaked\n", encoding="utf-8")
            # Place a file that would be reached via .. if escapes were allowed
            (work / "areas" / "schedule").mkdir(parents=True)
            os.environ["APO_VAULT_PATHS"] = f"{optima}:{work}"
            os.environ["APO_DEFAULT_VAULT"] = "optima"
            vaults._vault_id_cache.clear()
            spec = optima_contract.SourceSpec(
                id="escape",
                role="work_theme",
                path="../secret.yaml",
                vault="work",
                if_missing="skip",
            )
            _default, bindings = vaults.load_bindings()
            self.assertIsNone(
                optima_merge._resolve_source_path(optima, spec, bindings)
            )


class MergeTickIdleCostTest(unittest.TestCase):
    """Idle merge ticks must not re-parse the optima contract every second."""

    def tearDown(self) -> None:
        optima_contract.clear_optima_contract_cache()

    def test_idle_tick_reparses_nothing(self) -> None:
        import unittest.mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_usage(root, "optima")
            cdir = root / "system" / "contracts"
            (cdir / "optima-contract.schema.yaml").write_text(
                "refresh:\n  watch:\n    enabled: true\n    interval_seconds: 3600\n",
                encoding="utf-8",
            )
            ctl = optima_merge.VaultMergeController(root, verbose=False)
            ctl.tick(index_busy=False)  # warm caches + arm interval
            with unittest.mock.patch.object(
                optima_contract.yaml, "safe_load"
            ) as parse, unittest.mock.patch.object(
                optima_merge, "run_merge"
            ) as merge:
                for _ in range(20):
                    ctl.tick(index_busy=False)
                self.assertEqual(parse.call_count, 0, "contract re-parsed on idle tick")
                self.assertEqual(merge.call_count, 0, "merge ran before interval")


if __name__ == "__main__":
    unittest.main()
