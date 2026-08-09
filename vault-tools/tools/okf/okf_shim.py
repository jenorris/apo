#!/usr/bin/env python3
"""Bridge from the vault-tools scripts to ``apo-engine okf``.

vault-tools runs standalone with its own ``_bootstrap`` and no dependency on
the engine package, so this resolves the engine two ways: import it if it is
importable, otherwise shell out to an ``apo-engine`` on PATH. Either way there
is exactly one OKF implementation being exercised.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _engine_importable() -> bool:
    try:
        import apo_engine.okf_cli  # noqa: F401
    except Exception:
        return False
    return True


def run_engine_okf(subcommand: str, vault_root: Path, args: list[str]) -> int:
    """Run ``apo-engine okf <subcommand> --vault-root <root> <args…>``."""
    argv = [subcommand, "--vault-root", str(vault_root), *args]

    if _engine_importable():
        from apo_engine import okf_cli

        return okf_cli.main(argv)

    exe = shutil.which("apo-engine")
    if exe:
        return subprocess.call([exe, "okf", *argv])

    print(
        "vault-tools: apo-engine not importable and not on PATH.\n"
        "  This script is a shim over `apo-engine okf` — install the engine\n"
        "  (`just setup`) or run `apo-engine okf " + subcommand + "` directly.",
        file=sys.stderr,
    )
    return 2


def engine_env_hint() -> str:
    return os.environ.get("APO_ENGINE_HINT", "")
