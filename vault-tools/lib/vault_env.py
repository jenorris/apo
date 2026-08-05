"""Resolve vault root and discover machine contracts under system/contracts/."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

CONTRACT_STEM_RE = re.compile(r"^(.+?)(?:\.schema)?\.ya?ml$", re.IGNORECASE)


def _die(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def resolve_vault_root(
    vault: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve vault root from --vault / VAULT_ROOT / APO_VAULT_ROOT."""
    environ = env if env is not None else os.environ
    raw = vault
    if raw is None or raw == "":
        raw = environ.get("VAULT_ROOT") or environ.get("APO_VAULT_ROOT")
    if not raw:
        _die("vault-tools: set VAULT_ROOT or APO_VAULT_ROOT, or pass --vault <path>")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        _die(f"vault-tools: vault root is not a directory: {root}")
    return root


def contract_ids(vault_root: Path) -> set[str]:
    """
    Return contract ids discovered under <vault>/system/contracts/.

    Filenames like okf-contract.schema.yaml → okf-contract.
    Also accepts bare okf-contract.yaml.
    """
    contracts_dir = vault_root / "system" / "contracts"
    ids: set[str] = set()
    if not contracts_dir.is_dir():
        return ids
    for path in contracts_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        m = CONTRACT_STEM_RE.match(path.name)
        if not m:
            continue
        stem = m.group(1)
        if stem.endswith(".schema"):
            stem = stem[: -len(".schema")]
        ids.add(stem)
        if stem.endswith("-contract"):
            ids.add(stem[: -len("-contract")])
    return ids


def has_contract(vault_root: Path, required: str) -> bool:
    return required in contract_ids(vault_root)
