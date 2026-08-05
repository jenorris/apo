"""Search contract loader — per-vault default exclude globs for unscoped search.

Active when ``system/contracts/search-contract.schema.yaml`` (or legacy
``system/config/search-contract.schema.yaml``) exists under the vault root.
Used by ``search`` and ``history`` browse when ``exclude=`` is omitted and
``folder=`` is empty.

Fallback: ``APO_SEARCH_EXCLUDE`` env (deprecated desk-wide default).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from apo_engine import config

SEARCH_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "search-contract.schema.yaml",
    Path("system") / "config" / "search-contract.schema.yaml",
)
SEARCH_CONTRACT_REL = SEARCH_CONTRACT_CANDIDATES[0]


def resolve_search_contract_path(vault_root: Path, explicit: str | None = None) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_SEARCH_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in SEARCH_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_search_contract(vault_root: Path, explicit: str | None = None) -> dict[str, Any] | None:
    """Parse search-contract YAML if present. Returns None when missing/unreadable."""
    path = resolve_search_contract_path(vault_root, explicit)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _normalize_exclude_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def resolve_search_exclude(
    vault_root: Path,
    *,
    caller_exclude: list[str] | None,
    folder_clean: str,
) -> tuple[list[str] | None, list[str] | None, str]:
    """Resolve effective exclude globs for one vault.

    Returns ``(effective_exclude, applied_default, source)`` where ``source`` is
    ``caller`` | ``folder`` | ``vault`` | ``env`` | ``none``.
    """
    if caller_exclude:
        return list(caller_exclude), None, "caller"
    if folder_clean:
        return None, None, "folder"
    root = vault_root.resolve()
    data = load_search_contract(root)
    if data is not None:
        vault_defaults = _normalize_exclude_list(data.get("default_exclude"))
        if vault_defaults:
            return vault_defaults, vault_defaults, "vault"
        return None, None, "vault"
    if config.SEARCH_EXCLUDE_DEFAULT:
        env_defaults = list(config.SEARCH_EXCLUDE_DEFAULT)
        return env_defaults, env_defaults, "env"
    return None, None, "none"


def clear_default_exclude_cache() -> None:
    """Invalidate any cached contract reads (reserved; currently no-op)."""
    return None
