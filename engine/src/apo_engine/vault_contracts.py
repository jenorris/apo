"""Discover vault contracts for the ``vault`` management tool.

Preferred live home: ``<vault>/system/contracts/*.yaml``.
Legacy (still discovered): ``system/config/*-contract.schema.yaml`` and
``okf-profile.schema.yaml``. When the same contract id exists in both places,
``system/contracts/`` wins.

Discovery is read-only for agents/harnesses. Two things are shared with the
engine-side loaders that live beside each contract's reader (``search_contract``,
``table_contract``, ``git_contract``, …):

* ``load_yaml_cached`` — one mtime/size-keyed YAML cache, so hot paths (indexer
  per file, watcher per tick, ``patch_table`` per op) do not re-parse a contract
  that has not changed.
* ``check_contract`` — per-id shape checks (unknown keys, bad enums, wrong
  container types). Findings are advisory: they ride on ``vault(contracts)``
  entries as ``warnings`` and on ``vault(lint)`` as ``contract.*`` flaws. A
  contract the engine cannot read still degrades to "contract off" at runtime;
  the checks exist so that silent drift (a key the engine never reads, a
  ``tables:`` mapping the loader used to drop) is visible instead of silent.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

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

_yaml_cache_lock = threading.Lock()
_yaml_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
_YAML_CACHE_MAX = 64


def load_yaml_cached(path: Path) -> dict[str, Any] | None:
    """Parse a contract YAML mapping, cached on ``(path, mtime_ns, size)``.

    Returns None when the file is missing, unreadable, invalid YAML, or not a
    mapping — the same "contract off" contract every loader already had. The
    returned dict is a shallow copy; callers may add keys but must not mutate
    nested containers in place.
    """
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _yaml_cache_lock:
        hit = _yaml_cache.get(key)
    if hit is not None:
        return dict(hit)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    with _yaml_cache_lock:
        if len(_yaml_cache) > _YAML_CACHE_MAX:
            _yaml_cache.clear()
        _yaml_cache[key] = data
    return dict(data)


def clear_yaml_cache() -> None:
    """Drop the shared contract YAML cache (tests; contract rewrites in-process)."""
    with _yaml_cache_lock:
        _yaml_cache.clear()


# --- shape-check helpers ---------------------------------------------------------


def finding(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def unknown_key_findings(
    data: dict[str, Any], known: frozenset[str] | set[str], *, prefix: str = ""
) -> list[dict[str, str]]:
    """One ``contract.unknown_key`` finding per top-level key the engine never reads."""
    out: list[dict[str, str]] = []
    for key in data:
        if not isinstance(key, str) or key in known:
            continue
        field = f"{prefix}.{key}" if prefix else key
        out.append(
            finding(
                "contract.unknown_key",
                field,
                f"{field}: not read by the engine (known: {', '.join(sorted(known))})",
            )
        )
    return out


def enum_finding(
    value: Any, allowed: tuple[str, ...] | frozenset[str], field: str
) -> dict[str, str] | None:
    """``contract.invalid_value`` when ``value`` is set and outside ``allowed``."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {a.lower() for a in allowed}:
        return None
    return finding(
        "contract.invalid_value",
        field,
        f"{field} {value!r} not in {'|'.join(sorted(allowed))}",
    )


def str_list_findings(value: Any, field: str) -> list[dict[str, str]]:
    """``contract.invalid_shape`` unless ``value`` is absent or a list of strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [finding("contract.invalid_shape", field, f"{field} must be a list of strings")]
    bad = [i for i, x in enumerate(value) if not isinstance(x, str) or not x.strip()]
    if bad:
        return [
            finding(
                "contract.invalid_shape",
                f"{field}[{bad[0]}]",
                f"{field} entries must be non-empty strings",
            )
        ]
    return []


def mapping_finding(value: Any, field: str) -> dict[str, str] | None:
    """``contract.invalid_shape`` when ``value`` is set and not a mapping."""
    if value is None or isinstance(value, dict):
        return None
    return finding("contract.invalid_shape", field, f"{field} must be a mapping")


def _checker_for(cid: str) -> Callable[[dict[str, Any]], list[dict[str, str]]] | None:
    # Lazy: the contract modules import this module for the helpers above.
    if cid in ("okf-contract", "okf-profile"):
        from apo_engine.okf.contract import check_contract as fn
    elif cid == "search-contract":
        from apo_engine.search_contract import check_contract as fn
    elif cid == "table-contract":
        from apo_engine.table_contract import check_contract as fn
    elif cid == "git-contract":
        from apo_engine.git_contract import check_contract as fn
    elif cid == "mermaid-contract":
        from apo_engine.mermaid_contract import check_contract as fn
    elif cid == "archival-contract":
        from apo_engine.archival_contract import check_contract as fn
    elif cid == "telemetry-contract":
        from apo_engine.telemetry_contract import check_contract as fn
    elif cid == "optima-contract":
        from apo_engine.optima_contract import check_contract as fn
    else:
        return None
    return fn


def check_contract(cid: str, data: dict[str, Any] | None) -> list[dict[str, str]]:
    """Advisory shape findings for one parsed contract; ``[]`` for ids without a checker.

    Contracts the engine does not interpret (usage, read, local-web, vault-private
    schemas) have no checker — their keys belong to other consumers.
    """
    if not isinstance(data, dict):
        return []
    fn = _checker_for(cid)
    if fn is None:
        return []
    try:
        return list(fn(data))
    except Exception as exc:  # a checker bug must never break discovery
        return [finding("contract.check_failed", "$", f"contract check crashed: {exc}")]


# --- discovery --------------------------------------------------------------------


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
        warnings = check_contract(cid, data)
        if warnings:
            out["warnings"] = warnings
    return out


def discover_contracts(vault_root: Path) -> dict[str, dict[str, Any]]:
    """Return contract id → entry for one vault root.

    Entries include parsed ``data`` when readable, plus ``warnings`` (shape
    findings) when the id has a checker and it found drift. Ids from
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


def contract_fingerprint(vault_root: Path) -> str:
    """``rel:mtime_ns`` for every contract file on disk — cache keys for sweeps."""
    root = vault_root.resolve()
    parts: list[str] = []
    paths: list[Path] = []
    contracts_dir = root / CONTRACTS_DIR
    if contracts_dir.is_dir():
        paths.extend(p for p in sorted(contracts_dir.iterdir()) if p.is_file())
    legacy_dir = root / LEGACY_CONFIG_DIR
    if legacy_dir.is_dir():
        for name in _LEGACY_FILES.values():
            p = legacy_dir / name
            if p.is_file():
                paths.append(p)
        paths.extend(sorted(legacy_dir.glob("*-contract.schema.y*ml")))
    seen: set[str] = set()
    for p in paths:
        rel = str(p.relative_to(root)).replace("\\", "/")
        if rel in seen:
            continue
        seen.add(rel)
        try:
            parts.append(f"{rel}:{p.stat().st_mtime_ns}")
        except OSError:
            parts.append(rel)
    return "|".join(parts) or "none"


def summarize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Strip parsed ``data`` — id/path/source/ok (+ error / warnings)."""
    out: dict[str, Any] = {
        "id": entry["id"],
        "path": entry["path"],
        "source": entry["source"],
        "ok": entry.get("ok", True),
    }
    if not out["ok"] and entry.get("error"):
        out["error"] = entry["error"]
    if entry.get("warnings"):
        out["warnings"] = entry["warnings"]
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
