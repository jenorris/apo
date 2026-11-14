"""Fail if tracked files still carry employer tokens.

The token list lives in this test. The scan skips this file.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKENS = (
    "gradguard",
    "skypad",
    "floci",
    "desma",
    ".ts.net",
    "faber",
    "tuition",
    "cortana",
    "bossman",
    "lucid",
    "amtrust",
    "harmon",
    "markel",
    "allianz",
    "starrez",
)


class PublicTreeDenylistTest(unittest.TestCase):
    def test_tracked_files_omit_employer_tokens(self):
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        hits: list[str] = []
        self_rel = Path(__file__).resolve().relative_to(ROOT).as_posix()
        for raw in listed.stdout.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", "replace")
            if rel == self_rel:
                continue
            path = ROOT / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            found = [token for token in TOKENS if token in text or token in rel.lower()]
            if found:
                hits.append(f"{rel}: {', '.join(found)}")
        self.assertEqual(hits, [], "\n".join(hits))
