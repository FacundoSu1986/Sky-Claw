"""Steps de acceptance del orden LOOT (Stage 5) → Grass Cache (Stage 8).

El contrato se afirma como lo consume producción: vía ``normalize_tool_result``
(igual que ``sky_claw/app/gui/controllers/ritual_runner.py``). No es cosmético —
el dispatcher NO emite una sola forma de fallo: el guard del servicio devuelve
``{success, message}``, mientras el gate HITL y los middleware de error
devuelven la shape legacy ``{status, reason, details}`` SIN ``success``. Leer el
dict crudo hacía que cualquier escenario de fallo del middleware reventara con
``KeyError: 'success'`` en vez de fallar con una aserción legible.

Los campos estructurados propios del servicio (``outcome``, ``cgid_count``,
``teardown_failures``) no viven en la vista normalizada: se afirman sobre
``ctx["result_crudo"]``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_bdd import given, parsers, scenarios, then, when
from tests.bdd.support.async_harness import run
from tests.bdd.support.grass_cache_harness import sembrar_loot_completado

from sky_claw.app.db.journal import TransactionStatus
from sky_claw.local.tools.tool_result import normalize_tool_result

FEATURE = "../features/sop_modding/generacion_grass_cache.feature"

# ``scenarios()`` liga TODOS los escenarios del feature automáticamente — a
# diferencia de un `@scenario(FEATURE, "...")` por escenario, un escenario
# nuevo agregado al .feature sin este cambio ya queda ligado (o falla al
# recolectar si le faltan steps) en vez de quedar silenciosamente sin test
# (review #420). Es el patrón que ``test_mapa_sop_features.py`` exige para
# cerrar una entrada del mapa SOP↔feature.
scenarios(FEATURE)


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("que LOOT tiene un FlightReport commiteado en el journal")
def loot_con_flight_report_commiteado(bdd_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
    run(bdd_loop, sembrar_loot_completado(ctx["journal"]))


@given("que LOOT no tiene un FlightReport commiteado en el journal")
def loot_sin_flight_report(ctx: dict[str, Any]) -> None:
    assert ctx["journal"] is not None


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


def _despachar(ctx: dict[str, Any], bdd_loop: asyncio.AbstractEventLoop, payload: dict[str, Any]) -> None:
    """Despacha y guarda AMBAS vistas: la cruda y la normalizada."""

    async def _impl() -> None:
        ctx["result_crudo"] = await ctx["dispatcher"].dispatch("generate_grass_cache", payload)

    run(bdd_loop, _impl())
    ctx["result"] = normalize_tool_result(ctx["result_crudo"])


@when(parsers.parse('el dispatcher despacha "{tool_name}" para el worldspace "{worldspace}"'))
def dispatcher_despacha_tool(
    bdd_loop: asyncio.AbstractEventLoop,
    ctx: dict[str, Any],
    tool_name: str,
    worldspace: str,
) -> None:
    assert tool_name == "generate_grass_cache"
    _despachar(ctx, bdd_loop, {"worldspaces": [worldspace], "conflicting_mods": ["ENB Helper"]})


@when(parsers.parse('el dispatcher despacha "{tool_name}" para "{worldspace}" con force_stage_guard'))
def dispatcher_despacha_con_force(
    bdd_loop: asyncio.AbstractEventLoop,
    ctx: dict[str, Any],
    tool_name: str,
    worldspace: str,
) -> None:
    assert tool_name == "generate_grass_cache"
    _despachar(
        ctx,
        bdd_loop,
        {"worldspaces": [worldspace], "conflicting_mods": ["ENB Helper"], "force_stage_guard": True},
    )


# ---------------------------------------------------------------------------
# Then — contrato normalizado
# ---------------------------------------------------------------------------


@then("el contrato normalizado responde con éxito y mensaje vacío")
def contrato_exitoso(ctx: dict[str, Any]) -> None:
    resultado = ctx["result"]
    assert resultado["success"] is True, resultado["message"]
    assert resultado["message"] == ""


@then("el contrato normalizado responde con fallo")
def contrato_fallido(ctx: dict[str, Any]) -> None:
    assert ctx["result"]["success"] is False


@then(parsers.parse('el mensaje normalizado indica ejecutar "{comando}"'))
def mensaje_accionable(ctx: dict[str, Any], comando: str) -> None:
    assert comando in ctx["result"]["message"]


@then(parsers.parse('el fallo se atribuye a "{reason}"'))
def fallo_atribuido(ctx: dict[str, Any], reason: str) -> None:
    """El ``reason`` legacy identifica QUÉ capa cortó (gate HITL vs servicio)."""
    assert ctx["result_crudo"]["reason"] == reason


# ---------------------------------------------------------------------------
# Then — efectos observables del camino feliz
# ---------------------------------------------------------------------------


@then(parsers.parse('se solicita aprobación HITL para "{tool_name}"'))
def aprobacion_hitl_solicitada(ctx: dict[str, Any], tool_name: str) -> None:
    guard = ctx["colaboradores"]["hitl_guard"]
    guard.request_approval.assert_awaited_once()
    solicitud = guard.request_approval.await_args.kwargs
    assert solicitud["category"] == "tool_execution"
    assert tool_name in solicitud["reason"]


@then(parsers.parse('el contrato reporta outcome "{outcome}" y un cgid_count mayor que cero'))
def contrato_reporta_recuento(ctx: dict[str, Any], outcome: str) -> None:
    crudo = ctx["result_crudo"]
    assert crudo["outcome"] == outcome
    assert crudo["cgid_count"] > 0


@then(parsers.parse('se solicitan los locks "{primero}" y "{segundo}" en ese orden'))
def locks_solicitados_en_orden(ctx: dict[str, Any], primero: str, segundo: str) -> None:
    colaboradores = ctx["colaboradores"]
    assert colaboradores["entradas_lock"] == [primero, segundo]
    for resource_id in (primero, segundo):
        colaboradores["locks_creados"][resource_id].__aenter__.assert_awaited_once_with()


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
    assert "teardown_failures" not in ctx["result_crudo"]


# ---------------------------------------------------------------------------
# Then — ausencia de mutación (compartido por los tres caminos de rechazo)
# ---------------------------------------------------------------------------


@then("el ritual no alcanza ninguna mutación")
def ritual_sin_mutacion(bdd_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
    """Un solo step compuesto en vez de cuatro aserciones negativas sueltas.

    Cuando el servicio gane un colaborador mutante nuevo, se refuerza ACÁ y los
    tres escenarios de rechazo lo heredan — en vez de tres copias que se
    arreglan de a una (que es la clase de defecto dominante del repo).
    """
    colaboradores = ctx["colaboradores"]
    assert colaboradores["llamadas_lock"] == [], "no debe pedirse ningún lock mutante"
    colaboradores["runner_factory"].assert_not_called()
    assert colaboradores["profile_manager"].mock_calls == [], "no debe tocarse el perfil temporal"

    async def _consultar() -> list[Any]:
        return await ctx["journal"].list_recent_transactions(limit=10)

    transacciones = run(bdd_loop, _consultar())
    grass = [tx for tx in transacciones if "Grass precache" in tx.description]
    assert all(tx.status is not TransactionStatus.COMMITTED for tx in grass)
