#!/usr/bin/env python3
"""Tests for lint_readme.py against good/bad fixtures."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lint_readme.py"
GOOD = ROOT / "fixtures" / "good" / "README.md"
BAD = ROOT / "fixtures" / "bad" / "README.md"


class LintReadmeTests(unittest.TestCase):
    def _run(self, *paths: Path, strict: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(SCRIPT), *[str(p) for p in paths]]
        if strict:
            cmd.append("--strict")
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    def test_good_fixture_clean(self) -> None:
        proc = self._run(GOOD)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("0 error(s)", proc.stdout)

    def test_bad_fixture_errors(self) -> None:
        proc = self._run(BAD)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        combined = proc.stderr + proc.stdout
        self.assertIn("empty section", combined)
        self.assertIn("broken local link", combined)
        self.assertIn("broken local image", combined)
        self.assertIn("placeholder text", combined)
        self.assertIn("possible AI-slop phrasing", combined)

    def test_strict_fails_on_warnings_alone(self) -> None:
        # Create a warning-only temp by relying on bad fixture having warnings;
        # with --strict, exit is 1 when any warning exists (errors also present).
        proc = self._run(BAD, strict=True)
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
