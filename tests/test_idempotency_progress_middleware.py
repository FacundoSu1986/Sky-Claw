"""Tests for FASE 1.5.4 — IdempotencyMiddleware + ProgressMiddleware.

Validates:
- IdempotencyMiddleware rejects duplicate concurrent executions
- IdempotencyMiddleware releases lock on completion/failure
- ProgressMiddleware publishes lifecycle events to CoreEventBus
- ProgressMiddleware is a no-op pass-through when event_bus is None
- Middleware chain preserves the dispatch() contract (returns dict)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.core.event_bus import CoreEventBus, Event
from sky_claw.app.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.app.orchestrator.tool_strategies.middleware import (
    IdempotencyMiddleware,
    ProgressMiddleware,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_strategy(name: str = "test_tool") -> Any:
    """Create a mock ToolStrategy with a .name attribute."""
    strategy = MagicMock()
    strategy.name = name
    return strategy


async def _make_next_call(result: dict[str, Any] | None = None) -> Any:
    """Create a next_call thunk that returns the given result."""
    ret = result if result is not None else {"status": "success"}

    async def next_call() -> dict[str, Any]:
        return ret

    return next_call


# ------------------------------------------------------------------
# IdempotencyMiddleware
# ------------------------------------------------------------------


class TestIdempotencyMiddleware:
    """IdempotencyMiddleware rejects duplicate concurrent executions."""

    @pytest.mark.asyncio
    async def test_allows_first_execution(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        result = await mw(strategy, {"status": "active"}, next_call)

        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_rejects_duplicate_execution(self) -> None:
        """Simulates concurrent execution: first task is RUNNING when second arrives."""
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        payload = {"status": "active"}

        # Block the first execution so it stays RUNNING
        block_event = asyncio.Event()

        async def blocked_call() -> dict[str, Any]:
            await block_event.wait()
            return {"status": "success"}

        # Start first execution in background
        task1 = asyncio.create_task(mw(strategy, payload, blocked_call))
        # Give it time to acquire the lock and transition to RUNNING
        await asyncio.sleep(0.05)

        # Second execution with same payload should be rejected
        next_call_2 = await _make_next_call({"status": "success"})
        result_2 = await mw(strategy, payload, next_call_2)
        assert result_2["status"] == "error"
        assert result_2["reason"] == "DuplicateExecution"

        # Unblock and await first task
        block_event.set()
        result_1 = await task1
        assert result_1 == {"status": "success"}

    @pytest.mark.asyncio
    async def test_allows_different_payload(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")

        next_call_1 = await _make_next_call()
        result_1 = await mw(strategy, {"status": "active"}, next_call_1)
        assert result_1["status"] == "success"

        next_call_2 = await _make_next_call()
        result_2 = await mw(strategy, {"status": "inactive"}, next_call_2)
        assert result_2["status"] == "success"

    @pytest.mark.asyncio
    async def test_releases_lock_on_completion(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        payload = {"x": 1}

        # First execution completes
        next_call_1 = await _make_next_call()
        await mw(strategy, payload, next_call_1)

        # Lock should be released — same payload can run again
        next_call_2 = await _make_next_call()
        result = await mw(strategy, payload, next_call_2)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_releases_lock_on_failure(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        payload = {"x": 1}

        async def failing_call() -> dict[str, Any]:
            raise RuntimeError("Tool crashed")

        with pytest.raises(RuntimeError, match="Tool crashed"):
            await mw(strategy, payload, failing_call)

        # Lock should be released after failure
        next_call_2 = await _make_next_call()
        result = await mw(strategy, payload, next_call_2)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_cancelacion_libera_la_key_en_vez_de_dejarla_1h_bloqueada(self) -> None:
        """F4 (auditoría 2026-07-18, orchestrator_resilience): ``CancelledError``
        no hereda de ``Exception`` — el ``except Exception`` original lo dejaba
        pasar sin transicionar la task a FAILED, así que ``IdempotencyGuard``
        seguía viendo la key como activa hasta que expirara su TTL (1h por
        defecto). Cualquier cancelación (timeout del cliente, shutdown parcial)
        bloqueaba reintentos del mismo tool+payload durante una hora entera."""
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("generate_lods")
        payload = {"profile": "Default"}

        async def cancelled_call() -> dict[str, Any]:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await mw(strategy, payload, cancelled_call)

        # La key debe quedar liberada de inmediato — no recién tras el TTL.
        next_call = await _make_next_call({"status": "success"})
        result = await mw(strategy, payload, next_call)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_state_machine_tracks_task_lifecycle(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")

        next_call = await _make_next_call({"status": "success", "mods": [1, 2]})
        await mw(strategy, {}, next_call)

        # There should be a completed task in the state machine
        assert sm.active_task_count == 0

    @pytest.mark.asyncio
    async def test_releases_lock_and_marks_failed_on_error_status_dict(self) -> None:
        """P2: IdempotencyMiddleware transiciona a FAILED si el resultado contiene status='error'."""
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("generate_lods")
        payload = {"x": 1}

        error_dict = {
            "status": "error",
            "reason": "PipelineFailed",
            "details": "DynDOLOD abortado por timeout",
        }
        next_call_1 = await _make_next_call(error_dict)
        result = await mw(strategy, payload, next_call_1)

        assert result == error_dict
        tasks = list(sm._tasks.values())
        assert len(tasks) == 1
        assert tasks[0].state == "FAILED"
        assert tasks[0].error_message == "DynDOLOD abortado por timeout"
        assert tasks[0].result == error_dict

        # El lock debe liberarse inmediatamente tras la falla
        next_call_2 = await _make_next_call({"status": "success"})
        result_2 = await mw(strategy, payload, next_call_2)
        assert result_2["status"] == "success"

    @pytest.mark.asyncio
    async def test_releases_lock_and_marks_failed_on_success_false(self) -> None:
        """P2: IdempotencyMiddleware transiciona a FAILED si el resultado contiene success=False."""
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("generate_lods")
        payload = {"x": 2}

        error_dict = {"success": False, "message": "Preflight en rojo"}
        next_call = await _make_next_call(error_dict)
        result = await mw(strategy, payload, next_call)

        assert result == error_dict
        tasks = list(sm._tasks.values())
        assert len(tasks) == 1
        assert tasks[0].state == "FAILED"
        assert tasks[0].error_message == "Preflight en rojo"
        assert tasks[0].result == error_dict

    @pytest.mark.asyncio
    async def test_releases_lock_and_marks_failed_on_invalid_non_dict_result(self) -> None:
        """P2: IdempotencyMiddleware transiciona a FAILED si el resultado no es un diccionario."""
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("generate_lods")
        payload = {"x": 3}

        async def non_dict_call() -> Any:
            return ["no", "es", "dict"]

        result = await mw(strategy, payload, non_dict_call)
        assert result == ["no", "es", "dict"]
        tasks = list(sm._tasks.values())
        assert len(tasks) == 1
        assert tasks[0].state == "FAILED"
        assert tasks[0].error_message == "Invalid result type: list"


class _MaquinaConRival(ToolStateMachine):
    """Simula la ventana ``is_active`` → ``acquire_idempotency`` (#575 §1).

    Hoy no hay ningún ``await`` entre el pre-check y el acquire, así que esa ventana no
    se abre en el flujo asyncio actual. Esta subclase la fuerza de forma determinista:
    justo después de crear la task, un rival toma la key (lo que haría una reentrancia,
    un hilo o un ``await`` agregado a esa secuencia en el futuro).
    """

    def create_task(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        initial_state: str = "PENDING",
    ) -> Any:
        record = super().create_task(tool_name, payload, initial_state=initial_state)
        self.guard.acquire(record.idempotency_key, "rival")
        return record


class TestIdempotencyAcquireRechazado:
    """#575 §1: el ``bool`` de ``acquire_idempotency()`` no puede ignorarse."""

    @pytest.mark.asyncio
    async def test_acquire_rechazado_no_ejecuta_el_inner_chain(self) -> None:
        # Arrange
        sm = _MaquinaConRival()
        mw = IdempotencyMiddleware(state_machine=sm)
        inner = AsyncMock(return_value={"status": "success"})

        # Act
        result = await mw(_make_strategy("list_mods"), {"x": 1}, inner)

        # Assert: duplicado rechazado con el mismo contrato que el pre-check
        assert result["status"] == "error"
        assert result["reason"] == "DuplicateExecution"
        inner.assert_not_called()

    @pytest.mark.asyncio
    async def test_acquire_rechazado_no_deja_task_huerfana(self) -> None:
        # Arrange
        sm = _MaquinaConRival()
        mw = IdempotencyMiddleware(state_machine=sm)

        # Act
        await mw(_make_strategy("list_mods"), {"x": 1}, AsyncMock(return_value={"status": "success"}))

        # Assert: ninguna task queda PENDING/activa ni registrada
        assert sm.active_task_count == 0
        assert sm._tasks == {}

    @pytest.mark.asyncio
    async def test_acquire_rechazado_conserva_el_lock_del_rival(self) -> None:
        # Arrange
        sm = _MaquinaConRival()
        mw = IdempotencyMiddleware(state_machine=sm)
        key = sm.guard.make_key("list_mods", {"x": 1})

        # Act
        await mw(_make_strategy("list_mods"), {"x": 1}, AsyncMock(return_value={"status": "success"}))

        # Assert: el rechazado no libera la key que pertenece a otro dueño
        assert sm.guard.is_active(key)

    @pytest.mark.asyncio
    async def test_los_dos_caminos_de_rechazo_devuelven_exactamente_el_mismo_resultado(self) -> None:
        """Hermano en el mismo archivo: el rechazo del pre-check y el del ``acquire`` no pueden divergir.

        Los otros tests miran ``status`` y ``reason``; el texto de ``details`` es lo que ve el operador.
        """
        # Arrange
        payload = {"x": 1}
        inner = AsyncMock(return_value={"status": "success"})
        sm_precheck = ToolStateMachine()
        sm_precheck.guard.acquire(sm_precheck.guard.make_key("list_mods", payload), "rival")
        sm_acquire = _MaquinaConRival()

        # Act
        por_precheck = await IdempotencyMiddleware(state_machine=sm_precheck)(
            _make_strategy("list_mods"), payload, inner
        )
        por_acquire = await IdempotencyMiddleware(state_machine=sm_acquire)(_make_strategy("list_mods"), payload, inner)

        # Assert
        assert por_precheck["reason"] == "DuplicateExecution"
        assert por_acquire == por_precheck
        inner.assert_not_called()

    @pytest.mark.asyncio
    async def test_dos_ejecuciones_concurrentes_solo_una_llega_al_inner_chain(self) -> None:
        # Arrange
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        payload = {"x": 1}
        llamadas = 0
        liberar = asyncio.Event()

        async def inner() -> dict[str, Any]:
            nonlocal llamadas
            llamadas += 1
            await liberar.wait()
            return {"status": "success"}

        # Act
        primera = asyncio.create_task(mw(strategy, payload, inner))
        await asyncio.sleep(0)  # la primera toma la key y queda en RUNNING
        segunda = await mw(strategy, payload, inner)
        liberar.set()
        resultado_primera = await primera

        # Assert
        assert llamadas == 1
        assert resultado_primera == {"status": "success"}
        assert segunda["reason"] == "DuplicateExecution"
        assert sm.active_task_count == 0


# ------------------------------------------------------------------
# ProgressMiddleware
# ------------------------------------------------------------------


class TestProgressMiddleware:
    """ProgressMiddleware publishes lifecycle events to CoreEventBus."""

    @pytest.mark.asyncio
    async def test_noop_when_no_bus(self) -> None:
        """When event_bus is None, middleware is a transparent pass-through."""
        mw = ProgressMiddleware(event_bus=None)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        result = await mw(strategy, {}, next_call)
        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_emits_started_and_completed(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.*", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        result = await mw(strategy, {}, next_call)
        assert result == {"status": "success"}

        # Give the bus time to dispatch
        await asyncio.sleep(0.1)

        topics = [e.topic for e in collected_events]
        assert "ops.tool.started" in topics
        assert "ops.tool.completed" in topics

        await bus.stop()

    @pytest.mark.asyncio
    async def test_emits_failed_on_exception(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.*", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("failing_tool")

        async def failing_call() -> dict[str, Any]:
            raise RuntimeError("Boom")

        with pytest.raises(RuntimeError):
            await mw(strategy, {}, failing_call)

        await asyncio.sleep(0.1)

        topics = [e.topic for e in collected_events]
        assert "ops.tool.started" in topics
        assert "ops.tool.failed" in topics

        # Verify failed event has error details
        failed_event = next(e for e in collected_events if e.topic == "ops.tool.failed")
        assert failed_event.payload["tool"] == "failing_tool"
        assert "Boom" in failed_event.payload["error"]
        assert failed_event.payload["error_type"] == "RuntimeError"

        await bus.stop()

    @pytest.mark.asyncio
    async def test_completed_payload_includes_status(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.completed", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success", "count": 5})

        await mw(strategy, {}, next_call)
        await asyncio.sleep(0.1)

        assert len(collected_events) == 1
        assert collected_events[0].payload["status"] == "success"
        assert collected_events[0].payload["tool"] == "list_mods"

        await bus.stop()

    @pytest.mark.asyncio
    async def test_bus_failure_does_not_disrupt_execution(self) -> None:
        """If the bus fails to publish, the tool still executes."""
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("Bus down"))

        mw = ProgressMiddleware(event_bus=mock_bus)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        # Should NOT raise despite bus failure
        result = await mw(strategy, {}, next_call)
        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_started_payload_includes_tool_name(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.started", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("generate_lods")
        next_call = await _make_next_call()

        await mw(strategy, {"profile": "Default"}, next_call)
        await asyncio.sleep(0.1)

        assert len(collected_events) == 1
        assert collected_events[0].payload["tool"] == "generate_lods"
        assert "profile" in collected_events[0].payload["payload_keys"]

        await bus.stop()
