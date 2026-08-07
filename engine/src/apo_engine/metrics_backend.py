"""Pluggable metrics storage — embedded DuckDB (default)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apo_engine import telemetry_contract as tc

DEFAULT_EMBEDDED_PATH = Path.home() / ".apo" / "metrics.duckdb"


@dataclass(frozen=True)
class StoreConfig:
    backend: str  # embedded | none
    path: Path

    @property
    def enabled(self) -> bool:
        return self.backend != "none"


def _runtime_dir() -> Path:
    raw = os.environ.get("APO_DEFERRED_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".apo"


def _store_from_contract(vault_root: Path | None) -> dict[str, Any]:
    data = tc.load_telemetry_contract(vault_root) if vault_root else None
    if not data:
        return {}
    store = data.get("store")
    return store if isinstance(store, dict) else {}


def resolve_store_config(vault_root: Path | None = None) -> StoreConfig:
    """Resolve metrics backend from env, then vault contract, then defaults."""
    store = _store_from_contract(vault_root)
    env_backend = os.environ.get("APO_METRICS_BACKEND", "").strip().lower()
    backend = env_backend or str(store.get("backend") or "embedded").strip().lower()
    if backend in ("local",):
        backend = "embedded"
    if backend not in ("embedded", "none"):
        backend = "embedded"
    raw_path = str(store.get("path") or "").strip()
    path = Path(raw_path).expanduser() if raw_path else _runtime_dir() / "metrics.duckdb"
    return StoreConfig(backend=backend, path=path)


class MetricsBackend(Protocol):
    def status(self) -> dict[str, Any]: ...

    def record(self, collection: str, event: dict[str, Any]) -> None: ...

    def read_events(
        self,
        collection: str,
        *,
        days: int | None = None,
        tool: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


class EmbeddedDuckDBBackend:
    """Default — ~/.apo/metrics.duckdb via tool_metrics DuckDB helpers."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        from apo_engine.tool_metrics import metrics_db_path

        return metrics_db_path(self._path)

    def status(self) -> dict[str, Any]:
        p = self.path
        return {
            "backend": "embedded",
            "path": str(p),
            "reachable": p.parent.exists(),
            "db_exists": p.is_file(),
        }

    def record(self, collection: str, event: dict[str, Any]) -> None:
        from apo_engine import tool_metrics as tm

        tm._embedded_record(collection, event, self._path)

    def read_events(
        self,
        collection: str,
        *,
        days: int | None = None,
        tool: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from apo_engine import tool_metrics as tm

        return tm._embedded_read_events(
            collection,
            days=days,
            tool=tool,
            conversation_id=conversation_id,
            path=self._path,
        )


_backend_cache: MetricsBackend | None = None
_backend_config: StoreConfig | None = None


def get_backend(vault_root: Path | None = None, *, force: bool = False) -> MetricsBackend:
    global _backend_cache, _backend_config
    cfg = resolve_store_config(vault_root)
    if not force and _backend_cache is not None and _backend_config == cfg:
        return _backend_cache
    backend: MetricsBackend = EmbeddedDuckDBBackend(
        cfg.path if cfg.backend == "embedded" else None
    )
    _backend_cache = backend
    _backend_config = cfg
    return backend


def metrics_enabled(vault_root: Path | None = None) -> bool:
    raw = os.environ.get("APO_TOOL_METRICS")
    if raw is not None and str(raw).strip().lower() in ("0", "false", "no", "off"):
        return False
    cfg = resolve_store_config(vault_root)
    if cfg.backend == "none":
        return False
    policy = tc.policy_for_vault(vault_root)
    return policy.enabled
