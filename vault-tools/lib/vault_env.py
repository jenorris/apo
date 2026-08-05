"""Resolve vault root and discover machine contracts under system/contracts/."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Align with apo_engine.vault_contracts discovery (stdlib-only copy).
CONTRACTS_DIR = Path("system") / "contracts"
LEGACY_CONFIG_DIR = Path("system") / "config"
_LEGACY_FILES: dict[str, str] = {
    "okf-contract": "okf-contract.schema.yaml",
    "okf-profile": "okf-profile.schema.yaml",
    "git-contract": "git-contract.schema.yaml",
    "local-web-contract": "local-web-contract.schema.yaml",
}
_OKF_IDS = frozenset({"okf-contract", "okf", "okf-profile"})


def _die(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def contract_id_from_name(filename: str) -> str:
    """Map ``okf-contract.schema.yaml`` → ``okf-contract``."""
    name = filename.strip()
    for suffix in (".schema.yaml", ".schema.yml", ".yaml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


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


def resolve_under_vault(vault_root: Path, path_arg: str | Path) -> Path:
    """
    Resolve a CLI path argument and require it stay under vault_root.

    Relative paths are joined to the vault root. Absolute paths must resolve
    inside the vault (``..`` / symlink escapes → exit 2).
    """
    root = vault_root.resolve()
    raw = Path(path_arg).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        _die(f"vault-tools: path escapes vault root: {path_arg} (vault={root})")
    return candidate


def contract_ids(vault_root: Path) -> set[str]:
    """
    Return contract ids (preferred ``system/contracts/``, then legacy
    ``system/config/*-contract.schema.yaml`` / ``okf-profile.schema.yaml``).

    Mirrors ``apo_engine.vault_contracts.discover_contracts`` id set (no YAML parse).
    Also adds short aliases: ``okf-contract`` → ``okf``.
    """
    root = vault_root.resolve()
    found: set[str] = set()

    contracts_dir = root / CONTRACTS_DIR
    if contracts_dir.is_dir():
        for path in contracts_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
                continue
            cid = contract_id_from_name(path.name)
            if not cid or cid.startswith("."):
                continue
            found.add(cid)

    legacy_dir = root / LEGACY_CONFIG_DIR
    if legacy_dir.is_dir():
        seen_names: set[str] = set()
        for cid, name in _LEGACY_FILES.items():
            p = legacy_dir / name
            if p.is_file():
                found.add(cid)
                seen_names.add(name)
        for path in sorted(legacy_dir.glob("*-contract.schema.yaml")):
            if path.name in seen_names:
                continue
            found.add(contract_id_from_name(path.name))
        for path in sorted(legacy_dir.glob("*-contract.schema.yml")):
            if path.name in seen_names:
                continue
            found.add(contract_id_from_name(path.name))

    # Short aliases for *-contract ids
    for cid in list(found):
        if cid.endswith("-contract"):
            found.add(cid[: -len("-contract")])
    return found


def has_contract(vault_root: Path, required: str) -> bool:
    ids = contract_ids(vault_root)
    if required in ids:
        return True
    # OKF write-path accepts okf-contract or legacy okf-profile
    if required in _OKF_IDS and ids & _OKF_IDS:
        return True
    return False
