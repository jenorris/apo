"""Optima contract loader — Stage B merge settings for ``apo-engine watch``.

Active when ``system/contracts/optima-contract.schema.yaml`` (or legacy
``system/config/…``) exists under the vault root. Merge tick is gated by
``refresh.watch.enabled`` (default false until Stage B is enabled on the desk).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apo_engine import vault_contracts as vc

OPTIMA_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "optima-contract.schema.yaml",
    Path("system") / "config" / "optima-contract.schema.yaml",
)
OPTIMA_CONTRACT_REL = OPTIMA_CONTRACT_CANDIDATES[0]

IF_MISSING = ("skip", "error")
PROJECTION_ROLE = "domain_projection"
PROJECTION_DOMAINS = ("atlas", "work")


def _positive_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class SourceSpec:
    id: str
    role: str
    path: str
    vault: str | None = None
    if_missing: str = "skip"  # skip | error
    domain: str | None = None  # domain_projection role only: atlas | work
    stale_after_minutes: int = 120  # domain_projection role only


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
    return vc.load_yaml_cached(path)


def clear_optima_contract_cache() -> None:
    """Test helper — drop the shared contract YAML cache."""
    vc.clear_yaml_cache()


def check_contract(data: dict[str, Any]) -> list[dict[str, str]]:
    """Shape findings for the Stage B merge knobs only.

    The optima contract is a broad agent IR (schemas, query hints, commands)
    read by vault scripts too, so unknown top-level keys are not flagged —
    only the ``refresh`` block the watcher's merge tick actually parses.
    """
    findings: list[dict[str, str]] = []
    refresh = data.get("refresh")
    f = vc.mapping_finding(refresh, "refresh")
    if f:
        return [f]
    if not isinstance(refresh, dict):
        return findings
    watch = refresh.get("watch")
    f = vc.mapping_finding(watch, "refresh.watch")
    if f:
        findings.append(f)
    elif isinstance(watch, dict) and watch.get("interval_seconds") is not None:
        try:
            float(watch["interval_seconds"])
        except (TypeError, ValueError):
            findings.append(
                vc.finding(
                    "contract.invalid_value",
                    "refresh.watch.interval_seconds",
                    f"interval_seconds {watch['interval_seconds']!r} is not a number (60 used)",
                )
            )
    sources = refresh.get("sources")
    if sources is not None and not isinstance(sources, list):
        findings.append(
            vc.finding("contract.invalid_shape", "refresh.sources", "sources must be a list")
        )
    for i, row in enumerate(sources if isinstance(sources, list) else []):
        where = f"refresh.sources[{i}]"
        if not isinstance(row, dict):
            findings.append(vc.finding("contract.invalid_shape", where, "source must be a mapping"))
            continue
        if not str(row.get("path") or "").strip():
            findings.append(
                vc.finding("contract.invalid_shape", f"{where}.path", "source without path is skipped")
            )
        f = vc.enum_finding(row.get("if_missing"), IF_MISSING, f"{where}.if_missing")
        if f:
            findings.append(f)
        if row.get("role") == PROJECTION_ROLE:
            f = vc.enum_finding(row.get("domain"), PROJECTION_DOMAINS, f"{where}.domain")
            if f:
                findings.append(f)
            elif not row.get("domain"):
                findings.append(
                    vc.finding(
                        "contract.invalid_shape",
                        f"{where}.domain",
                        f"role {PROJECTION_ROLE} requires domain ({' | '.join(PROJECTION_DOMAINS)})",
                    )
                )
    return findings


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
                    domain=(str(row["domain"]).strip() if row.get("domain") else None),
                    stale_after_minutes=_positive_int(row.get("stale_after_minutes"), 120),
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
