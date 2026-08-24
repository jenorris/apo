"""OTLP metrics backend — config resolution, attribute mapping, fan-out."""

from __future__ import annotations

from pathlib import Path

import pytest

from apo_engine import metrics_backend as mb
from apo_engine import otlp_backend as ob


@pytest.fixture(autouse=True)
def _clear_backend_cache(monkeypatch):
    monkeypatch.delenv("APO_METRICS_BACKEND", raising=False)
    monkeypatch.delenv("APO_OTLP_ENDPOINT", raising=False)
    mb._backend_cache = None
    mb._backend_config = None
    yield
    mb._backend_cache = None
    mb._backend_config = None


class _Recorder:
    """Stand-in backend that captures what it was handed."""

    def __init__(self, rows=None):
        self.recorded = []
        self._rows = rows or []

    def status(self):
        return {"backend": "recorder"}

    def record(self, collection, event):
        self.recorded.append((collection, event))

    def read_events(self, collection, *, days=None, tool=None, conversation_id=None):
        return self._rows


class _Exploder(_Recorder):
    def record(self, collection, event):
        raise RuntimeError("sink down")


# --- config resolution -------------------------------------------------


@pytest.mark.parametrize("value", ["otlp", "both", "embedded", "none"])
def test_valid_backends_round_trip(monkeypatch, value):
    monkeypatch.setenv("APO_METRICS_BACKEND", value)
    assert mb.resolve_store_config(None).backend == value


@pytest.mark.parametrize("alias", ["local", "duckdb"])
def test_legacy_aliases_map_to_embedded(monkeypatch, alias):
    """The shipped contract says `duckdb`, which was never a valid value."""
    monkeypatch.setenv("APO_METRICS_BACKEND", alias)
    assert mb.resolve_store_config(None).backend == "embedded"


def test_unknown_backend_warns_instead_of_silently_coercing(monkeypatch, caplog):
    monkeypatch.setenv("APO_METRICS_BACKEND", "carrier-pigeon")
    with caplog.at_level("WARNING"):
        cfg = mb.resolve_store_config(None)
    assert cfg.backend == "embedded"
    assert "carrier-pigeon" in caplog.text


def test_write_target_predicates(monkeypatch):
    cases = {
        "embedded": (True, False),
        "otlp": (False, True),
        "both": (True, True),
        "none": (False, False),
    }
    for backend, (duck, otlp) in cases.items():
        monkeypatch.setenv("APO_METRICS_BACKEND", backend)
        cfg = mb.resolve_store_config(None)
        assert (cfg.writes_duckdb, cfg.writes_otlp) == (duck, otlp), backend


def test_endpoint_env_override(monkeypatch):
    monkeypatch.setenv("APO_OTLP_ENDPOINT", "http://collector.internal:4318/v1/traces")
    assert ob.otlp_endpoint() == "http://collector.internal:4318/v1/traces"


def test_endpoint_defaults_to_local_collector():
    assert ob.otlp_endpoint() == ob.DEFAULT_ENDPOINT


def test_backend_factory_selects_type(monkeypatch):
    """Assert on reported identity, not class identity.

    apo_engine is importable both as an editable install and via PYTHONPATH, so
    the same class can exist as two distinct objects and isinstance() lies.
    """
    monkeypatch.setenv("APO_METRICS_BACKEND", "otlp")
    assert mb.get_backend(None, force=True).status()["backend"] == "otlp"

    monkeypatch.setenv("APO_METRICS_BACKEND", "both")
    status = mb.get_backend(None, force=True).status()
    assert status["backend"] == "fanout"
    assert [m["backend"] for m in status["members"]] == ["embedded", "otlp"]

    monkeypatch.setenv("APO_METRICS_BACKEND", "embedded")
    assert mb.get_backend(None, force=True).status()["backend"] == "embedded"


# --- attribute mapping -------------------------------------------------


def test_span_attributes_namespace_and_renames():
    event = {
        "tool": "search_notes",
        "ok": True,
        "vault_id": "jeremy",
        "conversation_id": "proc-abc123",
        "apo_version": "0.6.4",
        "folder_set": True,
        "duration_ms": 12.5,
    }
    attrs = ob._span_attributes("e9c50a9b35a0", event)

    assert attrs["apo.collection"] == "e9c50a9b35a0"
    assert attrs["apo.tool"] == "search_notes"
    assert attrs["apo.vault_id"] == "jeremy"
    assert attrs["apo.folder_set"] is True
    # renamed for readability in Jaeger
    assert attrs["apo.session_id"] == "proc-abc123"
    assert attrs["apo.version"] == "0.6.4"
    assert "apo.conversation_id" not in attrs
    assert "apo.apo_version" not in attrs
    # duration is span timing, not an attribute
    assert "apo.duration_ms" not in attrs


