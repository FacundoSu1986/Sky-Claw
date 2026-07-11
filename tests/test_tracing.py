from __future__ import annotations

import importlib
import logging
from importlib import metadata

from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider


class TestTracingModule:
    def test_configure_returns_noop_when_no_endpoint(self, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        from sky_claw.antigravity.core import tracing as t

        importlib.reload(t)
        provider = t.configure_tracing()
        tracer = provider.get_tracer("test")
        span = tracer.start_span("test-span")
        assert not span.is_recording()

    def test_configure_returns_sdk_provider_when_endpoint_set(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

        from sky_claw.antigravity.core import tracing as t

        importlib.reload(t)
        provider = t.configure_tracing()
        assert isinstance(provider, SDKTracerProvider)
        provider.shutdown()

    def test_get_tracer_returns_tracer(self, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        from sky_claw.antigravity.core import tracing as t

        importlib.reload(t)
        t.configure_tracing()
        tracer = t.get_tracer("sky_claw.test")
        assert tracer is not None

    def test_shutdown_is_idempotent(self, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        from sky_claw.antigravity.core import tracing as t

        importlib.reload(t)
        t.configure_tracing()
        t.shutdown_tracing()
        t.shutdown_tracing()  # second call — must not raise

    def test_configure_is_idempotent(self, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        from sky_claw.antigravity.core import tracing as t

        importlib.reload(t)
        provider = t.configure_tracing()
        assert t.configure_tracing() is provider

    def test_service_version_falls_back_when_distribution_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

        from sky_claw.antigravity.core import tracing as t

        importlib.reload(t)

        def _raise_pkg_not_found(_: str) -> str:
            raise metadata.PackageNotFoundError

        monkeypatch.setattr(t.metadata, "version", _raise_pkg_not_found)
        provider = t.configure_tracing()
        assert isinstance(provider, SDKTracerProvider)
        assert provider.resource.attributes["service.version"] == "unknown"
        provider.shutdown()


class TestTracingLogCorrelation:
    def test_trace_id_injected_in_log_record(self, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        import importlib

        from sky_claw.antigravity.core import tracing as t

        importlib.reload(t)
        t.configure_tracing()

        from sky_claw.logging_config import CorrelationFilter

        filt = CorrelationFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        filt.filter(record)
        assert hasattr(record, "trace_id")


class TestJsonFormatterTraceId:
    """Ítem 4 sub-1: trace_id debe ser un campo REQUERIDO explícito del JSON
    formatter (paridad con correlation_id), no depender del extra-merge de
    pythonjsonlogger — que solo lo emite si el record ya trae el atributo."""

    def test_trace_id_es_campo_requerido_del_json_formatter(self) -> None:
        import json as _json

        from pythonjsonlogger import json as _pjl

        from sky_claw.logging_config import _JSON_LOG_FORMAT

        formatter = _pjl.JsonFormatter(_JSON_LOG_FORMAT)
        # Record SIN trace_id seteado: si trace_id es campo requerido, la clave
        # aparece igual (valor null); si solo fuese extra-merge, faltaría.
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hola",
            args=(),
            exc_info=None,
        )
        record.correlation_id = "cid-1"  # type: ignore[attr-defined]
        out = _json.loads(formatter.format(record))
        assert "trace_id" in out
        assert "correlation_id" in out


class TestSyncEngineSpans:
    def test_sync_engine_imports_get_tracer(self) -> None:
        from sky_claw.antigravity.orchestrator import sync_engine

        with open(sync_engine.__file__, encoding="utf-8") as fh:
            src = fh.read()
        assert "get_tracer" in src
        assert "sync.batch" in src
        assert "sync.mod" in src


class TestAppContextTracingWiring:
    def test_app_context_references_tracing(self) -> None:
        from sky_claw import app_context

        with open(app_context.__file__, encoding="utf-8") as fh:
            src = fh.read()
        assert "configure_tracing" in src
        assert "shutdown_tracing" in src
        assert "push_async_callback" in src
