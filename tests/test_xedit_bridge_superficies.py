"""Auditoría nativa con el xEdit Bridge en las DOS superficies (ADR 0018).

Regla #1 del repo: una capacidad que se alcanza desde la GUI (``SupervisorAgent`` →
``tool_dispatcher``) y desde el agente LLM (``AsyncToolRegistry``) se cablea y se prueba en las
dos. La admisión copia archivos al laboratorio del bridge: exige aprobación humana en ambas
(gate HITL en la GUI, ``HITLGuard`` explícito en el agente). La auditoría es de lectura.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.schemas import AdmitirPluginNativoParams, AuditarPluginNativoParams
from sky_claw.app.agent.tools.xedit_bridge_tools import admitir_plugin_nativo, auditar_plugin_nativo
from sky_claw.app.orchestrator.tool_strategies.admitir_plugin_nativo import AdmitirPluginNativoStrategy
from sky_claw.app.orchestrator.tool_strategies.auditar_plugin_nativo import AuditarPluginNativoStrategy
from sky_claw.app.orchestrator.tool_strategies.base import ApprovalPayloadDescriber, ApprovalPayloadValidator
from sky_claw.app.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS, HitlGateMiddleware
from sky_claw.app.security.hitl import Decision
from tests._orchestration_dispatcher_dependencies import crear_dependencias_desde_doble

HERRAMIENTAS_DEL_BRIDGE = frozenset({"auditar_plugin_nativo", "admitir_plugin_nativo"})


def _servicio() -> MagicMock:
    servicio = MagicMock()
    servicio.auditar = AsyncMock(return_value={"success": True, "message": "", "aprobado": True})
    servicio.admitir = AsyncMock(return_value={"success": True, "message": "", "clausura": ["Skyrim.esm", "Mod.esp"]})
    servicio.perfil_activo = MagicMock(return_value="prueva huevo 3")
    return servicio


def _hitl(decision: Decision) -> MagicMock:
    hitl = MagicMock()
    hitl.request_approval = AsyncMock(return_value=decision)
    return hitl


# --- superficie GUI: estrategias del dispatcher --------------------------------------------


def test_admitir_es_destructiva_y_auditar_no() -> None:
    assert AdmitirPluginNativoStrategy.name in DESTRUCTIVE_TOOL_PATTERNS
    assert AuditarPluginNativoStrategy.name not in DESTRUCTIVE_TOOL_PATTERNS


def test_admitir_implementa_las_capacidades_hitl() -> None:
    estrategia = AdmitirPluginNativoStrategy(service=_servicio())
    assert isinstance(estrategia, ApprovalPayloadValidator)
    assert isinstance(estrategia, ApprovalPayloadDescriber)


async def test_gate_deniega_admitir_sin_guard_y_no_copia_nada() -> None:
    servicio = _servicio()
    resultado = await HitlGateMiddleware()(
        AdmitirPluginNativoStrategy(service=servicio), {"plugin": "Mod.esp"}, AsyncMock()
    )
    assert resultado["status"] == "error" and resultado["reason"] == "HITLGateUnavailable"
    servicio.admitir.assert_not_awaited()


@pytest.mark.parametrize("payload", [{}, {"plugin": "..\\Skyrim.esm"}, {"plugin": "Mod.esp", "perfil": "Otro"}])
def test_admitir_validate_for_approval_rechaza_payload_que_no_se_honra(payload: dict) -> None:
    with pytest.raises((ValueError, ValidationError)):
        AdmitirPluginNativoStrategy(service=_servicio()).validate_for_approval(payload)


def test_admitir_describe_for_approval_es_legible_para_el_operador() -> None:
    texto = AdmitirPluginNativoStrategy(service=_servicio()).describe_for_approval({"plugin": "Mod.esp"})
    assert "Mod.esp" in texto and "MO2" in texto and "xEdit Bridge" in texto


async def test_estrategias_delegan_al_servicio() -> None:
    servicio = _servicio()
    await AuditarPluginNativoStrategy(service=servicio).execute({"plugin": "Mod.esp", "timeout": 90})
    await AdmitirPluginNativoStrategy(service=servicio).execute({"plugin": "Mod.esp"})
    servicio.auditar.assert_awaited_once_with("Mod.esp", timeout=90)
    servicio.admitir.assert_awaited_once_with("Mod.esp")


@pytest.mark.parametrize(
    "payload", [{"plugin": "x.txt"}, {"plugin": "Mod.esp", "timeout": 121}, {"plugin": "Mod.esp", "extra": 1}]
)
async def test_auditar_payload_invalido_es_rechazado(payload: dict) -> None:
    with pytest.raises(ValidationError):
        await AuditarPluginNativoStrategy(service=_servicio()).execute(payload)


def _dispatcher(gate: HitlGateMiddleware):
    from sky_claw.app.orchestrator.supervisor import SupervisorAgent
    from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher

    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup.scraper = MagicMock()
    sup.tools = MagicMock()
    sup.interface = MagicMock()
    for atributo in (
        "_loot_service",
        "_synthesis_service",
        "_xedit_service",
        "_dyndolod_service",
        "_pandora_service",
        "_grass_cache_service",
    ):
        setattr(sup, atributo, MagicMock())
    sup._xedit_bridge_service = _servicio()
    sup.profile_name = "TestProfile"
    return build_orchestration_dispatcher(crear_dependencias_desde_doble(sup), hitl_gate=gate)


def test_dispatcher_registra_admitir_gateada_y_auditar_libre() -> None:
    gate = HitlGateMiddleware(allow_unattended=True)
    dispatcher = _dispatcher(gate)
    gateadas = {nombre for nombre, cadena in dispatcher._middleware.items() if any(mw is gate for mw in cadena)}
    assert "admitir_plugin_nativo" in gateadas
    assert "auditar_plugin_nativo" in dispatcher._middleware and "auditar_plugin_nativo" not in gateadas


# --- superficie LLM: AsyncToolRegistry ------------------------------------------------------


@pytest.fixture()
def registro(tmp_path: pathlib.Path) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(registry=MagicMock(), mo2=mo2, sync_engine=MagicMock())


def test_registro_publica_ambas_tools_con_sus_parametros(registro: AsyncToolRegistry) -> None:
    assert registro._tools["auditar_plugin_nativo"].params_model is AuditarPluginNativoParams
    assert registro._tools["admitir_plugin_nativo"].params_model is AdmitirPluginNativoParams


@pytest.mark.parametrize(
    "params",
    [
        {"plugin": "..\\x.esp"},
        {"plugin": "x.txt"},
        {"plugin": "Mod.esp", "timeout": 4},
        {"plugin": "Mod.esp", "timeout": "60"},
    ],
)
def test_parametros_de_auditar_validan_plugin_y_timeout(params: dict) -> None:
    with pytest.raises(ValidationError):
        AuditarPluginNativoParams(**params)


async def test_auditar_handler_no_pide_aprobacion() -> None:
    servicio = _servicio()
    salida = json.loads(await auditar_plugin_nativo(servicio, "Mod.esp", 60))
    assert salida["aprobado"] is True
    servicio.auditar.assert_awaited_once_with("Mod.esp", timeout=60)


async def test_auditar_handler_sin_servicio_explica_la_configuracion() -> None:
    salida = json.loads(await auditar_plugin_nativo(None, "Mod.esp"))
    assert salida["success"] is False and "XEDIT_BRIDGE_ROOT" in salida["message"]


async def test_admitir_handler_sin_hitl_rechaza_y_no_copia() -> None:
    servicio = _servicio()
    salida = json.loads(await admitir_plugin_nativo(servicio, None, "Mod.esp"))
    assert salida["success"] is False and "aprobación" in salida["message"]
    servicio.admitir.assert_not_awaited()


async def test_admitir_handler_aprobado_copia_con_pedido_explicito() -> None:
    servicio, hitl = _servicio(), _hitl(Decision.APPROVED)
    salida = json.loads(await admitir_plugin_nativo(servicio, hitl, "Mod.esp"))
    assert salida["success"] is True
    servicio.admitir.assert_awaited_once_with("Mod.esp")
    pedido = hitl.request_approval.await_args.kwargs
    # Misma categoría que el gate de la GUI (middleware.py): las dos superficies piden lo mismo y
    # solo «Modo local», elegido por el operador, la auto-aprueba.
    assert pedido["category"] == "tool_execution"
    assert "Mod.esp" in pedido["reason"] and "prueva huevo 3" in pedido["reason"]


@pytest.mark.parametrize("decision", [Decision.DENIED, Decision.TIMEOUT])
async def test_admitir_handler_denegado_o_vencido_no_copia(decision: Decision) -> None:
    servicio = _servicio()
    salida = json.loads(await admitir_plugin_nativo(servicio, _hitl(decision), "Mod.esp"))
    assert salida["success"] is False
    servicio.admitir.assert_not_awaited()


# --- cableado del registro: execute() llega al servicio con el resolver y el guard reales ----


async def test_registro_sin_resolver_explica_la_configuracion(registro: AsyncToolRegistry) -> None:
    salida = json.loads(await registro.execute("auditar_plugin_nativo", {"plugin": "Mod.esp"}))
    assert salida["success"] is False and "XEDIT_BRIDGE_ROOT" in salida["message"]


async def test_registro_cablea_resolver_y_hitl_de_la_sesion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sky_claw.local.tools.xedit_bridge_service as modulo

    servicio, hitl, resolver = _servicio(), _hitl(Decision.APPROVED), MagicMock()
    construidos: list[object] = []

    def fabrica(path_resolver: object) -> MagicMock:
        construidos.append(path_resolver)
        return servicio

    monkeypatch.setattr(modulo, "XEditBridgeService", fabrica)
    mo2 = MagicMock()
    mo2.root = tmp_path
    registro = AsyncToolRegistry(
        registry=MagicMock(), mo2=mo2, sync_engine=MagicMock(), hitl=hitl, path_resolver=resolver
    )

    salida = json.loads(await registro.execute("admitir_plugin_nativo", {"plugin": "Mod.esp"}))
    assert salida["success"] is True
    assert construidos == [resolver], "el servicio tiene que usar el MISMO resolver que la GUI"
    hitl.request_approval.assert_awaited_once()
    servicio.admitir.assert_awaited_once_with("Mod.esp")


async def test_registro_no_deja_que_el_llm_elija_mo2_ni_perfil(registro: AsyncToolRegistry) -> None:
    for extra in ({"mo2": "C:\\otro"}, {"profile": "Otro"}):
        with pytest.raises(ValidationError):
            await registro.execute("admitir_plugin_nativo", {"plugin": "Mod.esp", **extra})


# --- ancla: cada tool del bridge existe en las dos superficies -----------------------------


def test_cada_tool_del_bridge_existe_en_las_dos_superficies(registro: AsyncToolRegistry) -> None:
    dispatcher = _dispatcher(HitlGateMiddleware(allow_unattended=True))
    assert set(registro._tools) >= HERRAMIENTAS_DEL_BRIDGE, "falta en el agente LLM"
    assert set(dispatcher._middleware) >= HERRAMIENTAS_DEL_BRIDGE, "falta en la GUI (tool_dispatcher)"
    publicadas = {n for n in set(registro._tools) | set(dispatcher._middleware) if "plugin_nativo" in n}
    assert publicadas == HERRAMIENTAS_DEL_BRIDGE, (
        "una tool nueva del bridge tiene que sumarse acá y en las dos superficies"
    )
