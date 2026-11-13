#!/usr/bin/env python3
"""Reject retired transport and command vocabulary on active surfaces."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = {
    "CHANGELOG.md",
    "docs/local-rpc.md",
    "scripts/check_stale_vocabulary.py",
    "engine/tests/test_stale_vocabulary.py",
}
PATTERNS = (
    re.compile(r"\bRPC\b", re.IGNORECASE),
    re.compile(r"\brpc\.py\b", re.IGNORECASE),
    re.compile(r"\bapo-engine serve\b", re.IGNORECASE),
)
SUFFIXES = {".md", ".py", ".go", ".yaml", ".yml", ".toml", ".sh"}
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "graphify-out"}


def findings() -> list[str]:
    out: list[str] = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if rel in ALLOWLIST or any(part in SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                out.append(f"{rel}:{number}: {line.strip()}")
    return out


def main() -> int:
    stale = findings()
    if stale:
        print("retired active vocabulary found:", file=sys.stderr)
        print("\n".join(stale), file=sys.stderr)
        return 1
    print("stale vocabulary: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
