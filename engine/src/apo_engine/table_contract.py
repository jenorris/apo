"""Table contract loader — per-vault row_key / merge defaults for GFM tables.

Active when ``system/contracts/table-contract.schema.yaml`` (or legacy
``system/config/table-contract.schema.yaml``) exists under the vault root.
Used by the indexer (``row_key`` via ``key_column``) and by ``replace_table``
merge defaults. Fuzzy header matching with ambiguity reject remains the
engine default; this contract only overrides per path pattern.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

import yaml

TABLE_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "table-contract.schema.yaml",
    Path("system") / "config" / "table-contract.schema.yaml",
)


def resolve_table_contract_path(vault_root: Path, explicit: str | None = None) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_TABLE_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in TABLE_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_table_contract(vault_root: Path, explicit: str | None = None) -> dict[str, Any] | None:
    """Parse table-contract YAML if present. Returns None when missing/unreadable."""
    path = resolve_table_contract_path(vault_root, explicit)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def table_rule_for(vault_root: Path, rel: str) -> dict[str, Any]:
    """Return the first matching ``tables[]`` rule for a vault-relative path.

    Empty dict when no contract or no pattern matches. First match wins.
    """
    data = load_table_contract(vault_root)
    if not data:
        return {}
    rules = data.get("tables")
    if not isinstance(rules, list):
        return {}
    path = (rel or "").replace("\\", "/").lstrip("./")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = str(rule.get("match") or "").strip()
        if not match:
            continue
        if fnmatch.fnmatchcase(path, match) or fnmatch.fnmatch(path, match):
            return rule
    return {}


def key_column_for(vault_root: Path, rel: str) -> str | None:
    """Resolved ``key_column`` for ``rel``, or None (engine first-cell default)."""
    col = table_rule_for(vault_root, rel).get("key_column")
    if isinstance(col, str) and col.strip():
        return col.strip()
    return None
