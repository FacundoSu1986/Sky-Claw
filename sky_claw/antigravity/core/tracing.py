"""OpenTelemetry distributed tracing for sky_claw.

Reads OTEL_EXPORTER_OTLP_ENDPOINT from env. If absent or empty, installs a
NoOpTracerProvider so the rest of the code can import get_tracer() unconditionally
without crashing in dev/CI environments that lack a collector.

Call configure_tracing() once at app startup (AppContext._start_full_inner).
Call shutdown_tracing() on shutdown — it flushes pending spans.
"""

from __future__ import annotations

import logging
import os
from importlib import metadata
from typing import Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NoOpTracerProvider

logger = logging.getLogger(__name__)

SERVICE_NAME_VALUE: Final[str] = "sky-claw"

_provider: TracerProvider | NoOpTracerProvider | None = None


def _resolve_service_version() -> str:
    try:
        return metadata.version(SERVICE_NAME_VALUE)
    except metadata.PackageNotFoundError:
        return "unknown"


def configure_tracing() -> TracerProvider | NoOpTracerProvider:
    """Initialize the global TracerProvider.

    Returns a NoOpTracerProvider if OTEL_EXPORTER_OTLP_ENDPOINT is not set,
    ensuring the app never crashes when no collector is running.
    """
    global _provider
    if _provider is not None:
        return _provider

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("tracing_disabled: OTEL_EXPORTER_OTLP_ENDPOINT not set")
        noop: NoOpTracerProvider = NoOpTracerProvider()
        trace.set_tracer_provider(noop)
        _provider = noop
        return noop

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME_VALUE,
            "service.version": _resolve_service_version(),
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _provider = provider
    logger.info("tracing_enabled", extra={"endpoint": endpoint})
    return provider


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the TracerProvider. Safe to call multiple times."""
    global _provider
    if _provider is None:
        return
    if isinstance(_provider, TracerProvider):
        _provider.shutdown()
    _provider = None
    logger.info("tracing_shutdown")


def get_tracer(name: str) -> trace.Tracer:
    """Return a tracer for the given instrumentation scope name."""
    return trace.get_tracer(name)
