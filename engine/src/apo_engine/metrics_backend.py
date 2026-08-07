"""Pluggable metrics storage — embedded DuckDB (default) or desk-metrics HTTP sink."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apo_engine import telemetry_contract as tc

DEFAULT_LOCAL_URI = "http://127.0.0.1:9473"
DEFAULT_EMBEDDED_PATH = Path.home() / ".apo" / "metrics.duckdb"


@dataclass(frozen=True)
class StoreConfig:
    backend: str  # embedded | local | none
    path: Path
    local_uri: str

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
    if backend not in ("embedded", "local", "none"):
        backend = "embedded"
    raw_path = str(store.get("path") or "").strip()
    path = Path(raw_path).expanduser() if raw_path else _runtime_dir() / "metrics.duckdb"
    local_uri = (
        os.environ.get("APO_METRICS_LOCAL_URI", "").strip()
        or str(store.get("local_uri") or store.get("uri") or "").strip()
        or DEFAULT_LOCAL_URI
    ).rstrip("/")
    return StoreConfig(backend=backend, path=path, local_uri=local_uri)


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


class LocalDeskMetricsBackend:
    """HTTP sink — desk-metrics daemon on loopback."""

    def __init__(self, uri: str) -> None:
        self.uri = uri.rstrip("/")

    def status(self) -> dict[str, Any]:
        try:
            data = self._request("GET", "/v1/health", None)
            return {
                "backend": "local",
                "uri": self.uri,
                "reachable": bool(data.get("ok")),
                "desk_metrics": data,
            }
        except OSError:
            return {
                "backend": "local",
                "uri": self.uri,
                "reachable": False,
                "error": "desk-metrics unreachable",
            }

    def record(self, collection: str, event: dict[str, Any]) -> None:
        body = {
            "source": "apo",
            "namespace": collection,
            "kind": "tool_call",
            **event,
        }
        try:
            self._request("POST", "/v1/events", body)
        except OSError:
            return

    def read_events(
        self,
        collection: str,
        *,
        days: int | None = None,
        tool: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "action": "events",
            "source": "apo",
            "namespace": collection,
        }
        if days is not None:
            body["days"] = days
        if tool:
            body["tool"] = tool
        if conversation_id:
            body["conversation_id"] = conversation_id
        try:
            data = self._request("POST", "/v1/query", body)
        except OSError:
            return []
        events = data.get("events")
        return events if isinstance(events, list) else []

    def workbench_report(self, days: int = 7) -> dict[str, Any]:
        try:
            return self._request(
                "POST",
                "/v1/query",
                {"action": "workbench", "days": days},
            )
        except OSError as e:
            return {"ok": False, "error": "unreachable", "message": str(e)}

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        url = f"{self.uri}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8") if e.fp else "{}"
        except urllib.error.URLError as e:
            raise OSError(str(e)) from e
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": "invalid_json"}
        return parsed if isinstance(parsed, dict) else {"ok": False}


_backend_cache: MetricsBackend | None = None
_backend_config: StoreConfig | None = None


def get_backend(vault_root: Path | None = None, *, force: bool = False) -> MetricsBackend:
    global _backend_cache, _backend_config
    cfg = resolve_store_config(vault_root)
    if not force and _backend_cache is not None and _backend_config == cfg:
        return _backend_cache
    if cfg.backend == "local":
        backend: MetricsBackend = LocalDeskMetricsBackend(cfg.local_uri)
    else:
        backend = EmbeddedDuckDBBackend(cfg.path if cfg.backend == "embedded" else None)
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
