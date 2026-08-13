"""Forward Apo tool-call events to an OTLP collector (Jaeger via otlp-mcp).

Additive to the DuckDB metrics store: the embedded backend remains the queryable
source for habit KPIs (``vault(action=stats)``), while this module mirrors each
already-privacy-redacted event as one OpenTelemetry span so Apo's real in-process
timing and dimensions land in Jaeger next to Cursor hook / just-recipe spans.

Design:
- **Best-effort, never raises.** Import errors (SDK not installed) or a down
  collector must not affect the tool call or the DuckDB write.
- **Optional dependency.** Install with ``pip install apo-engine[otel]``.
- **Shared trace correlation.** ``trace_id`` is derived from ``conversation_id``
  with the same scheme as the Workbench Cursor hooks (``sha256(cid)[:16]``), and
  spans nest under the synthetic session span seed ``"{cid}:session"`` so Apo
  spans join the same Jaeger trace as ``cursor.session.start`` for that
  conversation.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

from apo_engine import telemetry_contract as tc

SERVICE_NAME = "apo-mcp"
SPAN_NAME = "apo.tool"
DEFAULT_ENDPOINT = "http://127.0.0.1:4318"

# Cache SDK availability so a missing dependency costs one import attempt, not one per call.
_sdk_available: bool | None = None

_OFF_VALUES = {"0", "false", "no", "off"}

# Flags / note-context keys copied verbatim from the (already redacted) event dict.
_FLAG_KEYS = (
    "folder_set",
    "fields_set",
    "expected_mtime_set",
    "used_alias",
    "ops_count",
)
_NOTE_KEYS = ("note_path", "path_hash", "heading", "chunk_hash")


def _contract_otel_setting(vault_root: Path | None) -> bool | None:
    if vault_root is None:
        return None
    data = tc.load_telemetry_contract(vault_root)
    if not isinstance(data, dict):
        return None
    otel = data.get("otel")
    if isinstance(otel, dict) and "export" in otel:
        return bool(otel["export"])
    return None


def otel_export_enabled(vault_root: Path | None = None) -> bool:
    """Resolve export gate: explicit env → vault contract → endpoint auto-on.

    - ``APO_OTEL_EXPORT`` (``1``/``0``) is authoritative when set.
    - Else a vault telemetry contract ``otel.export`` flag, when present.
    - Else auto-on when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
    """
    raw = os.environ.get("APO_OTEL_EXPORT")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in _OFF_VALUES
    contract = _contract_otel_setting(vault_root)
    if contract is not None:
        return contract
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def _endpoint() -> str:
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def _trace_id(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:16], "big") or 1


def _span_id(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[16:24], "big") or 1


def _span_attributes(collection: str, event: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "tool.name": str(event.get("tool") or "?"),
        "tool.category": "mcp",
        "tool.ok": bool(event.get("ok")),
        "collection": collection or "default",
        "duration_ms": float(event.get("duration_ms") or 0.0),
        "req_bytes": int(event.get("req_bytes") or 0),
        "resp_bytes": int(event.get("resp_bytes") or 0),
    }
    for key in ("vault_id", "conversation_id", "apo_version"):
        val = event.get(key)
        if val:
            attrs[key] = str(val)
    if event.get("error"):
        attrs["error"] = str(event["error"])
    shape = event.get("error_shape")
    if isinstance(shape, (list, tuple)) and shape:
        attrs["error_shape"] = ",".join(str(s) for s in shape)
    elif isinstance(shape, str) and shape:
        attrs["error_shape"] = shape
    for key in _FLAG_KEYS:
        if key in event and event[key] is not None:
            attrs[key] = event[key]
    for key in _NOTE_KEYS:
        if event.get(key):
            attrs[key] = str(event[key])
    return attrs


def _sdk_ready() -> bool:
    global _sdk_available
    if _sdk_available is not None:
        return _sdk_available
    try:
        import opentelemetry.sdk.trace  # noqa: F401

        _sdk_available = True
    except Exception:
        _sdk_available = False
    return _sdk_available


def forward_event(
    collection: str,
    event: dict[str, Any],
    *,
    vault_root: Path | None = None,
    span_exporter: Any | None = None,
) -> bool:
    """Emit one OTLP span mirroring ``event``. Returns True if a span was sent.

    Best-effort: any failure (disabled, SDK missing, collector down) returns
    False and never raises.
    """
    if span_exporter is None:
        if not otel_export_enabled(vault_root):
            return False
        if not _sdk_ready():
            return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.id_generator import IdGenerator
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            set_span_in_context,
        )

        cid = str(event.get("conversation_id") or "").strip()
        trace_seed = cid or "anonymous"
        tid = _trace_id(trace_seed)
        # Deterministic span id per (conversation, tool, ts) so retries dedupe.
        span_seed = f"{trace_seed}:apo:{event.get('tool')}:{event.get('ts')}"
        sid = _span_id(span_seed)

        class _FixedIds(IdGenerator):
            def generate_trace_id(self) -> int:
                return tid

            def generate_span_id(self) -> int:
                return sid

            def is_trace_id_random(self) -> bool:
                return False

        resource = Resource.create({"service.name": SERVICE_NAME})
        provider = TracerProvider(resource=resource, id_generator=_FixedIds())
        if span_exporter is not None:
            exporter = span_exporter
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=f"{_endpoint()}/v1/traces")
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        # Nest under the Cursor session span for this conversation (same trace).
        context = None
        if cid:
            parent_ctx = SpanContext(
                trace_id=tid,
                span_id=_span_id(f"{trace_seed}:session"),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            context = set_span_in_context(NonRecordingSpan(parent_ctx))

        duration_ms = float(event.get("duration_ms") or 0.0)
        end_ns = time.time_ns()
        start_ns = end_ns - int(duration_ms * 1_000_000)

        tracer = provider.get_tracer("apo.engine.otel")
        span = tracer.start_span(SPAN_NAME, context=context, start_time=start_ns)
        for key, value in _span_attributes(collection, event).items():
            span.set_attribute(key, value)
        span.end(end_time=end_ns)

        provider.force_flush(timeout_millis=2000)
        if span_exporter is None:
            provider.shutdown()
        return True
    except Exception:
        return False
