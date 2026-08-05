"""Bootstrap VAULT_ROOT + contract preflight for OKF tools."""

from __future__ import annotations

import sys
from pathlib import Path

# Align sys.path so `lib` imports work when scripts run as files
_TOOLS_ROOT = Path(__file__).resolve().parents[2]
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))
# tools/okf/ for sibling okf_common
_OKF_DIR = Path(__file__).resolve().parent
if str(_OKF_DIR) not in sys.path:
    sys.path.insert(0, str(_OKF_DIR))

from lib.preflight import require_contracts  # noqa: E402
from lib.vault_env import resolve_vault_root  # noqa: E402
from okf_common import configure  # noqa: E402


def split_vault_args(argv: list[str]) -> tuple[str | None, list[str]]:
    """Pull --vault PATH (or --vault=PATH) from argv; return (vault, rest)."""
    vault: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--vault" and i + 1 < len(argv):
            vault = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--vault="):
            vault = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return vault, rest


def bootstrap(argv: list[str]) -> tuple[Path, list[str]]:
    vault_arg, rest = split_vault_args(argv)
    root = resolve_vault_root(vault_arg)
    require_contracts(root, ["okf-contract"])
    configure(root)
    return root, rest
