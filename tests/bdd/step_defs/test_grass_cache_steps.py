"""Steps de acceptance del orden LOOT (Stage 5) → Grass Cache (Stage 8)."""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_bdd import given, parsers, scenario, then, when
from tests.bdd.conftest import run
from tests.bdd.support.grass_cache_harness import sembrar_loot_completado

from sky_claw.app.db.journal import TransactionStatus


@scenario(
    "../features/sop_modding/generacion_grass_cache.feature",
    "El agente ejecuta Grass Cache después de un LOOT commiteado",
)
def test_grass_cache_con_stage5_completado() -> None:
    """El dispatcher aprobado ejecuta Stage 8 después de un LOOT confirmado."""


@scenario(
    "../features/sop_modding/generacion_grass_cache.feature",
    "El guard rechaza Grass Cache cuando LOOT no está confirmado",
)
def test_grass_cache_guard_fail_closed() -> None:
    """Sin FlightReport de LOOT, el dispatcher no alcanza ninguna mutación."""


@given("que LOOT tiene un FlightReport commiteado en el journal")
def loot_con_flight_report_commiteado(bdd_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
    run(bdd_loop, sembrar_loot_completado(ctx["journal"]))


@given("que LOOT no tiene un FlightReport commiteado en el journal")
def loot_sin_flight_report(ctx: dict[str, Any]) -> None:
    assert ctx["journal"] is not None


@when(parsers.parse('el agente despacha "{tool_name}" para el worldspace "{worldspace}"'))
def agente_despacha_tool(
    bdd_loop: asyncio.AbstractEventLoop,
    ctx: dict[str, Any],
    tool_name: str,
    worldspace: str,
) -> None:
    payload = {"worldspaces": [worldspace], "conflicting_mods": ["ENB Helper"]}

    async def _impl() -> None:
        ctx["result"] = await ctx["dispatcher"].dispatch(tool_name, payload)

    run(bdd_loop, _impl())


@then("el contrato responde con éxito y mensaje vacío")
def contrato_exitoso(ctx: dict[str, Any]) -> None:
    resultado = ctx["result"]
    assert resultado["success"] is True, resultado["message"]
    assert resultado["message"] == ""


@then(parsers.parse('se solicita aprobación HITL para "{tool_name}"'))
def aprobacion_hitl_solicitada(ctx: dict[str, Any], tool_name: str) -> None:
    guard = ctx["colaboradores"]["hitl_guard"]
    guard.request_approval.assert_awaited_once()
    solicitud = guard.request_approval.await_args.kwargs
    assert solicitud["category"] == "tool_execution"
    assert tool_name in solicitud["reason"]


@then(parsers.parse('el contrato reporta outcome "{outcome}" y un cgid_count mayor que cero'))
def contrato_reporta_recuento(ctx: dict[str, Any], outcome: str) -> None:
    resultado = ctx["result"]
    assert resultado["outcome"] == outcome
    assert resultado["cgid_count"] > 0


@then(parsers.parse('se solicitan los locks "{primero}" y "{segundo}" en ese orden'))
def locks_solicitados_en_orden(ctx: dict[str, Any], primero: str, segundo: str) -> None:
    pedidos = [llamada["resource_id"] for llamada in ctx["colaboradores"]["llamadas_lock"]]
    assert pedidos == [primero, segundo]


@then(parsers.parse('el journal registra "{descripcion}" como commiteado'))
def journal_tx_commiteada(bdd_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any], descripcion: str) -> None:
    async def _consultar() -> list[Any]:
        return await ctx["journal"].list_recent_transactions(limit=10)

    transacciones = run(bdd_loop, _consultar())
    rituales = [tx for tx in transacciones if descripcion in tx.description]
    assert len(rituales) == 1
    assert rituales[0].status is TransactionStatus.COMMITTED


@then(parsers.parse('se publica el evento "{topico}"'))
def publica_evento(ctx: dict[str, Any], topico: str) -> None:
    bus = ctx["colaboradores"]["event_bus"]
    topicos = [llamada.args[0].topic for llamada in bus.publish.await_args_list]
    assert topico in topicos


@then("el perfil temporal se desmonta sin fallos de teardown")
def perfil_temporal_desmontado(ctx: dict[str, Any]) -> None:
    ctx["colaboradores"]["profile_manager"].teardown.assert_awaited_once_with()
    assert "teardown_failures" not in ctx["result"]


@then("el contrato responde con fallo")
def contrato_fallido(ctx: dict[str, Any]) -> None:
    assert ctx["result"]["success"] is False


@then(parsers.parse('el mensaje indica ejecutar "{comando}"'))
def mensaje_accionable(ctx: dict[str, Any], comando: str) -> None:
    assert comando in ctx["result"]["message"]


@then("no se solicita ningún lock mutante")
def sin_locks_mutantes(ctx: dict[str, Any]) -> None:
    assert ctx["colaboradores"]["llamadas_lock"] == []


@then("no se crea ningún runner del juego")
def sin_runner_de_juego(ctx: dict[str, Any]) -> None:
    ctx["colaboradores"]["runner_factory"].assert_not_called()


@then("el journal no contiene una transacción de grass commiteada")
def journal_sin_tx_grass_commiteada(bdd_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
    async def _consultar() -> list[Any]:
        return await ctx["journal"].list_recent_transactions(limit=10)

    transacciones = run(bdd_loop, _consultar())
    grass = [tx for tx in transacciones if "Grass precache" in tx.description]
    assert all(tx.status is not TransactionStatus.COMMITTED for tx in grass)
