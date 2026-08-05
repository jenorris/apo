"""Contract preflight for shared vault-tools."""

from __future__ import annotations

import sys
from pathlib import Path

from .vault_env import contract_ids, has_contract


def require_contracts(vault_root: Path, required: list[str]) -> None:
    """
    Exit non-zero if the vault does not ship the required contracts.

    ``required`` entries may be ``okf-contract`` or short ``okf`` (matches
    okf-contract.schema.yaml).
    """
    missing = [name for name in required if not has_contract(vault_root, name)]
    if not missing:
        return
    found = sorted(contract_ids(vault_root))
    found_msg = ", ".join(found) if found else "(none)"
    print(
        "vault-tools: vault missing required contract(s): "
        + ", ".join(missing)
        + f"\n  vault: {vault_root}"
        + f"\n  found under system/contracts/: {found_msg}",
        file=sys.stderr,
    )
    raise SystemExit(2)
