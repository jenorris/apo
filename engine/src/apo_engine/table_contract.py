"""Table contract loader — per-vault row_key / merge defaults for GFM tables.

Active when ``system/contracts/table-contract.schema.yaml`` (or legacy
``system/config/table-contract.schema.yaml``) exists under the vault root.
Used by the indexer (``row_key`` via ``key_column``) and by ``patch_table``
(row lookup by ``key_column``; ``replace_table`` merge defaults and header
synonyms). Fuzzy header matching with ambiguity reject remains the engine
default; this contract only overrides per path pattern.

``tables`` accepts two shapes, both first-match-wins in file order:

.. code-block:: yaml

    tables:                      # list form (template)
      - match: "areas/**/inventory.md"
        key_column: SKU
    tables:                      # mapping form (glob → rule)
      horizon.md: {key_column: start}

``match`` globs use ``fnmatch`` semantics (``*`` crosses ``/``), unlike OKF
``path_rules`` where ``*`` stops at ``/``.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from apo_engine import vault_contracts as vc

TABLE_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "table-contract.schema.yaml",
    Path("system") / "config" / "table-contract.schema.yaml",
)
TABLE_CONTRACT_REL = TABLE_CONTRACT_CANDIDATES[0]

MERGE_MODES = ("replace", "append", "upsert")
KNOWN_TOP_KEYS = frozenset({"table_contract_version", "vault_id", "tables"})
KNOWN_RULE_KEYS = frozenset(
    {"match", "key_column", "merge", "allow_new_columns", "header_synonyms", "notes"}
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
    """Parse table-contract YAML if present. Returns None when missing/unreadable.

    Cached on (path, mtime_ns, size): the indexer asks per file and
    ``patch_table`` asks per op.
    """
    path = resolve_table_contract_path(vault_root, explicit)
    if path is None:
        return None
    return vc.load_yaml_cached(path)


def normalize_rules(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``tables`` as an ordered list of rule dicts, each with a ``match`` key.

    Accepts the list form (``[{match, …}]``) and the mapping form
    (``{"<glob>": {…}}``). Rules without a usable ``match`` are dropped.
    """
    if not data:
        return []
    rules = data.get("tables")
    out: list[dict[str, Any]] = []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            match = str(rule.get("match") or "").strip()
            if match:
                out.append({**rule, "match": match})
    elif isinstance(rules, dict):
        for match, rule in rules.items():
            m = str(match or "").strip()
            if not m or not isinstance(rule, dict):
                continue
            out.append({**rule, "match": m})
    return out


def table_rule_for(vault_root: Path, rel: str) -> dict[str, Any]:
    """Return the first matching ``tables`` rule for a vault-relative path.

    Empty dict when no contract or no pattern matches. First match wins.
    """
    path = (rel or "").replace("\\", "/").lstrip("./")
    for rule in normalize_rules(load_table_contract(vault_root)):
        match = rule["match"]
        if fnmatch.fnmatchcase(path, match) or fnmatch.fnmatch(path, match):
            return rule
    return {}


def key_column_for(vault_root: Path, rel: str) -> str | None:
    """Resolved ``key_column`` for ``rel``, or None (engine first-cell default)."""
    return key_column_of(table_rule_for(vault_root, rel))


def key_column_of(rule: dict[str, Any]) -> str | None:
    col = rule.get("key_column")
    if isinstance(col, str) and col.strip():
        return col.strip()
    return None


def merge_default_of(rule: dict[str, Any]) -> str | None:
    """Rule ``merge`` when it names a supported mode, else None."""
    merge = rule.get("merge")
    if isinstance(merge, str) and merge.strip().lower() in MERGE_MODES:
        return merge.strip().lower()
    return None


def header_synonyms_of(rule: dict[str, Any]) -> dict[str, str]:
    """Rule ``header_synonyms`` (incoming → canonical) as a str→str mapping."""
    raw = rule.get("header_synonyms")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def check_contract(data: dict[str, Any]) -> list[dict[str, str]]:
    """Shape findings for a parsed table contract (advisory, never blocks)."""
    findings = vc.unknown_key_findings(data, KNOWN_TOP_KEYS)
    rules = data.get("tables")
    if rules is None:
        return findings
    if not isinstance(rules, (list, dict)):
        findings.append(
            vc.finding(
                "contract.invalid_shape",
                "tables",
                "tables must be a list of {match, …} rules or a {glob: rule} mapping",
            )
        )
        return findings
    items = (
        [(f"tables[{i}]", r) for i, r in enumerate(rules)]
        if isinstance(rules, list)
        else [(f"tables[{k!r}]", r) for k, r in rules.items()]
    )
    for where, rule in items:
        if not isinstance(rule, dict):
            findings.append(vc.finding("contract.invalid_shape", where, "rule must be a mapping"))
            continue
        if isinstance(rules, list) and not str(rule.get("match") or "").strip():
            findings.append(
                vc.finding("contract.invalid_shape", where, "rule needs a non-empty match glob")
            )
        findings.extend(vc.unknown_key_findings(rule, KNOWN_RULE_KEYS, prefix=where))
        merge = rule.get("merge")
        if merge is not None and merge_default_of(rule) is None:
            findings.append(
                vc.finding(
                    "contract.invalid_value",
                    f"{where}.merge",
                    f"merge {merge!r} not in {'|'.join(MERGE_MODES)}",
                )
            )
        syn = rule.get("header_synonyms")
        if syn is not None and not isinstance(syn, dict):
            findings.append(
                vc.finding(
                    "contract.invalid_shape",
                    f"{where}.header_synonyms",
                    "header_synonyms must be a mapping of incoming → canonical header",
                )
            )
    return findings
