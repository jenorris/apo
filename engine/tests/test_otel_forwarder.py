"""Unit tests for OTLP span forwarding (no live collector required)."""
from __future__ import annotations

import unittest
from unittest import mock

import pytest

from apo_engine import otel_forwarder as of

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)


def _sample_event() -> dict:
    return {
        "ts": "2026-08-13T14:00:00Z",
        "tool": "search_notes",
        "ok": True,
        "error": None,
        "duration_ms": 12.5,
        "req_bytes": 40,
        "resp_bytes": 900,
        "vault_id": "work",
        "conversation_id": "conv-abc",
        "apo_version": "0.8.1",
        "folder_set": True,
    }


class EnablementTests(unittest.TestCase):
    def test_env_off_wins(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"APO_OTEL_EXPORT": "0", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:4318"},
            clear=True,
        ):
            self.assertFalse(of.otel_export_enabled())

    def test_env_on_wins(self) -> None:
        with mock.patch.dict("os.environ", {"APO_OTEL_EXPORT": "1"}, clear=True):
            self.assertTrue(of.otel_export_enabled())

    def test_auto_on_with_endpoint(self) -> None:
        with mock.patch.dict(
            "os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:4318"}, clear=True
        ):
            self.assertTrue(of.otel_export_enabled())

    def test_default_off(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(of.otel_export_enabled())


class ForwardSpanTests(unittest.TestCase):
    def test_span_attributes_and_trace_correlation(self) -> None:
        exporter = InMemorySpanExporter()
        sent = of.forward_event("work", _sample_event(), span_exporter=exporter)
        self.assertTrue(sent)

        spans = exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]

        self.assertEqual(span.name, of.SPAN_NAME)
        self.assertEqual(
            span.resource.attributes.get("service.name"), of.SERVICE_NAME
        )
        # trace_id derived from conversation_id (shared with Cursor hook spans)
        self.assertEqual(span.context.trace_id, of._trace_id("conv-abc"))
        # nested under the synthetic session span for the conversation
        self.assertIsNotNone(span.parent)
        self.assertEqual(span.parent.span_id, of._span_id("conv-abc:session"))

        attrs = dict(span.attributes)
        self.assertEqual(attrs["tool.name"], "search_notes")
        self.assertEqual(attrs["tool.category"], "mcp")
        self.assertEqual(attrs["tool.ok"], True)
        self.assertEqual(attrs["duration_ms"], 12.5)
        self.assertEqual(attrs["req_bytes"], 40)
        self.assertEqual(attrs["resp_bytes"], 900)
        self.assertEqual(attrs["vault_id"], "work")
        self.assertEqual(attrs["conversation_id"], "conv-abc")
        self.assertEqual(attrs["collection"], "work")
        self.assertEqual(attrs["folder_set"], True)

        # span width reflects duration_ms
        width_ms = (span.end_time - span.start_time) / 1_000_000
        self.assertAlmostEqual(width_ms, 12.5, delta=0.5)

    def test_error_shape_flattened(self) -> None:
        exporter = InMemorySpanExporter()
        event = _sample_event()
        event.update(ok=False, error="validation_error", error_shape=["missing:path", "int:limit"])
        of.forward_event("work", event, span_exporter=exporter)
        span = exporter.get_finished_spans()[0]
        attrs = dict(span.attributes)
        self.assertEqual(attrs["tool.ok"], False)
        self.assertEqual(attrs["error"], "validation_error")
        self.assertEqual(attrs["error_shape"], "missing:path,int:limit")

    def test_disabled_is_noop(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            # No injected exporter → gate applies; default env is off.
            self.assertFalse(of.forward_event("work", _sample_event()))


class RecordCallIntegrationTests(unittest.TestCase):
    def test_record_call_forwards(self) -> None:
        from apo_engine import tool_metrics

        captured: list[tuple] = []

        def _capture(collection, event, *, vault_root=None):  # type: ignore[no-untyped-def]
            captured.append((collection, event.get("tool"), event.get("conversation_id")))
            return True

        # String targets (not patch.object) so patches resolve through
        # sys.modules at call time. test_mcp_validation_hints deletes and
        # re-execs apo_engine.* modules, and record_call imports
        # otel_forwarder.forward_event lazily — a fresh module object at call
        # time. Forcing the enable gate also keeps this independent of ambient
        # env / metrics_backend module cache left by sibling tests.
        from apo_engine.telemetry_contract import TelemetryPolicy

        with mock.patch(
            "apo_engine.tool_metrics.metrics_enabled", return_value=True
        ), mock.patch(
            "apo_engine.telemetry_contract.policy_for_vault",
            return_value=TelemetryPolicy(),
        ), mock.patch(
            "apo_engine.otel_forwarder.forward_event", side_effect=_capture
        ):
            tool_metrics.record_call(
                collection="work",
                tool="write_note",
                ok=True,
                duration_ms=3.0,
                conversation_id="conv-xyz",
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "work")
        self.assertEqual(captured[0][1], "write_note")


if __name__ == "__main__":
    unittest.main()