def test_span_attributes_drop_none_and_stringify_lists():
    attrs = ob._span_attributes(
        "c",
        {
            "tool": "patch_note",
            "error": None,
            "error_shape": ["extra_forbidden:ops.0.set_field.path"],
        },
    )
    assert "apo.error" not in attrs
    assert attrs["apo.error_shape"] == ["extra_forbidden:ops.0.set_field.path"]


def test_span_attributes_carry_no_unlisted_keys():
    """Privacy: only allowlisted keys reach the wire."""
    attrs = ob._span_attributes("c", {"tool": "x", "search_query": "SECRET", "note_body": "SECRET"})
    assert not any("SECRET" in str(v) for v in attrs.values())
    assert "apo.search_query" not in attrs
    assert "apo.note_body" not in attrs


def test_otlp_backend_read_events_is_empty():
    """Not a queryable store — Jaeger and spanmetrics serve reads."""
    assert ob.OtlpBackend().read_events("c") == []


# --- fan-out -----------------------------------------------------------


def test_fanout_writes_to_every_member():
    a, b = _Recorder(), _Recorder()
    ob.FanoutBackend([a, b]).record("coll", {"tool": "search_notes"})
    assert len(a.recorded) == 1 and len(b.recorded) == 1


def test_fanout_survives_a_failing_member():
    """One sink must never take down another, nor the tool call."""
    bad, good = _Exploder(), _Recorder()
    ob.FanoutBackend([bad, good]).record("coll", {"tool": "search_notes"})
    assert len(good.recorded) == 1


def test_fanout_reads_from_first_member_with_rows():
    empty, full = _Recorder(rows=[]), _Recorder(rows=[{"tool": "search_notes"}])
    assert ob.FanoutBackend([empty, full]).read_events("coll") == [{"tool": "search_notes"}]


def test_fanout_ignores_none_members():
    assert ob.FanoutBackend([None, _Recorder()])._backends.__len__() == 1


# --- shutdown flush ----------------------------------------------------


def test_signal_flush_forces_export_and_chains():
    """MCP clients SIGTERM stdio servers; queued spans must not die with them.

    atexit does not run on a signal, so without this the last spans of a
    session — often the whole session — never leave the process.
    """
    import signal

    class _Provider:
        def __init__(self):
            self.flushed = False

        def force_flush(self, timeout_millis=None):
            self.flushed = True

    chained = []

    def _previous(signum, frame):
        chained.append(signum)

    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _previous)
    try:
        provider = _Provider()
        ob._install_signal_flush(provider)
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not _previous, "handler was not installed"

        handler(signal.SIGTERM, None)
        assert provider.flushed, "spans were not flushed on SIGTERM"
        assert chained == [signal.SIGTERM], "previous handler was not chained"
    finally:
        signal.signal(signal.SIGTERM, original)


def test_signal_flush_survives_a_failing_provider():
    import signal

    class _Boom:
        def force_flush(self, timeout_millis=None):
            raise RuntimeError("collector unreachable")

    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        ob._install_signal_flush(_Boom())
        # Must not raise — telemetry never blocks shutdown.
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, original)


# --- session identity --------------------------------------------------


def test_process_session_id_is_stable_and_used_as_fallback():
    from apo_engine import session_context as sc

    sid = sc.process_session_id()
    assert sid == sc.process_session_id()
    # The whole point: an unattributed call is an unanalysable call.
    assert sc.request_conversation_id() == sid


def test_explicit_session_wins_over_process_fallback():
    from apo_engine import session_context as sc

    with sc.bind_request_session(conversation_id="conv-from-client"):
        assert sc.request_conversation_id() == "conv-from-client"
    assert sc.request_conversation_id() == sc.process_session_id()


def test_apo_session_id_env_overrides(monkeypatch):
    """Callers managing their own identity can supply it (CI, gateways)."""
    from apo_engine import session_context as sc

    monkeypatch.setenv("APO_SESSION_ID", "ci-run-42")
    assert sc._initial_session_id() == "ci-run-42"

    monkeypatch.delenv("APO_SESSION_ID", raising=False)
    generated = sc._initial_session_id()
    assert generated.startswith("proc-") and generated != sc._initial_session_id()
