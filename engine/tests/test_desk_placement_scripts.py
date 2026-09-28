"""scripts/desk-project-json.sh — registry resolution for the desk placement scripts.

The placement scripts used to inherit whatever discovery vars `just` dotenv-loaded
from the repo .env (a dev profile), rendering the desk against a registry the
MCP host never serves. These tests drive the helper with a stub engine binary
that echoes its environment and argv, so the resolution rules are pinned without
a real vault registry.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "desk-project-json.sh"

_DISCOVERY_VARS = (
    "APO_VAULTS",
    "APO_VAULT_PATHS",
    "APO_COLLECTION_ROOT",
    "APO_DEFAULT_VAULT",
    "APO_NOTES_ROOT",
    "APO_INDEX",
    "APO_COLLECTION",
    "APO_DESK_REGISTRY",
)


@unittest.skipUnless(shutil.which("bash"), "bash required")
class DeskProjectJsonScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-desk-script-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = self.tmp / "apo-engine-stub"
        self.stub.write_text(
            "#!/usr/bin/env bash\n"
            "python3 - \"$@\" <<'PY'\n"
            "import json, os, sys\n"
            "keys = ['APO_VAULTS','APO_VAULT_PATHS','APO_COLLECTION_ROOT','APO_DEFAULT_VAULT','APO_NOTES_ROOT']\n"
            "print(json.dumps({'argv': sys.argv[1:], 'env': {k: os.environ.get(k) for k in keys}}))\n"
            "PY\n",
            encoding="utf-8",
        )
        self.stub.chmod(0o755)
        self.registry = self.tmp / "vaults.json"
        self.registry.write_text('{"default": "work", "vaults": {}}', encoding="utf-8")

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        base = {k: v for k, v in os.environ.items() if k not in _DISCOVERY_VARS}
        base["APO_ENGINE_BIN"] = str(self.stub)
        base.update(env or {})
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=base,
            cwd=str(REPO),
            timeout=30,
        )

    def test_explicit_registry_scrubs_inherited_discovery_vars(self):
        proc = self._run(
            "--registry",
            str(self.registry),
            "--mode",
            "index",
            env={
                # The dotenv dev profile the scripts used to pick up blindly.
                "APO_COLLECTION_ROOT": "/dev/profile/Notes",
                "APO_VAULT_PATHS": "/dev/profile/compliance",
                "APO_DEFAULT_VAULT": "atlas",
                "APO_NOTES_ROOT": "/dev/profile/Notes/Atlas",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["argv"], ["desk-project", "--mode", "index"])
        self.assertEqual(out["env"]["APO_VAULTS"], str(self.registry))
        for k in ("APO_COLLECTION_ROOT", "APO_VAULT_PATHS", "APO_DEFAULT_VAULT", "APO_NOTES_ROOT"):
            self.assertIsNone(out["env"][k], k)
        self.assertIn("(explicit)", proc.stderr)

    def test_env_registry_override_and_default_mode(self):
        proc = self._run(env={"APO_DESK_REGISTRY": str(self.registry)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["argv"], ["desk-project", "--mode", "index"])
        self.assertEqual(out["env"]["APO_VAULTS"], str(self.registry))

    def test_missing_registry_file_fails(self):
        proc = self._run("--registry", str(self.tmp / "nope.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("registry not found", proc.stderr)

    def test_inherited_discovery_is_used_but_named_on_stderr(self):
        proc = self._run("--mode=full", env={"APO_VAULT_PATHS": "/x:/y", "APO_DEFAULT_VAULT": "work"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["argv"], ["desk-project", "--mode", "full"])
        self.assertEqual(out["env"]["APO_VAULT_PATHS"], "/x:/y")
        self.assertIn("APO_VAULT_PATHS=/x:/y (inherited", proc.stderr)

    def test_no_registry_at_all_is_an_error(self):
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no vault registry", proc.stderr)


if __name__ == "__main__":
    unittest.main()
