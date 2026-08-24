"""OTLP metrics backend — export tool-call events as OpenTelemetry spans.

One span per ``tools/call``, exported to a local OTel Collector which fans out
to Jaeger (human UI) and otlp-mcp (agent-queryable MCP surface).

Why spans rather than Prometheus metrics: Apo's telemetry is *per-event
forensics* ("which calls did session X make, in what order, with what error
shape"). Prometheus stores aggregates and cannot answer that, and
``conversation_id`` as a metric label is unbounded cardinality. Aggregate
KPIs are derived downstream by the collector's ``spanmetrics`` connector, so
we instrument once and get both shapes.

Privacy is enforced upstream: ``tool_metrics.record_call`` applies the vault's
``TelemetryPolicy`` before dispatching here, so ``note_path`` / ``heading`` /
``chunk_hash`` are already filtered. Never read raw tool arguments in this
module — that would bypass the contract.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://localhost:4318/v1/traces"

# Event keys that map to span attributes as-is, under the ``apo.`` namespace.
_ATTR_KEYS = (
    "tool",
    "ok",
    "error",
    "vault_id",
    "conversation_id",
    "apo_version",
    "req_bytes",
    "resp_bytes",
    "folder_set",
    "fields_set",
    "expected_mtime_set",
    "used_alias",
    "ops_count",
    "error_shape",
    "note_path",
    "path_hash",
    "heading",
    "chunk_hash",
)

# Renames applied on the way out, so span attributes read naturally.
_ATTR_RENAME = {
    "apo_version": "apo.version",
    "conversation_id": "apo.session_id",
}

_provider: Any = None
_tracer: Any = None
_init_lock = threading.Lock()
_init_failed = False


def otlp_endpoint() -> str:
    """Traces endpoint — env override, else the local collector."""
    raw = os.environ.get("APO_OTLP_ENDPOINT", "").strip()
    return raw or DEFAULT_ENDPOINT


def _build_tracer(endpoint: str, service_name: str) -> Any:
    """Create an isolated TracerProvider. Never touches OTel global state.

    fastmcp pulls in ``opentelemetry-api``; hijacking the global provider could
    interfere with it (or with a future in-process tracing setup), so we keep
    our own provider and hand out tracers from it directly.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from apo_engine.tool_metrics import engine_version

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": engine_version(),
        }
    )
    provider = TracerProvider(resource=resource)
    # Batched: export happens off the request path. The DuckDB backend wrote
    # synchronously inside every tool call; this is strictly less overhead.
    #
    # The delay is deliberately short. Under stdio the client kills this
    # process when the session ends, and anything still queued dies with it —
    # a 5s window silently lost whole short sessions in testing.
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint),
            max_queue_size=2048,
            schedule_delay_millis=1000,
        )
    )
    atexit.register(_shutdown, provider)
    _install_signal_flush(provider)
    return provider


def _shutdown(provider: Any) -> None:
    """Flush pending spans at process exit — stdio servers die abruptly."""
    try:
        provider.shutdown()
    except Exception:  # noqa: BLE001 - best effort at interpreter teardown
        pass


def _install_signal_flush(provider: Any) -> None:
    """Flush on SIGTERM/SIGINT.

    atexit does not run when the process is signalled, and MCP clients
    terminate stdio servers rather than asking them to stop. Without this the
    final (often the only) spans of a session never leave the process.

    Chains to the previous handler so we do not change shutdown behaviour.
    """
    import signal

    def _make(sig: int):
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):
            return
        if not callable(previous) and previous not in (signal.SIG_DFL, signal.SIG_IGN):
            return

        def _handler(signum, frame):  # type: ignore[no-untyped-def]
            try:
                provider.force_flush(3000)
            except Exception:  # noqa: BLE001 - never block shutdown
                pass
            if callable(previous):
                previous(signum, frame)
            elif previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not the main thread — atexit remains as the fallback.
            pass

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            _make(sig)


