"""Registry wake + multi-vault hot-add / soft-remove helpers."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import deferred, vaults, watch


def _write_usage(root: Path, vault_id: str) -> None:
    contracts = root / "system" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "usage-contract.schema.yaml").write_text(
        f"vault_id: {vault_id}\n", encoding="utf-8"
    )


class RegistryWakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        deferred.DEFERRED_DIR = self.tmp / "deferred"

    def test_touch_and_consume_registry_wake(self):
        self.assertFalse(deferred.wake_registry_pending())
        deferred.touch_registry_wake()
        wake = deferred.DEFERRED_DIR / deferred.REGISTRY_WAKE_NAME
        self.assertTrue(wake.is_file())
        self.assertTrue(deferred.wake_registry_pending())
        self.assertFalse(wake.is_file())
        self.assertFalse(deferred.wake_registry_pending())


class RegistryHotAddTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        self.a.mkdir()
        self.b.mkdir()
        (self.a / "note.md").write_text("# a\n", encoding="utf-8")
        (self.b / "note.md").write_text("# b\n", encoding="utf-8")
        _write_usage(self.a, "alpha")
        _write_usage(self.b, "beta")
        self.reg = self.tmp / "vaults.json"
        self.deferred_dir = self.tmp / "deferred"
        self.deferred_dir.mkdir()
        deferred.DEFERRED_DIR = self.deferred_dir
        vaults._vault_id_cache.clear()
        vaults._APO_VAULTS_WARNED = False

    def _write_reg(self, roots: list[tuple[str, Path]]) -> None:
        vaults_map = {
            f"key-{name}": {"root": str(path)} for name, path in roots
        }
        self.reg.write_text(
            json.dumps({"default": roots[0][0], "vaults": vaults_map}),
            encoding="utf-8",
        )

    def test_supervisor_hot_adds_new_vault(self):
        self._write_reg([("alpha", self.a)])
        env = {
            "APO_VAULTS": str(self.reg),
            "APO_DEFAULT_VAULT": "alpha",
            "APO_DEFERRED_DIR": str(self.deferred_dir),
            "APO_EMBED_BACKEND": "hash",
        }
        started: list[str] = []
        control = {"stop": False}

        def fake_watch_one(binding, *args, **kwargs):
            started.append(binding.name)
            stop = kwargs.get("stop")
            while not control["stop"] and not (stop is not None and stop.is_set()):
                time.sleep(0.05)

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(watch, "_watch_one", side_effect=fake_watch_one):
                t = threading.Thread(
                    target=lambda: watch.run_watch(
                        interval=30, use_events=False, verbose=False
                    ),
                    daemon=True,
                )
                t.start()
                deadline = time.time() + 3
                while "alpha" not in started and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIn("alpha", started)

                self._write_reg([("alpha", self.a), ("beta", self.b)])
                deferred.touch_registry_wake()
                deadline = time.time() + 5
                while "beta" not in started and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIn("beta", started)

                control["stop"] = True
                t.join(timeout=3)
                self.assertFalse(t.is_alive())

    def test_supervisor_soft_removes_vault_siblings_survive(self):
        self._write_reg([("alpha", self.a), ("beta", self.b)])
        env = {
            "APO_VAULTS": str(self.reg),
            "APO_DEFAULT_VAULT": "alpha",
            "APO_DEFERRED_DIR": str(self.deferred_dir),
            "APO_EMBED_BACKEND": "hash",
        }
        alive: dict[str, bool] = {}
        stops: dict[str, threading.Event] = {}

        def fake_watch_one(binding, *args, **kwargs):
            alive[binding.name] = True
            stop = kwargs.get("stop")
            if stop is not None:
                stops[binding.name] = stop
            while stop is None or not stop.is_set():
                time.sleep(0.05)
            alive[binding.name] = False

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(watch, "_watch_one", side_effect=fake_watch_one):
                t = threading.Thread(
                    target=lambda: watch.run_watch(
                        interval=30, use_events=False, verbose=False
                    ),
                    daemon=True,
                )
                t.start()
                deadline = time.time() + 3
                while not ({"alpha", "beta"} <= set(alive)) and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue({"alpha", "beta"} <= set(alive))

                # Drop beta from registry → soft-remove; alpha keeps running.
                self._write_reg([("alpha", self.a)])
                deferred.touch_registry_wake()
                deadline = time.time() + 5
                while alive.get("beta", True) and time.time() < deadline:
                    time.sleep(0.05)
                self.assertFalse(alive.get("beta", True))
                self.assertTrue(alive.get("alpha"))

                # Stop remaining via process interrupt path: set alpha stop
                if "alpha" in stops:
                    stops["alpha"].set()
                t.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
