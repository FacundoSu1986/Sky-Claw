"""Tests para las emisiones ops.* de Fase 6 (migradas a convención jerárquica en Fase 7).

Cubre:
- Constantes/helpers de tópicos exportados desde sky_claw.core
  (``ops.telemetry.tick``, prefijos ``ops.process``, ``ops.log``).
- Modelos Pydantic OpsProcessChangePayload, OpsSystemLogPayload,
  OpsTelemetryPayload.
- TelemetryDaemon emite ``ops.telemetry.tick`` (nuevo) Y
  ``system.telemetry.metrics`` (legacy).
- TelemetryDaemon incluye ``uptime_s`` en el payload ``ops.telemetry.tick``.
- TelemetryDaemon emite ``ops.log.warning`` cuando RAM supera umbral.
- Cooldown evita emisión duplicada de advertencia RAM en ventana corta.
- Los nuevos tópicos matchean los patrones ``fnmatch`` del puente WS
  (``ops.<cat>.*``), garantizando propagación al frontend.
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pytest

from sky_claw.core.event_bus import (
    OPS_LOG_TOPIC_PREFIX,
    OPS_PROCESS_TOPIC_PREFIX,
    OPS_TELEMETRY_TOPIC,
    CoreEventBus,
    Event,
    ops_log_topic,
    ops_process_topic,
)
from sky_claw.core.event_payloads import (
    OpsProcessChangePayload,
    OpsSystemLogPayload,
    OpsTelemetryPayload,
)


# ---------------------------------------------------------------------------
# Constantes y helpers de tópico (Fase 7 — convención jerárquica)
# ---------------------------------------------------------------------------


class TestOpsTopicConstants:
    """Verifica que las constantes de tópico tienen los valores correctos."""

    def test_ops_telemetry_topic_is_hierarchical(self) -> None:
        """ops.telemetry.tick usa sub-segmento para matchear ops.telemetry.*"""
        assert OPS_TELEMETRY_TOPIC == "ops.telemetry.tick"
        assert fnmatch.fnmatch(OPS_TELEMETRY_TOPIC, "ops.telemetry.*")

    def test_ops_process_topic_prefix(self) -> None:
        assert OPS_PROCESS_TOPIC_PREFIX == "ops.process"

    def test_ops_log_topic_prefix(self) -> None:
        assert OPS_LOG_TOPIC_PREFIX == "ops.log"

    def test_ops_process_topic_helper(self) -> None:
        assert ops_process_topic("started") == "ops.process.started"
        assert ops_process_topic("completed") == "ops.process.completed"
        assert ops_process_topic("error") == "ops.process.error"

    def test_ops_process_topics_match_ws_pattern(self) -> None:
        """Los tópicos ops.process.<state> deben matchear ops.process.* del WS."""
        for state in ("started", "completed", "error"):
            topic = ops_process_topic(state)
            assert fnmatch.fnmatch(topic, "ops.process.*"), (
                f"Tópico {topic!r} no matchea ops.process.*"
            )

    def test_ops_log_topic_helper(self) -> None:
        assert ops_log_topic("info") == "ops.log.info"
        assert ops_log_topic("warning") == "ops.log.warning"
        assert ops_log_topic("error") == "ops.log.error"
        assert ops_log_topic("critical") == "ops.log.critical"

    def test_ops_log_topics_match_ws_pattern(self) -> None:
        """Los tópicos ops.log.<level> deben matchear ops.log.* del WS."""
        for level in ("info", "warning", "error", "critical"):
            topic = ops_log_topic(level)
            assert fnmatch.fnmatch(topic, "ops.log.*"), (
                f"Tópico {topic!r} no matchea ops.log.*"
            )

    def test_all_topics_start_with_ops_prefix(self) -> None:
        samples = (
            OPS_TELEMETRY_TOPIC,
            ops_process_topic("started"),
            ops_log_topic("info"),
        )
        for topic in samples:
            assert topic.startswith("ops."), (
                f"Tópico {topic!r} no comienza con 'ops.'"
            )


# ---------------------------------------------------------------------------
# Payloads — validación de esquemas
# ---------------------------------------------------------------------------


class TestOpsTelemetryPayload:
    """Verifica el modelo OpsTelemetryPayload."""

    def test_valid_construction(self) -> None:
        p = OpsTelemetryPayload(
            cpu=12.5,
            ram_mb=512.0,
            ram_percent=45.2,
            uptime_s=120.0,
        )
        assert p.cpu == 12.5
        assert p.ram_mb == 512.0
        assert p.ram_percent == 45.2
        assert p.uptime_s == 120.0
        assert isinstance(p.ts, float)

    def test_to_log_dict_includes_all_fields(self) -> None:
        p = OpsTelemetryPayload(cpu=0.0, ram_mb=0.0, ram_percent=0.0, uptime_s=0.0)
        d = p.to_log_dict()
        for key in ("cpu", "ram_mb", "ram_percent", "uptime_s", "ts"):
            assert key in d

    def test_frozen_immutable(self) -> None:
        p = OpsTelemetryPayload(cpu=1.0, ram_mb=1.0, ram_percent=1.0, uptime_s=1.0)
        with pytest.raises(Exception):
            p.cpu = 99.0  # type: ignore[misc]


class TestOpsProcessChangePayload:
    """Verifica el modelo OpsProcessChangePayload."""

    def test_started_state(self) -> None:
        p = OpsProcessChangePayload(
            process_id="proc-001",
            tool_name="DynDOLOD",
            state="started",
        )
        assert p.state == "started"
        assert p.exit_code is None
        assert p.duration_seconds is None

    def test_completed_state_with_duration(self) -> None:
        p = OpsProcessChangePayload(
            process_id="proc-001",
            tool_name="LOOT",
            state="completed",
            exit_code=0,
            duration_seconds=42.5,
        )
        assert p.state == "completed"
        assert p.exit_code == 0
        assert p.duration_seconds == 42.5

    def test_error_state_with_message(self) -> None:
        p = OpsProcessChangePayload(
            process_id="proc-002",
            tool_name="Synthesis",
            state="error",
            error_message="Plugin not found",
        )
        assert p.state == "error"
        assert p.error_message == "Plugin not found"

    def test_invalid_state_rejected(self) -> None:
        with pytest.raises(Exception):
            OpsProcessChangePayload(
                process_id="x",
                tool_name="x",
                state="unknown",  # type: ignore[arg-type]
            )


class TestOpsSystemLogPayload:
    """Verifica el modelo OpsSystemLogPayload."""

    def test_warning_level(self) -> None:
        p = OpsSystemLogPayload(level="warning", message="RAM alta", source="telemetry-daemon")
        assert p.level == "warning"
        assert p.message == "RAM alta"
        assert p.source == "telemetry-daemon"

    def test_all_valid_levels(self) -> None:
        for lvl in ("info", "warning", "error", "critical"):
            p = OpsSystemLogPayload(level=lvl, message="msg", source="src")  # type: ignore[arg-type]
            assert p.level == lvl

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(Exception):
            OpsSystemLogPayload(
                level="debug",  # type: ignore[arg-type]
                message="x",
                source="x",
            )

    def test_to_log_dict(self) -> None:
        p = OpsSystemLogPayload(level="info", message="ok", source="test")
        d = p.to_log_dict()
        assert d["level"] == "info"
        assert d["message"] == "ok"
        assert d["source"] == "test"


# ---------------------------------------------------------------------------
# TelemetryDaemon — emisión de ops.*
# ---------------------------------------------------------------------------


def _make_fake_process(cpu: float = 5.0, rss_mb: float = 200.0) -> Any:
    """Retorna un mock de psutil.Process con valores fijos."""
    proc = MagicMock(spec=psutil.Process)
    proc.cpu_percent.return_value = cpu
    mem_info = MagicMock()
    mem_info.rss = int(rss_mb * 1024 * 1024)
    proc.memory_info.return_value = mem_info
    return proc


def _make_fake_vmem(percent: float = 50.0) -> Any:
    """Retorna un mock de psutil.virtual_memory() con porcentaje fijo."""
    vmem = MagicMock()
    vmem.percent = percent
    return vmem


@pytest.mark.asyncio
async def test_telemetry_daemon_emits_ops_telemetry_tick() -> None:
    """TelemetryDaemon publica al tópico ops.telemetry.tick en cada ciclo."""
    from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon

    bus = CoreEventBus()
    ops_events: list[Event] = []

    async def capture(event: Event) -> None:
        ops_events.append(event)

    # Usa el patrón real del puente WS para garantizar que matchea.
    bus.subscribe("ops.telemetry.*", capture)

    fake_proc = _make_fake_process(cpu=10.0, rss_mb=256.0)
    fake_vmem = _make_fake_vmem(percent=40.0)

    with (
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.Process", return_value=fake_proc),
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.virtual_memory", return_value=fake_vmem),
    ):
        daemon = TelemetryDaemon(event_bus=bus, interval=0.05)
        await bus.start()
        await daemon.start()
        await asyncio.sleep(0.25)
        await daemon.stop()
        await bus.stop()

    assert len(ops_events) >= 2
    ev = ops_events[0]
    assert ev.topic == "ops.telemetry.tick"
    assert ev.source == "telemetry-daemon"
    assert "cpu" in ev.payload
    assert "ram_mb" in ev.payload
    assert "ram_percent" in ev.payload
    assert "uptime_s" in ev.payload


@pytest.mark.asyncio
async def test_telemetry_daemon_still_emits_legacy_topic() -> None:
    """system.telemetry.metrics (legacy) sigue siendo emitido — backward compat."""
    from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon

    bus = CoreEventBus()
    legacy_events: list[Event] = []

    async def capture(event: Event) -> None:
        legacy_events.append(event)

    bus.subscribe("system.telemetry.*", capture)

    fake_proc = _make_fake_process()
    fake_vmem = _make_fake_vmem()

    with (
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.Process", return_value=fake_proc),
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.virtual_memory", return_value=fake_vmem),
    ):
        daemon = TelemetryDaemon(event_bus=bus, interval=0.05)
        await bus.start()
        await daemon.start()
        await asyncio.sleep(0.2)
        await daemon.stop()
        await bus.stop()

    assert len(legacy_events) >= 1
    assert all(ev.topic == "system.telemetry.metrics" for ev in legacy_events)


@pytest.mark.asyncio
async def test_telemetry_daemon_uptime_s_grows_over_time() -> None:
    """uptime_s en ops.telemetry.tick crece monótonamente entre ciclos."""
    from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon

    bus = CoreEventBus()
    ops_events: list[Event] = []

    async def capture(event: Event) -> None:
        ops_events.append(event)

    bus.subscribe("ops.telemetry.*", capture)

    fake_proc = _make_fake_process()
    fake_vmem = _make_fake_vmem()

    with (
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.Process", return_value=fake_proc),
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.virtual_memory", return_value=fake_vmem),
    ):
        daemon = TelemetryDaemon(event_bus=bus, interval=0.05)
        await bus.start()
        await daemon.start()
        await asyncio.sleep(0.35)
        await daemon.stop()
        await bus.stop()

    assert len(ops_events) >= 3
    uptimes = [ev.payload["uptime_s"] for ev in ops_events]
    # Uptime debe ser no-decreciente
    assert uptimes == sorted(uptimes), f"uptime_s no es monótono: {uptimes}"


@pytest.mark.asyncio
async def test_telemetry_daemon_emits_ops_log_warning_on_ram_threshold() -> None:
    """Cuando RAM supera el umbral, se emite ops.log.warning."""
    from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon

    bus = CoreEventBus()
    log_events: list[Event] = []

    async def capture(event: Event) -> None:
        log_events.append(event)

    bus.subscribe("ops.log.*", capture)

    fake_proc = _make_fake_process(cpu=5.0, rss_mb=100.0)
    # RAM al 95% — supera el umbral por defecto de 80%
    fake_vmem = _make_fake_vmem(percent=95.0)

    with (
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.Process", return_value=fake_proc),
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.virtual_memory", return_value=fake_vmem),
    ):
        # cooldown_s=0 para que cada ciclo pueda emitir (test rápido)
        daemon = TelemetryDaemon(
            event_bus=bus,
            interval=0.05,
            ram_warn_threshold_pct=80.0,
            ram_warn_cooldown_s=0.0,
        )
        await bus.start()
        await daemon.start()
        await asyncio.sleep(0.25)
        await daemon.stop()
        await bus.stop()

    assert len(log_events) >= 1
    ev = log_events[0]
    assert ev.topic == "ops.log.warning"
    assert ev.payload["level"] == "warning"
    assert "95.0" in ev.payload["message"]
    assert ev.payload["source"] == "telemetry-daemon"


@pytest.mark.asyncio
async def test_telemetry_daemon_no_log_below_ram_threshold() -> None:
    """Cuando RAM está por debajo del umbral, NO se emite ops.log.*."""
    from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon

    bus = CoreEventBus()
    log_events: list[Event] = []

    async def capture(event: Event) -> None:
        log_events.append(event)

    bus.subscribe("ops.log.*", capture)

    fake_proc = _make_fake_process()
    # RAM al 30% — muy por debajo del umbral
    fake_vmem = _make_fake_vmem(percent=30.0)

    with (
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.Process", return_value=fake_proc),
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.virtual_memory", return_value=fake_vmem),
    ):
        daemon = TelemetryDaemon(event_bus=bus, interval=0.05, ram_warn_threshold_pct=80.0)
        await bus.start()
        await daemon.start()
        await asyncio.sleep(0.2)
        await daemon.stop()
        await bus.stop()

    assert len(log_events) == 0, "No debe emitirse ops.log.* con RAM baja"


@pytest.mark.asyncio
async def test_telemetry_daemon_ram_cooldown_limits_warnings() -> None:
    """El cooldown evita emitir más de 1 advertencia por ventana."""
    from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon

    bus = CoreEventBus()
    log_events: list[Event] = []

    async def capture(event: Event) -> None:
        log_events.append(event)

    bus.subscribe("ops.log.*", capture)

    fake_proc = _make_fake_process()
    fake_vmem = _make_fake_vmem(percent=90.0)

    with (
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.Process", return_value=fake_proc),
        patch("sky_claw.orchestrator.telemetry_daemon.psutil.virtual_memory", return_value=fake_vmem),
    ):
        # cooldown de 10 minutos → en 0.3s sólo debe haber 1 advertencia
        daemon = TelemetryDaemon(
            event_bus=bus,
            interval=0.05,
            ram_warn_threshold_pct=80.0,
            ram_warn_cooldown_s=600.0,
        )
        await bus.start()
        await daemon.start()
        await asyncio.sleep(0.35)
        await daemon.stop()
        await bus.stop()

    assert len(log_events) == 1, (
        f"Cooldown debe limitar a 1 advertencia; se emitieron {len(log_events)}"
    )