def _get_tracer(endpoint: str, service_name: str) -> Any:
    """Lazily build the tracer; fail closed and stay quiet after one warning."""
    global _provider, _tracer, _init_failed
    if _tracer is not None:
        return _tracer
    if _init_failed:
        return None
    with _init_lock:
        if _tracer is not None:
            return _tracer
        if _init_failed:
            return None
        try:
            _provider = _build_tracer(endpoint, service_name)
            _tracer = _provider.get_tracer("apo_engine")
        except ImportError:
            _init_failed = True
            log.warning(
                "OTLP metrics backend requested but opentelemetry is not installed; "
                "telemetry disabled. Install with: pip install 'apo-engine[otlp]'"
            )
            return None
        except Exception as exc:  # noqa: BLE001 - telemetry must never break tools
            _init_failed = True
            log.warning("OTLP metrics backend init failed (%s); telemetry disabled", exc)
            return None
    return _tracer


def _span_attributes(collection: str, event: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {"apo.collection": collection}
    for key in _ATTR_KEYS:
        val = event.get(key)
        if val is None:
            continue
        name = _ATTR_RENAME.get(key, f"apo.{key}")
        if isinstance(val, (list, tuple)):
            # error_shape is a list of "type:loc" fingerprints — never values.
            attrs[name] = [str(v) for v in val]
        elif isinstance(val, (bool, int, float, str)):
            attrs[name] = val
        else:
            attrs[name] = str(val)
    return attrs


class OtlpBackend:
    """Write-only span sink. Reads are served by otlp-mcp / spanmetrics."""

    def __init__(self, endpoint: str | None = None, *, service_name: str = "apo-engine") -> None:
        self._endpoint = (endpoint or "").strip() or otlp_endpoint()
        self._service_name = service_name

    def status(self) -> dict[str, Any]:
        tracer = _get_tracer(self._endpoint, self._service_name)
        return {
            "backend": "otlp",
            "endpoint": self._endpoint,
            "service_name": self._service_name,
            "reachable": tracer is not None,
        }

    def record(self, collection: str, event: dict[str, Any]) -> None:
        tracer = _get_tracer(self._endpoint, self._service_name)
        if tracer is None:
            return
        try:
            from opentelemetry.trace import SpanKind
            from opentelemetry.trace.status import Status, StatusCode

            # record_call fires immediately after the tool returns, so "now" is
            # the end of the span. event["ts"] is only second-resolution, which
            # would quantise away most call durations.
            end_ns = time.time_ns()
            start_ns = end_ns - int(float(event.get("duration_ms") or 0.0) * 1_000_000)

            tool = str(event.get("tool") or "?")
            span = tracer.start_span(
                f"apo.tool/{tool}",
                kind=SpanKind.SERVER,
                start_time=start_ns,
                attributes=_span_attributes(collection, event),
            )
            if not event.get("ok", True):
                span.set_status(Status(StatusCode.ERROR, str(event.get("error") or "")))
            else:
                span.set_status(Status(StatusCode.OK))
            span.end(end_time=end_ns)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break tools
            log.debug("OTLP span emit failed: %s", exc)

    def read_events(
        self,
        collection: str,
        *,
        days: int | None = None,
        tool: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Not a queryable store — traces live in Jaeger, aggregates in spanmetrics."""
        return []


class FanoutBackend:
    """Write to several backends; read from the first that can answer.

    Used for the DuckDB -> OTLP cutover: spans start flowing before the read
    path moves, so there is never a window with no telemetry surface at all.
    """

    def __init__(self, backends: list[Any]) -> None:
        self._backends = [b for b in backends if b is not None]

    def status(self) -> dict[str, Any]:
        return {
            "backend": "fanout",
            "members": [b.status() for b in self._backends],
        }

    def record(self, collection: str, event: dict[str, Any]) -> None:
        for backend in self._backends:
            try:
                backend.record(collection, event)
            except Exception as exc:  # noqa: BLE001 - one sink must not break another
                log.debug("metrics fanout member failed: %s", exc)

    def read_events(
        self,
        collection: str,
        *,
        days: int | None = None,
        tool: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        for backend in self._backends:
            rows = backend.read_events(
                collection, days=days, tool=tool, conversation_id=conversation_id
            )
            if rows:
                return rows
        return []
