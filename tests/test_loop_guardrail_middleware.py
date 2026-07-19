"""F1a (auditoría 2026-07-18) — cortacircuitos cognitivo en el camino REAL.

El :class:`AgenticLoopGuardrail` vivía solo en los callbacks del StateGraph,
que nada en producción ejecuta (hallazgo F1 del informe #319): la GUI y el
agente LLM despachan por :class:`OrchestrationToolDispatcher`, así que un
agente en bucle podía repetir la misma tool sin freno. El guardrail corre
ahora como middleware GLOBAL del dispatcher (outermost), protegiendo todas
las tools registradas — también las read-only: un loop de queries sigue
siendo un loop.
"""

from __future__ import annotations

from typing import Any

from sky_claw.antigravity.orchestrator.tool_dispatcher import OrchestrationToolDispatcher
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import (
    LoopGuardrailMiddleware,
)


class _StrategyContadora:
    """Strategy fake que cuenta sus ejecuciones reales."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ejecuciones = 0

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        self.ejecuciones += 1
        return {"success": True, "message": ""}


def _dispatcher_con_guardrail(*strategies: _StrategyContadora) -> OrchestrationToolDispatcher:
    dispatcher = OrchestrationToolDispatcher(global_middleware=[LoopGuardrailMiddleware()])
    for strategy in strategies:
        dispatcher.register(strategy)
    return dispatcher


class TestLoopGuardrailMiddleware:
    async def test_tercera_invocacion_identica_tripea_sin_ejecutar(self) -> None:
        """A,A,A con el mismo payload: la 3ª devuelve el error dict del
        cortacircuitos SIN invocar la strategy."""
        strategy = _StrategyContadora("sort_load_order")
        dispatcher = _dispatcher_con_guardrail(strategy)
        payload = {"profile": "Default"}

        assert (await dispatcher.dispatch("sort_load_order", payload))["success"] is True
        assert (await dispatcher.dispatch("sort_load_order", payload))["success"] is True
        resultado = await dispatcher.dispatch("sort_load_order", payload)

        assert resultado["status"] == "error"
        assert resultado["reason"] == "CircuitBreakerTripped"
        assert "bucle" in resultado["details"]
        assert strategy.ejecuciones == 2

    async def test_ciclo_oscilante_a_b_a_b_tripea(self) -> None:
        """El síntoma clásico del agente atascado: alternar dos tools sin
        progresar. La 4ª invocación (segundo B) corta el ciclo."""
        a = _StrategyContadora("tool_a")
        b = _StrategyContadora("tool_b")
        dispatcher = _dispatcher_con_guardrail(a, b)

        await dispatcher.dispatch("tool_a", {})
        await dispatcher.dispatch("tool_b", {})
        await dispatcher.dispatch("tool_a", {})
        resultado = await dispatcher.dispatch("tool_b", {})

        assert resultado["reason"] == "CircuitBreakerTripped"
        assert a.ejecuciones == 2
        assert b.ejecuciones == 1

    async def test_payloads_distintos_no_tripean(self) -> None:
        """Misma tool con argumentos distintos es progreso legítimo."""
        strategy = _StrategyContadora("query_mod_metadata")
        dispatcher = _dispatcher_con_guardrail(strategy)

        for nexus_id in (1, 2, 3, 4, 5):
            resultado = await dispatcher.dispatch("query_mod_metadata", {"nexus_id": nexus_id})
            assert resultado.get("success") is True

        assert strategy.ejecuciones == 5

    async def test_tras_el_trip_sigue_bloqueado_hasta_reset_explicito(self) -> None:
        """Un caller atascado no puede ejecutar de nuevo sin intervención humana."""
        strategy = _StrategyContadora("sort_load_order")
        dispatcher = _dispatcher_con_guardrail(strategy)
        payload = {"profile": "Default"}

        await dispatcher.dispatch("sort_load_order", payload)
        await dispatcher.dispatch("sort_load_order", payload)
        assert (await dispatcher.dispatch("sort_load_order", payload))["reason"] == "CircuitBreakerTripped"

        resultado = await dispatcher.dispatch("sort_load_order", payload)
        assert resultado["reason"] == "CircuitBreakerTripped"
        assert strategy.ejecuciones == 2

    async def test_cubre_tools_registradas_sin_middleware_propio(self) -> None:
        """El middleware es GLOBAL: aplica aunque register() no reciba lista
        de middleware — ninguna tool queda fuera del cortacircuitos."""
        strategy = _StrategyContadora("scan_asset_conflicts")
        dispatcher = OrchestrationToolDispatcher(global_middleware=[LoopGuardrailMiddleware()])
        dispatcher.register(strategy)  # sin middleware propio

        await dispatcher.dispatch("scan_asset_conflicts", {})
        await dispatcher.dispatch("scan_asset_conflicts", {})
        resultado = await dispatcher.dispatch("scan_asset_conflicts", {})

        assert resultado["reason"] == "CircuitBreakerTripped"
        assert strategy.ejecuciones == 2

    async def test_reset_manual_rearma_el_circuito_abierto(self) -> None:
        """reset() permite continuar únicamente después de que el circuito abrió."""
        middleware = LoopGuardrailMiddleware()
        strategy = _StrategyContadora("sort_load_order")
        dispatcher = OrchestrationToolDispatcher(global_middleware=[middleware])
        dispatcher.register(strategy)

        await dispatcher.dispatch("sort_load_order", {})
        await dispatcher.dispatch("sort_load_order", {})
        assert (await dispatcher.dispatch("sort_load_order", {}))["reason"] == "CircuitBreakerTripped"
        middleware.reset()
        resultado = await dispatcher.dispatch("sort_load_order", {})

        assert resultado.get("success") is True
        assert strategy.ejecuciones == 3

    async def test_tool_no_registrada_conserva_el_contrato_legacy(self) -> None:
        """El miss de registro responde ToolNotFound ANTES del guardrail (una
        tool alucinada no debe consumir ventana del cortacircuitos)."""
        dispatcher = _dispatcher_con_guardrail()

        for _ in range(4):
            resultado = await dispatcher.dispatch("tool_alucinada", {})
            assert resultado == {"status": "error", "reason": "ToolNotFound"}
