"""Discover vault contracts for the ``vault`` management tool.

Preferred live home: ``<vault>/system/contracts/*.yaml``.
Legacy (still discovered): ``system/config/*-contract.schema.yaml`` and
``okf-profile.schema.yaml``. When the same contract id exists in both places,
``system/contracts/`` wins.

This module is read-only discovery for agents/harnesses. Engine write-path
loaders (OKF, git) keep their own resolve paths until those migrate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONTRACTS_DIR = Path("system") / "contracts"
LEGACY_CONFIG_DIR = Path("system") / "config"

# Legacy filenames under system/config/ (id → relative name).
_LEGACY_FILES: dict[str, str] = {
    "okf-contract": "okf-contract.schema.yaml",
    "okf-profile": "okf-profile.schema.yaml",
    "git-contract": "git-contract.schema.yaml",
    "local-web-contract": "local-web-contract.schema.yaml",
}


def contract_id_from_name(filename: str) -> str:
    """Map ``okf-contract.schema.yaml`` → ``okf-contract``."""
    name = filename.strip()
    for suffix in (".schema.yaml", ".schema.yml", ".yaml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def _parse_yaml_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"unreadable: {e}"
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return None, f"invalid yaml: {e}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "contract root must be a mapping"
    return data, None


def _entry(
    *,
    cid: str,
    rel: str,
    source: str,
    data: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": cid,
        "path": rel.replace("\\", "/"),
        "source": source,  # contracts | legacy
    }
    if error:
        out["ok"] = False
        out["error"] = error
    else:
        out["ok"] = True
        out["data"] = data if data is not None else {}
    return out


def discover_contracts(vault_root: Path) -> dict[str, dict[str, Any]]:
    """Return contract id → entry for one vault root.

    Entries include parsed ``data`` when readable. Ids from
    ``system/contracts/`` override legacy ``system/config/`` for the same id.
    """
    found: dict[str, dict[str, Any]] = {}
    root = vault_root.resolve()

    contracts_dir = root / CONTRACTS_DIR
    if contracts_dir.is_dir():
        for path in sorted(contracts_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".yaml", ".yml"}:
                continue
            cid = contract_id_from_name(path.name)
            if not cid or cid.startswith("."):
                continue
            rel = str(CONTRACTS_DIR / path.name)
            data, err = _parse_yaml_file(path)
            found[cid] = _entry(
                cid=cid, rel=rel, source="contracts", data=data, error=err
            )

    legacy_dir = root / LEGACY_CONFIG_DIR
    if legacy_dir.is_dir():
        # Prefer the explicit legacy map, then any *-contract.schema.yaml.
        candidates: list[tuple[str, Path]] = []
        seen_names: set[str] = set()
        for cid, name in _LEGACY_FILES.items():
            p = legacy_dir / name
            if p.is_file():
                candidates.append((cid, p))
                seen_names.add(name)
        for path in sorted(legacy_dir.glob("*-contract.schema.yaml")):
            if path.name in seen_names:
                continue
            candidates.append((contract_id_from_name(path.name), path))
        for path in sorted(legacy_dir.glob("*-contract.schema.yml")):
            if path.name in seen_names:
                continue
            candidates.append((contract_id_from_name(path.name), path))

        for cid, path in candidates:
            if cid in found:
                continue  # system/contracts/ wins
            # Collapse legacy okf-profile under okf-contract when that id is free
            # but okf-contract.schema.yaml is absent — keep distinct ids so
            # harnesses see what is on disk.
            rel = str(LEGACY_CONFIG_DIR / path.name)
            data, err = _parse_yaml_file(path)
            found[cid] = _entry(
                cid=cid, rel=rel, source="legacy", data=data, error=err
            )

    return dict(sorted(found.items()))


def contract_ids(vault_root: Path) -> list[str]:
    return list(discover_contracts(vault_root))


def summarize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Strip parsed ``data`` — id/path/source/ok (+ error)."""
    out: dict[str, Any] = {
        "id": entry["id"],
        "path": entry["path"],
        "source": entry["source"],
        "ok": entry.get("ok", True),
    }
    if not out["ok"] and entry.get("error"):
        out["error"] = entry["error"]
    return out


def present_contracts(
    contracts: dict[str, dict[str, Any]], *, full: bool
) -> dict[str, dict[str, Any]]:
    """Return contracts with or without YAML bodies (default: summaries)."""
    if full:
        return contracts
    return {cid: summarize_entry(entry) for cid, entry in contracts.items()}


def contracts_summary(vault_root: Path) -> list[dict[str, Any]]:
    """Ids + paths without parsed bodies (for ``list``)."""
    return [
        summarize_entry(entry) for entry in discover_contracts(vault_root).values()
    ]
