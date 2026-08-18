"""Optima contract loader — Stage B merge settings for ``apo-engine watch``.

Active when ``system/contracts/optima-contract.schema.yaml`` (or legacy
``system/config/…``) exists under the vault root. Merge tick is gated by
``refresh.watch.enabled`` (default false until Stage B is enabled on the desk).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

OPTIMA_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "optima-contract.schema.yaml",
    Path("system") / "config" / "optima-contract.schema.yaml",
)
OPTIMA_CONTRACT_REL = OPTIMA_CONTRACT_CANDIDATES[0]

_contract_cache_lock = threading.Lock()
_contract_cache: dict[tuple[str, int, int], dict[str, Any]] = {}


@dataclass(frozen=True)
class SourceSpec:
    id: str
    role: str
    path: str
    vault: str | None = None
    if_missing: str = "skip"  # skip | error


@dataclass(frozen=True)
class MergeSettings:
    enabled: bool = False
    interval_seconds: float = 60.0
    opt_out_env: str = "OPTIMA_SYNC"
    sources: tuple[SourceSpec, ...] = ()
    override_rel: str = "override.yaml"
    override_if_missing: str = "skip"
    reachability_rel: str = "system/config/reachability-rules.yaml"
    output_current: str = "current.yaml"
    on_all_sources_missing: str = "degrade_to_free_or_habit"


def resolve_optima_contract_path(
    vault_root: Path, explicit: str | None = None
) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_OPTIMA_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in OPTIMA_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_optima_contract(
    vault_root: Path, explicit: str | None = None
) -> dict[str, Any] | None:
    """Parse optima-contract YAML if present. Cached on (path, mtime_ns, size)."""
    path = resolve_optima_contract_path(vault_root, explicit)
    if path is None:
        return None
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _contract_cache_lock:
        hit = _contract_cache.get(key)
    if hit is not None:
        return dict(hit)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    with _contract_cache_lock:
        if len(_contract_cache) > 64:
            _contract_cache.clear()
        _contract_cache[key] = data
    return dict(data)


def clear_optima_contract_cache() -> None:
    """Test helper — drop mtime cache."""
    with _contract_cache_lock:
        _contract_cache.clear()


def optima_contract_active(vault_root: Path) -> bool:
    """True when live optima-contract YAML exists (vault need not be named optima)."""
    return load_optima_contract(vault_root) is not None


def merge_opted_out(settings: MergeSettings | None = None) -> bool:
    env_name = (settings.opt_out_env if settings else "OPTIMA_SYNC") or "OPTIMA_SYNC"
    raw = os.environ.get(env_name, "1")
    return str(raw).strip().lower() in {"0", "false", "off", "no"}


def merge_settings(vault_root: Path) -> MergeSettings:
    """Parse Stage B merge knobs from the live optima contract (missing → disabled)."""
    data = load_optima_contract(vault_root) or {}
    refresh = data.get("refresh") if isinstance(data.get("refresh"), dict) else {}
    watch = refresh.get("watch") if isinstance(refresh.get("watch"), dict) else {}
    known = data.get("known_paths") if isinstance(data.get("known_paths"), dict) else {}

    enabled = bool(watch.get("enabled", False))
    try:
        interval = float(watch.get("interval_seconds", 60))
    except (TypeError, ValueError):
        interval = 60.0
    interval = max(5.0, interval)

    opt_out = str(refresh.get("opt_out_env") or "OPTIMA_SYNC").strip() or "OPTIMA_SYNC"

    sources_raw = refresh.get("sources")
    sources: list[SourceSpec] = []
    if isinstance(sources_raw, list):
        for i, row in enumerate(sources_raw):
            if not isinstance(row, dict):
                continue
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            sources.append(
                SourceSpec(
                    id=str(row.get("id") or f"source_{i}"),
                    role=str(row.get("role") or row.get("id") or "unknown"),
                    path=path,
                    vault=(str(row["vault"]).strip() if row.get("vault") else None),
                    if_missing=str(row.get("if_missing") or "skip").strip() or "skip",
                )
            )

    local = refresh.get("local") if isinstance(refresh.get("local"), dict) else {}
    override_rel = str(
        local.get("override")
        or known.get("override")
        or "override.yaml"
    ).strip() or "override.yaml"
    override_if_missing = str(local.get("if_missing") or "skip").strip() or "skip"

    reachability = str(
        refresh.get("reachability_rules")
        or known.get("reachability_rules")
        or "system/config/reachability-rules.yaml"
    ).strip()

    output = refresh.get("output") if isinstance(refresh.get("output"), dict) else {}
    output_current = str(
        output.get("current") or known.get("current") or "current.yaml"
    ).strip() or "current.yaml"

    on_missing = str(
        refresh.get("on_all_sources_missing") or "degrade_to_free_or_habit"
    ).strip()

    return MergeSettings(
        enabled=enabled,
        interval_seconds=interval,
        opt_out_env=opt_out,
        sources=tuple(sources),
        override_rel=override_rel,
        override_if_missing=override_if_missing,
        reachability_rel=reachability,
        output_current=output_current,
        on_all_sources_missing=on_missing,
    )


def merge_enabled(vault_root: Path) -> bool:
    """True when contract enables watch merge and env has not opted out."""
    if not optima_contract_active(vault_root):
        return False
    settings = merge_settings(vault_root)
    if not settings.enabled:
        return False
    if merge_opted_out(settings):
        return False
    return True
