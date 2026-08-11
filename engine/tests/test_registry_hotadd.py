"""Registry wake + multi-vault hot-add helpers."""

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

from apo_engine import deferred, watch


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
        self.reg = self.tmp / "vaults.json"
        self.deferred_dir = self.tmp / "deferred"
        self.deferred_dir.mkdir()
        deferred.DEFERRED_DIR = self.deferred_dir

    def _write_reg(self, vaults_map: dict) -> None:
        self.reg.write_text(
            json.dumps({"default": next(iter(vaults_map)), "vaults": vaults_map}),
            encoding="utf-8",
        )

    def test_supervisor_hot_adds_new_vault(self):
        self._write_reg(
            {
                "alpha": {
                    "root": str(self.a),
                    "index": str(self.tmp / "index-a.db"),
                    "collection": "alpha",
                },
            }
        )
        env = {
            "APO_VAULTS": str(self.reg),
            "APO_DEFERRED_DIR": str(self.deferred_dir),
            "APO_EMBED_BACKEND": "hash",
        }
        started: list[str] = []
        control = {"stop": False}

        def fake_watch_one(binding, *args, **kwargs):
            started.append(binding.name)
            while not control["stop"]:
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

                self._write_reg(
                    {
                        "alpha": {
                            "root": str(self.a),
                            "index": str(self.tmp / "index-a.db"),
                            "collection": "alpha",
                        },
                        "beta": {
                            "root": str(self.b),
                            "index": str(self.tmp / "index-b.db"),
                            "collection": "beta",
                        },
                    }
                )
                deferred.touch_registry_wake()
                deadline = time.time() + 5
                while "beta" not in started and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIn("beta", started)

                control["stop"] = True
                t.join(timeout=3)
                self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
