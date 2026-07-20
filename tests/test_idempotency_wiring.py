"""F4 (auditoría 2026-07-18, orchestrator_resilience) — cableado real de
``IdempotencyMiddleware``.

La máquina de FASE 1.5.4 (``IdempotencyMiddleware``, ``ToolStateMachine``)
existía y estaba testeada de forma aislada (``test_idempotency_progress_middleware.py``),
pero ``build_orchestration_dispatcher`` — el ÚNICO camino real de dispatch,
igual que documenta F1a sobre ``LoopGuardrailMiddleware`` — nunca la
registraba. Dos invocaciones concurrentes de la misma tool+payload corrían
en paralelo sin ninguna protección anti-duplicados. Este módulo ancla que
el dispatcher construido por la fábrica real la aplica, siguiendo el mismo
patrón GLOBAL que ``LoopGuardrailMiddleware`` (F1a).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import HitlGateMiddleware


def _supervisor_minimo() -> SupervisorAgent:
    """SupervisorAgent construction-free (mismo patrón que test_supervisor_dispatch_tool.py)."""
    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup.scraper = MagicMock()
    sup.tools = MagicMock()
    sup._loot_service = MagicMock()
    sup.interface = MagicMock()
    sup._synthesis_service = MagicMock()
    sup._xedit_service = MagicMock()
    sup._dyndolod_service = MagicMock()
    sup._pandora_service = MagicMock()
    sup._grass_cache_service = MagicMock()
    sup.profile_name = "TestProfile"
    sup.journal = AsyncMock()
    return sup


class TestIdempotencyWiringEnElDispatcherReal:
    async def test_build_orchestration_dispatcher_registra_idempotencia_global(self) -> None:
        """Dos dispatch() concurrentes de query_mod_metadata con el MISMO
        payload: el segundo debe ser rechazado por DuplicateExecution en vez
        de correr en paralelo — sin wiring, ambos corrían libremente."""
        sup = _supervisor_minimo()
        dispatcher = build_orchestration_dispatcher(
            sup,
            hitl_gate=HitlGateMiddleware(allow_unattended=True),
        )

        bloqueo = asyncio.Event()

        async def query_lenta(*_args: Any, **_kwargs: Any) -> MagicMock:
            await bloqueo.wait()
            resultado = MagicMock()
            resultado.model_dump.return_value = {"mod_id": 1}
            return resultado

        sup.scraper.query_nexus = AsyncMock(side_effect=query_lenta)

        payload = {"query": "cool mod"}
        tarea_1 = asyncio.create_task(dispatcher.dispatch("query_mod_metadata", payload))
        await asyncio.sleep(0.05)  # deja que la 1ª adquiera el lock y quede RUNNING

        # Sin wiring, la 2ª invocación NO se rechaza: también entra a
        # query_lenta y queda esperando `bloqueo` — timeout corto en vez de
        # un hang eterno para que el rojo falle limpio, no cuelgue la suite.
        try:
            resultado_2 = await asyncio.wait_for(dispatcher.dispatch("query_mod_metadata", payload), timeout=1.0)
        except TimeoutError:
            bloqueo.set()
            await tarea_1
            raise AssertionError(
                "la 2ª invocación no fue rechazada — corrió en paralelo sin protección de idempotencia"
            ) from None

        assert resultado_2["status"] == "error"
        assert resultado_2["reason"] == "DuplicateExecution"

        bloqueo.set()
        resultado_1 = await tarea_1
        assert resultado_1 == {"mod_id": 1}

    async def test_idempotency_inyectado_es_el_que_corre_en_el_dispatch(self) -> None:
        """``idempotency=`` sigue el mismo patrón de inyección que
        ``loop_guardrail=`` (F1a): el SupervisorAgent real retiene su propia
        instancia para poder introspeccionarla/rearmarla. Si la fábrica
        ignorara el parámetro y armara una interna, el ``ToolStateMachine``
        inyectado nunca vería la task."""
        from sky_claw.antigravity.orchestrator.tool_state_machine import ToolStateMachine
        from sky_claw.antigravity.orchestrator.tool_strategies.middleware import IdempotencyMiddleware

        sm = ToolStateMachine()
        sup = _supervisor_minimo()
        dispatcher = build_orchestration_dispatcher(
            sup,
            hitl_gate=HitlGateMiddleware(allow_unattended=True),
            idempotency=IdempotencyMiddleware(state_machine=sm),
        )
        sup.scraper.query_nexus = AsyncMock(return_value=MagicMock(model_dump=lambda: {"mod_id": 1}))

        await dispatcher.dispatch("query_mod_metadata", {"query": "cool mod"})

        assert sm.active_task_count == 0  # la task pasó por RUNNING → COMPLETED en ESTE state machine
