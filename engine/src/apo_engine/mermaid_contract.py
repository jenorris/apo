"""Mermaid contract loader — catalog join, chunk strategy, validation mode."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

import yaml

MERMAID_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "mermaid-contract.schema.yaml",
    Path("system") / "config" / "mermaid-contract.schema.yaml",
)


def resolve_mermaid_contract_path(vault_root: Path, explicit: str | None = None) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_MERMAID_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in MERMAID_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_mermaid_contract(vault_root: Path, explicit: str | None = None) -> dict[str, Any] | None:
    path = resolve_mermaid_contract_path(vault_root, explicit)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def diagram_rule_for(vault_root: Path, rel: str) -> dict[str, Any]:
    data = load_mermaid_contract(vault_root)
    if not data:
        return {}
    rules = data.get("diagrams")
    if not isinstance(rules, list):
        return {}
    path = (rel or "").replace("\\", "/").lstrip("./")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = str(rule.get("match") or "").strip()
        if match and (fnmatch.fnmatchcase(path, match) or fnmatch.fnmatch(path, match)):
            return rule
    return {}


def catalog_rule_for(vault_root: Path, rel: str) -> dict[str, Any]:
    data = load_mermaid_contract(vault_root)
    if not data:
        return {}
    rules = data.get("catalogs")
    if not isinstance(rules, list):
        return {}
    path = (rel or "").replace("\\", "/").lstrip("./")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = str(rule.get("match") or "").strip()
        if match and (fnmatch.fnmatchcase(path, match) or fnmatch.fnmatch(path, match)):
            return rule
    return {}


def catalog_entry_for(vault_root: Path, rel: str) -> dict[str, Any]:
    """Load catalog.yaml entry for a diagram path when contract matches."""
    rule = catalog_rule_for(vault_root, rel)
    if not rule:
        return {}
    catalog_path = str(rule.get("catalog_path") or "").strip()
    if not catalog_path:
        return {}
    cat_file = vault_root / catalog_path
    if not cat_file.is_file():
        return {}
    try:
        data = yaml.safe_load(cat_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    diagrams = data.get("diagrams")
    if not isinstance(diagrams, list):
        return {}
    slug_from = str(rule.get("slug_from") or "parent_dir").strip()
    path = Path(rel.replace("\\", "/"))
    if slug_from == "parent_dir":
        slug = path.parent.name
    else:
        slug = path.stem
    for entry in diagrams:
        if isinstance(entry, dict) and str(entry.get("slug") or "") == slug:
            out = dict(entry)
            out["diagram_id"] = slug
            out.setdefault("title", out.get("title") or slug.replace("-", " ").title())
            out.setdefault("description", out.get("title"))
            out.setdefault("okf_type", "Diagram")
            return out
    return {}


def chunk_strategy_for(vault_root: Path, rel: str) -> str:
    strat = diagram_rule_for(vault_root, rel).get("chunk_strategy")
    if isinstance(strat, str) and strat.strip():
        return strat.strip()
    return "nodes_and_edges"


def include_edge_chunks(vault_root: Path, rel: str) -> bool:
    val = diagram_rule_for(vault_root, rel).get("include_edge_chunks")
    if isinstance(val, bool):
        return val
    return True


def validation_mode(vault_root: Path, rel: str) -> str:
    mode = diagram_rule_for(vault_root, rel).get("validation")
    if isinstance(mode, str) and mode.strip():
        return mode.strip().lower()
    return "soft"


def flatten_template_for(vault_root: Path, rel: str) -> str:
    tpl = diagram_rule_for(vault_root, rel).get("flatten_template")
    if isinstance(tpl, str) and tpl.strip():
        return tpl.strip()
    return "{title} > {subgraph} > {node} — {label}"
