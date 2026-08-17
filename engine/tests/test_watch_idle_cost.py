"""Idle watcher CPU cost — the per-cycle hot path must stay cheap.

Every vault watcher thread runs `maybe_reproject` + the git-sync tick roughly
once per second. Both used to do real work on every call (registry reload +
contract stats; YAML re-parse + a forked `git rev-parse`), so an idle desk with
N vaults burned N x tens-of-ms every second. These tests pin the cheap path.
"""

from __future__ import annotations

import subprocess
import unittest
import unittest.mock
from pathlib import Path

from apo_engine import git_contract, git_sync, vault_project


def _reset_reproject_state() -> None:
    vault_project._last_reproject_mono = 0.0
    vault_project._last_poll_mono = 0.0
    vault_project._last_desk_mtime = None
    vault_project._last_contracts_sig = None


class MaybeReprojectPollGateTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_reproject_state()
        self.addCleanup(_reset_reproject_state)
        git_contract._contract_cache.clear()
        git_contract._work_tree_cache.clear()

    def test_repeat_polls_do_not_rescan(self) -> None:
        """The drift scan is gated *before* it runs — that is the CPU fix."""
        with unittest.mock.patch.object(
            vault_project, "_contracts_signature", return_value=""
        ) as sig:
            # First call seeds state and is allowed to scan.
            vault_project.maybe_reproject(reason="desk-poll")
            self.assertEqual(sig.call_count, 1)
            # Simulate 10 vault threads polling again immediately.
            for _ in range(10):
                self.assertIsNone(vault_project.maybe_reproject(reason="desk-poll"))
            self.assertEqual(
                sig.call_count,
                1,
                "contracts signature must not be recomputed inside the poll gap",
            )

    def test_force_bypasses_poll_gate(self) -> None:
        """`vault(action=project, force=True)` must never be throttled."""
        with unittest.mock.patch.object(
            vault_project, "_contracts_signature", return_value=""
        ) as sig:
            vault_project.maybe_reproject(reason="desk-poll")
            base = sig.call_count
            out = vault_project.maybe_reproject(reason="explicit", force=True)
            self.assertEqual(sig.call_count, base + 1)
            self.assertIsNotNone(out)
            self.assertTrue(out["changed"])

    def test_drift_detected_after_gap_elapses(self) -> None:
        """Gating delays detection, it must not lose it."""
        with unittest.mock.patch.object(
            vault_project, "_contracts_signature", side_effect=["a", "b"]
        ):
            with unittest.mock.patch.object(
                vault_project, "_desk_mtime", return_value=None
            ):
                # Seeds "a" (first-call init returns None).
                self.assertIsNone(vault_project.maybe_reproject(reason="seed"))
                # Pretend the gap has passed; signature now differs -> reported.
                vault_project._last_poll_mono = 1.0
                with unittest.mock.patch.object(
                    vault_project.time, "monotonic", return_value=10_000.0
                ):
                    out = vault_project.maybe_reproject(reason="desk-poll")
        self.assertIsNotNone(out)
        self.assertTrue(out["changed"])


class GitContractCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        git_contract._contract_cache.clear()
        git_contract._work_tree_cache.clear()
        self.addCleanup(git_contract._contract_cache.clear)
        self.addCleanup(git_contract._work_tree_cache.clear)

    def _vault(self, tmp: Path, *, enabled: bool = True) -> Path:
        cdir = tmp / "system" / "contracts"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\n"
            "default_branch: main\n"
            "sync:\n"
            f"  enabled: {'true' if enabled else 'false'}\n",
            encoding="utf-8",
        )
        return tmp

    def test_load_git_contract_parses_once_per_mtime(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._vault(Path(td))
            with unittest.mock.patch.object(
                git_contract.yaml, "safe_load", wraps=git_contract.yaml.safe_load
            ) as parse:
                for _ in range(5):
                    data = git_contract.load_git_contract(root)
                    self.assertEqual(data["default_branch"], "main")
                self.assertEqual(parse.call_count, 1, "YAML re-parsed despite cache")

    def test_load_git_contract_invalidates_on_edit(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._vault(Path(td), enabled=True)
            self.assertTrue(git_contract.load_git_contract(root)["sync"]["enabled"])
            self._vault(Path(td), enabled=False)
            self.assertFalse(
                git_contract.load_git_contract(root)["sync"]["enabled"],
                "cache did not invalidate after contract edit",
            )

    def test_cached_result_is_not_shared_mutably(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._vault(Path(td))
            first = git_contract.load_git_contract(root)
            first["default_branch"] = "clobbered"
            self.assertEqual(
                git_contract.load_git_contract(root)["default_branch"],
                "main",
                "caller mutation leaked into the cache",
            )

    def test_is_git_work_tree_does_not_fork_per_call(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with unittest.mock.patch.object(
                git_contract.subprocess, "run", wraps=subprocess.run
            ) as run:
                for _ in range(6):
                    git_contract.is_git_work_tree(root)
                self.assertEqual(
                    run.call_count, 1, "git rev-parse forked more than once"
                )


class SyncTickIdleCostTest(unittest.TestCase):
    """A disabled/enabled sync tick must not re-read the contract each time."""

    def setUp(self) -> None:
        git_contract._contract_cache.clear()
        git_contract._work_tree_cache.clear()
        self.addCleanup(git_contract._contract_cache.clear)
        self.addCleanup(git_contract._work_tree_cache.clear)

    def test_idle_tick_reparses_nothing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cdir = root / "system" / "contracts"
            cdir.mkdir(parents=True)
            (cdir / "git-contract.schema.yaml").write_text(
                "git_contract_version: '0.1'\nsync:\n  enabled: false\n",
                encoding="utf-8",
            )
            ctl = git_sync.VaultSyncController(root, verbose=False)
            ctl.tick(index_busy=False)  # warm caches
            with unittest.mock.patch.object(
                git_contract.yaml, "safe_load"
            ) as parse, unittest.mock.patch.object(
                git_contract.subprocess, "run"
            ) as run:
                for _ in range(20):
                    ctl.tick(index_busy=False)
                self.assertEqual(parse.call_count, 0, "contract re-parsed on idle tick")
                self.assertEqual(run.call_count, 0, "git forked on idle tick")


if __name__ == "__main__":
    unittest.main()
