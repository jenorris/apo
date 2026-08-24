"""Pluggable metrics storage — OTLP spans, embedded DuckDB, or both."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apo_engine import telemetry_contract as tc

log = logging.getLogger(__name__)

DEFAULT_EMBEDDED_PATH = Path.home() / ".apo" / "metrics.duckdb"

VALID_BACKENDS = ("embedded", "otlp", "both", "none")


@dataclass(frozen=True)
class StoreConfig:
    backend: str  # embedded | otlp | both | none
    path: Path
    endpoint: str = ""

    @property
    def enabled(self) -> bool:
        return self.backend != "none"

    @property
    def writes_duckdb(self) -> bool:
        return self.backend in ("embedded", "both")

    @property
    def writes_otlp(self) -> bool:
        return self.backend in ("otlp", "both")


def _runtime_dir() -> Path:
    raw = os.environ.get("APO_DEFERRED_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".apo"


def _store_from_contract(vault_root: Path | None) -> dict[str, Any]:
    data = tc.load_telemetry_contract(vault_root) if vault_root else None
    if not data:
        return {}
    store = data.get("store")
    return store if isinstance(store, dict) else {}


# Historical spellings that mean "embedded DuckDB".
_BACKEND_ALIASES = {"local": "embedded", "duckdb": "embedded"}


def resolve_store_config(vault_root: Path | None = None) -> StoreConfig:
    """Resolve metrics backend from env, then vault contract, then defaults."""
    store = _store_from_contract(vault_root)
    env_backend = os.environ.get("APO_METRICS_BACKEND", "").strip().lower()
    backend = env_backend or str(store.get("backend") or "embedded").strip().lower()
    backend = _BACKEND_ALIASES.get(backend, backend)
    if backend not in VALID_BACKENDS:
        # Previously this coerced silently, which is how the shipped contract's
        # invalid `backend: duckdb` went unnoticed. Say something.
        log.warning(
            "unknown metrics store.backend %r; falling back to 'embedded' (valid: %s)",
            backend,
            ", ".join(VALID_BACKENDS),
        )
        backend = "embedded"
    raw_path = str(store.get("path") or "").strip()
    path = Path(raw_path).expanduser() if raw_path else _runtime_dir() / "metrics.duckdb"
    endpoint = str(store.get("endpoint") or "").strip()
    return StoreConfig(backend=backend, path=path, endpoint=endpoint)


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


def _build_backend(cfg: StoreConfig) -> MetricsBackend:
    from apo_engine.otlp_backend import FanoutBackend, OtlpBackend

    if cfg.backend == "otlp":
        return OtlpBackend(cfg.endpoint)
    if cfg.backend == "both":
        # Cutover mode: spans flow before the read path moves off DuckDB, so
        # there is never a window with no telemetry surface. Reads resolve to
        # DuckDB (OtlpBackend.read_events is empty by design).
        return FanoutBackend([EmbeddedDuckDBBackend(cfg.path), OtlpBackend(cfg.endpoint)])
    return EmbeddedDuckDBBackend(cfg.path if cfg.backend == "embedded" else None)


def get_backend(vault_root: Path | None = None, *, force: bool = False) -> MetricsBackend:
    global _backend_cache, _backend_config
    cfg = resolve_store_config(vault_root)
    if not force and _backend_cache is not None and _backend_config == cfg:
        return _backend_cache
    backend: MetricsBackend = _build_backend(cfg)
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
