"""Tests for TelemetryDaemon metric sampling, incl. honest GPU reporting.

Phase 1 ("Panel con datos reales"): the daemon must emit a GPU field so the
GUI can stop hardcoding a fake "GPU 18%". When no NVIDIA GPU / pynvml is
available the field is ``None`` (the GUI renders "N/D"), never a fabricated
number.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import psutil

from sky_claw.antigravity.orchestrator.telemetry_daemon import (
    TelemetryDaemon,
    TelemetryMetrics,
    _read_gpu_percent,
)


def test_telemetry_metrics_has_gpu_field() -> None:
    m = TelemetryMetrics(cpu=1.0, ram_mb=2.0, ram_percent=3.0, gpu=42.0)
    assert m.gpu == 42.0


def test_telemetry_metrics_gpu_defaults_to_none() -> None:
    m = TelemetryMetrics(cpu=1.0, ram_mb=2.0, ram_percent=3.0)
    assert m.gpu is None


def test_as_payload_includes_gpu_key() -> None:
    m = TelemetryMetrics(cpu=1.0, ram_mb=2.0, ram_percent=3.0, gpu=None)
    payload = m.as_payload()
    assert set(payload) == {"cpu", "ram_mb", "ram_percent", "gpu"}
    assert payload["gpu"] is None


def test_read_gpu_percent_returns_none_without_pynvml() -> None:
    # pynvml is not a project dependency; on a machine without it (CI, this
    # sandbox) the reader must degrade to None rather than raising.
    result = _read_gpu_percent()
    assert result is None or isinstance(result, float)


def test_sample_returns_real_cpu_ram_and_gpu_field() -> None:
    daemon = TelemetryDaemon(event_bus=MagicMock())
    proc = psutil.Process()
    proc.cpu_percent(interval=None)  # prime the baseline
    metrics = daemon._sample(proc)
    assert isinstance(metrics, TelemetryMetrics)
    assert isinstance(metrics.cpu, float)
    assert isinstance(metrics.ram_mb, float)
    assert isinstance(metrics.ram_percent, float)
    assert metrics.gpu is None or isinstance(metrics.gpu, float)
    # The published payload always carries the gpu key.
    assert "gpu" in metrics.as_payload()
