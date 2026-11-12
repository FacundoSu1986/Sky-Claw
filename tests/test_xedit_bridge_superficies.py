"""Auditoría nativa con el xEdit Bridge en las DOS superficies (ADR 0018).

Regla #1 del repo: una capacidad que se alcanza desde la GUI (``SupervisorAgent`` →
``tool_dispatcher``) y desde el agente LLM (``AsyncToolRegistry``) se cablea y se prueba en las
dos. La admisión copia archivos al laboratorio del bridge: exige aprobación humana en ambas
(gate HITL en la GUI, ``HITLGuard`` explícito en el agente). La auditoría es de lectura.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from sky_claw.app.agent.router import DEFAULT_TOOL_ROUND_TIMEOUT, MAX_TOOL_RESULT_CHARS
from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.schemas import TIMEOUT_MAXIMO_LLM, AdmitirPluginNativoParams, AuditarPluginNativoParams
from sky_claw.app.agent.tools.xedit_bridge_tools import (
    MARGEN_LLM_S,
    PRESUPUESTO_RESULTADO_LLM,
    GestorDeAdmisionesNativas,
    admitir_plugin_nativo,
    auditar_plugin_nativo,
)
from sky_claw.app.orchestrator.tool_strategies.admitir_plugin_nativo import AdmitirPluginNativoStrategy
from sky_claw.app.orchestrator.tool_strategies.auditar_plugin_nativo import AuditarPluginNativoStrategy
from sky_claw.app.orchestrator.tool_strategies.base import ApprovalPayloadDescriber, ApprovalPayloadValidator
from sky_claw.app.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS, HitlGateMiddleware
from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools.xedit_bridge_service import TIMEOUT_POR_DEFECTO
from tests._orchestration_dispatcher_dependencies import crear_dependencias_desde_doble

HERRAMIENTAS_DEL_BRIDGE = frozenset({"auditar_plugin_nativo", "admitir_plugin_nativo"})
RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _servicio() -> MagicMock:
    servicio = MagicMock()
    servicio.auditar = AsyncMock(return_value={"success": True, "message": "", "aprobado": True})
    servicio.admitir = AsyncMock(
        return_value={"success": True, "message": "", "admission_id": "adm-1", "clausura": ["Skyrim.esm", "Mod.esp"]}
    )
    servicio.perfil_activo = MagicMock(return_value="prueva huevo 3")
    return servicio


def _hitl(decision: Decision) -> MagicMock:
    hitl = MagicMock()
    hitl.request_approval = AsyncMock(return_value=decision)
    return hitl


def _hitl_pendiente() -> tuple[MagicMock, asyncio.Future[Decision]]:
    """Un guard cuyo operador todavía no respondió: la decisión llega cuando el test la resuelve."""
    decision: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()

    async def _esperar(**_k: object) -> Decision:
        return await decision

    hitl = MagicMock()
    hitl.request_approval = AsyncMock(side_effect=_esperar)
    return hitl, decision


def _gestor() -> tuple[GestorDeAdmisionesNativas, list[str]]:
    avisos: list[str] = []

    async def _notificar(mensaje: str) -> None:
        avisos.append(mensaje)

    return GestorDeAdmisionesNativas(notificar=_notificar), avisos


async def _terminar(gestor: GestorDeAdmisionesNativas) -> None:
    assert gestor.actual is not None and gestor.actual.tarea is not None
    await gestor.actual.tarea


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


async def test_admitir_describe_for_approval_muestra_el_nombre_sin_formato_invisible() -> None:
    # U+202E reordena lo que se ve: el operador aprobaría un nombre distinto del que se ejecuta.
    servicio = _servicio()
    estrategia = AdmitirPluginNativoStrategy(service=servicio)
    texto = estrategia.describe_for_approval({"plugin": "Mod‮pse.esl"})
    assert "‮" not in texto and "Mod\\u202epse.esl" in texto
    await estrategia.execute({"plugin": "Mod‮pse.esl"})
    servicio.admitir.assert_awaited_once_with("Mod‮pse.esl")  # se ejecuta el nombre real


def test_admitir_describe_for_approval_muestra_el_nombre_entero_dentro_del_detalle_del_gate() -> None:
    from sky_claw.app.orchestrator.tool_strategies.middleware import _describe_for_approval

    nombre = "M" * 251 + ".esp"  # el máximo del esquema
    detalle = _describe_for_approval(AdmitirPluginNativoStrategy(service=_servicio()), {"plugin": nombre})
    assert nombre in detalle, "el gate acota el detalle: el nombre tiene que sobrevivir entero"


def test_admitir_validate_for_approval_rechaza_un_nombre_que_no_se_puede_mostrar_entero() -> None:
    with pytest.raises(ValueError, match="no se puede mostrar"):
        AdmitirPluginNativoStrategy(service=_servicio()).validate_for_approval({"plugin": "​" * 70 + ".esp"})


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
        {"plugin": "Mod.esp", "timeout": TIMEOUT_MAXIMO_LLM + 1},
    ],
)
def test_parametros_de_auditar_validan_plugin_y_timeout(params: dict) -> None:
    with pytest.raises(ValidationError):
        AuditarPluginNativoParams(**params)


# El round de tools del router CANCELA cada llamada a DEFAULT_TOOL_ROUND_TIMEOUT. La auditoría tiene que
# vencer antes por su cuenta, con un error legible ("tiempo límite"), y no por una cancelación muda.


def test_auditar_en_el_agente_vence_antes_que_el_round_del_router() -> None:
    assert TIMEOUT_MAXIMO_LLM + MARGEN_LLM_S < DEFAULT_TOOL_ROUND_TIMEOUT
    assert TIMEOUT_POR_DEFECTO + MARGEN_LLM_S < DEFAULT_TOOL_ROUND_TIMEOUT
    assert AuditarPluginNativoParams.model_json_schema()["properties"]["timeout"]["anyOf"][0]["maximum"] == (
        TIMEOUT_MAXIMO_LLM
    )


async def test_auditar_handler_no_pide_aprobacion_y_usa_el_margen_del_agente() -> None:
    servicio = _servicio()
    salida = json.loads(await auditar_plugin_nativo(servicio, "Mod.esp", 60))
    assert salida["aprobado"] is True
    servicio.auditar.assert_awaited_once_with("Mod.esp", timeout=60, margen_s=MARGEN_LLM_S)


async def test_auditar_handler_acota_el_timeout_al_plazo_del_router() -> None:
    # Defensa en profundidad: aunque el esquema ya lo impide, el handler nunca pide más de lo que el round aguanta.
    servicio = _servicio()
    await auditar_plugin_nativo(servicio, "Mod.esp", 120)
    assert servicio.auditar.await_args.kwargs["timeout"] == TIMEOUT_MAXIMO_LLM


async def test_auditar_handler_sin_servicio_explica_la_configuracion() -> None:
    salida = json.loads(await auditar_plugin_nativo(None, "Mod.esp"))
    assert salida["success"] is False and "XEDIT_BRIDGE_ROOT" in salida["message"]


def test_el_presupuesto_del_resultado_entra_en_el_limite_del_router() -> None:
    # Contra lo que el router COMPARA de verdad (literal o constante, en cada camino), no solo contra la constante:
    # mismo detector que el ancla hermana de parallax.
    from tests.test_evaluar_parallax_elegibilidad_superficies import _literales_de_recorte_del_router

    topes = _literales_de_recorte_del_router()
    assert topes and MAX_TOOL_RESULT_CHARS in topes, "el router tiene que recortar con MAX_TOOL_RESULT_CHARS"
    assert min(topes) > PRESUPUESTO_RESULTADO_LLM


async def test_auditar_handler_resultado_grande_queda_json_valido_bajo_el_limite_del_router() -> None:
    # El router recorta por posición a MAX_TOOL_RESULT_CHARS: sin podar acá, el JSON se rompería dentro de
    # «hallazgos» y se perderían el contador de omitidos y el log_path.
    hallazgo = {c: "x" * 300 for c in ("signature", "formid", "record_name", "path", "description")}
    servicio = _servicio()
    servicio.auditar.return_value = {
        "success": True,
        "message": "",
        "aprobado": False,
        "hallazgos": [dict(hallazgo) for _ in range(50)],
        "hallazgos_omitidos": 7,
        "fallas": [],
        "log_path": "C:/bridge/run.log",
    }
    texto = await auditar_plugin_nativo(servicio, "Mod.esp")
    assert len(texto) <= PRESUPUESTO_RESULTADO_LLM
    salida = json.loads(texto)
    assert 0 < len(salida["hallazgos"]) < 50
    assert salida["hallazgos_omitidos"] == 7 + 50 - len(salida["hallazgos"])
    assert salida["log_path"] == "C:/bridge/run.log"


async def test_auditar_handler_resultado_chico_sale_intacto() -> None:
    servicio = _servicio()
    assert json.loads(await auditar_plugin_nativo(servicio, "Mod.esp")) == servicio.auditar.return_value


# La admisión espera una decisión humana y copia hasta 900 s: en el agente es un TRABAJO en segundo plano
# (como generate_lods). Responde enseguida; la aprobación y el resultado viajan por el canal del guard.


async def test_admitir_handler_sin_hitl_rechaza_y_no_copia() -> None:
    servicio, (gestor, _) = _servicio(), _gestor()
    salida = json.loads(await admitir_plugin_nativo(servicio, None, "Mod.esp", admisiones=gestor))
    assert salida["success"] is False and "aprobación" in salida["message"]
    assert gestor.actual is None
    servicio.admitir.assert_not_awaited()


async def test_admitir_handler_sin_gestor_de_trabajos_rechaza_y_no_copia() -> None:
    servicio = _servicio()
    salida = json.loads(await admitir_plugin_nativo(servicio, _hitl(Decision.APPROVED), "Mod.esp"))
    assert salida["success"] is False and "segundo plano" in salida["message"]
    servicio.admitir.assert_not_awaited()


async def test_admitir_handler_responde_sin_esperar_la_decision_del_operador() -> None:
    servicio, (gestor, avisos) = _servicio(), _gestor()
    hitl, decision = _hitl_pendiente()

    salida = json.loads(
        await asyncio.wait_for(admitir_plugin_nativo(servicio, hitl, "Mod.esp", admisiones=gestor), timeout=1)
    )
    assert (salida["success"], salida["estado"]) == (True, "esperando_aprobacion") and salida["trabajo_id"]
    await asyncio.sleep(0)
    servicio.admitir.assert_not_awaited()

    decision.set_result(Decision.APPROVED)
    await _terminar(gestor)
    servicio.admitir.assert_awaited_once_with("Mod.esp")
    assert gestor.actual is not None and gestor.actual.estado == "exitosa"
    assert len(avisos) == 1 and "Mod.esp" in avisos[0] and "auditar_plugin_nativo" in avisos[0]


async def test_admitir_aprobado_pide_con_la_categoria_de_la_gui_y_el_perfil() -> None:
    servicio, hitl, (gestor, _) = _servicio(), _hitl(Decision.APPROVED), _gestor()
    await admitir_plugin_nativo(servicio, hitl, "Mod.esp", admisiones=gestor)
    await _terminar(gestor)
    pedido = hitl.request_approval.await_args.kwargs
    # Misma categoría que el gate de la GUI (middleware.py): las dos superficies piden lo mismo y
    # solo «Modo local», elegido por el operador, la auto-aprueba.
    assert pedido["category"] == "tool_execution"
    assert "Mod.esp" in pedido["reason"] and "prueva huevo 3" in pedido["reason"]


@pytest.mark.parametrize("decision", [Decision.DENIED, Decision.TIMEOUT])
async def test_admitir_denegado_o_vencido_no_copia_y_avisa(decision: Decision) -> None:
    servicio, (gestor, avisos) = _servicio(), _gestor()
    await admitir_plugin_nativo(servicio, _hitl(decision), "Mod.esp", admisiones=gestor)
    await _terminar(gestor)
    servicio.admitir.assert_not_awaited()
    assert gestor.actual is not None and gestor.actual.estado == "rechazada"
    assert len(avisos) == 1 and "no se copió nada" in avisos[0]


@pytest.mark.parametrize(
    ("nombre", "visible"),
    [("M" * 251 + ".esp", "M" * 251 + ".esp"), ("Mod\u202epse.esl", "Mod\\u202epse.esl")],
    ids=["largo_maximo", "override_de_direccion"],
)
async def test_admitir_pedido_muestra_el_nombre_entero_y_sin_formato_invisible(nombre: str, visible: str) -> None:
    # Dos nombres con el mismo prefijo largo no pueden dar el mismo pedido, y lo que se ve es lo que se copia.
    servicio, hitl, (gestor, _) = _servicio(), _hitl(Decision.APPROVED), _gestor()
    await admitir_plugin_nativo(servicio, hitl, nombre, admisiones=gestor)
    await _terminar(gestor)
    razon = hitl.request_approval.await_args.kwargs["reason"]
    assert visible in razon and "\u202e" not in razon
    servicio.admitir.assert_awaited_once_with(nombre)


async def test_admitir_nombre_que_no_se_puede_mostrar_no_pide_aprobacion() -> None:
    servicio, hitl, (gestor, _) = _servicio(), _hitl(Decision.APPROVED), _gestor()
    salida = json.loads(await admitir_plugin_nativo(servicio, hitl, "\u200b" * 70 + ".esp", admisiones=gestor))
    assert salida["success"] is False and "no se puede mostrar" in salida["message"]
    hitl.request_approval.assert_not_awaited()
    assert gestor.actual is None


async def test_admitir_con_una_admision_viva_no_lanza_otra() -> None:
    servicio, (gestor, _) = _servicio(), _gestor()
    hitl, pendiente = _hitl_pendiente()
    primera = json.loads(await admitir_plugin_nativo(servicio, hitl, "Mod.esp", admisiones=gestor))
    segunda = json.loads(await admitir_plugin_nativo(servicio, hitl, "Otro.esp", admisiones=gestor))
    assert segunda["success"] is False and segunda["trabajo_id"] == primera["trabajo_id"]
    pendiente.set_result(Decision.DENIED)
    await _terminar(gestor)
    assert hitl.request_approval.await_count == 1


async def test_admitir_servicio_que_lanza_queda_fallida_y_avisa() -> None:
    servicio, (gestor, avisos) = _servicio(), _gestor()
    servicio.admitir.side_effect = RuntimeError("explotó")
    await admitir_plugin_nativo(servicio, _hitl(Decision.APPROVED), "Mod.esp", admisiones=gestor)
    await _terminar(gestor)
    assert gestor.actual is not None and gestor.actual.estado == "fallida"
    assert len(avisos) == 1 and "explotó" in avisos[0]


async def test_cerrar_cancela_la_admision_viva_y_avisa_una_vez() -> None:
    servicio, (gestor, avisos) = _servicio(), _gestor()
    hitl, _decision = _hitl_pendiente()
    await admitir_plugin_nativo(servicio, hitl, "Mod.esp", admisiones=gestor)
    await asyncio.sleep(0)
    await gestor.cerrar()
    assert gestor.actual is not None and gestor.actual.estado == "cancelada"
    servicio.admitir.assert_not_awaited()
    assert len(avisos) == 1 and "cancel" in avisos[0]
    await gestor.cerrar()  # idempotente: no repite el aviso
    assert len(avisos) == 1


async def test_auditar_handler_con_la_admision_en_curso_lo_dice_y_no_llama_al_bridge() -> None:
    servicio, (gestor, _) = _servicio(), _gestor()
    hitl, pendiente = _hitl_pendiente()
    await admitir_plugin_nativo(servicio, hitl, "Mod.esp", admisiones=gestor)
    salida = json.loads(await auditar_plugin_nativo(servicio, "mod.ESP", admisiones=gestor))
    assert salida["success"] is False and salida["aprobado"] is False and "en curso" in salida["message"]
    servicio.auditar.assert_not_awaited()
    otra = json.loads(await auditar_plugin_nativo(servicio, "Otro.esp", admisiones=gestor))
    assert otra["aprobado"] is True, "la admisión en curso de OTRO plugin no bloquea esta auditoría"
    pendiente.set_result(Decision.DENIED)
    await _terminar(gestor)


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
    gestor, _ = _gestor()
    registro = AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
        hitl=hitl,
        path_resolver=resolver,
        admisiones_nativas=gestor,
    )

    salida = json.loads(await registro.execute("admitir_plugin_nativo", {"plugin": "Mod.esp"}))
    assert salida["success"] is True and salida["estado"] == "esperando_aprobacion"
    await _terminar(gestor)
    assert construidos == [resolver], "el servicio tiene que usar el MISMO resolver que la GUI"
    hitl.request_approval.assert_awaited_once()
    servicio.admitir.assert_awaited_once_with("Mod.esp")

    # La auditoría del registro llega al servicio con el margen del agente, no con el de la GUI.
    await registro.execute("auditar_plugin_nativo", {"plugin": "Mod.esp"})
    servicio.auditar.assert_awaited_once_with("Mod.esp", timeout=None, margen_s=MARGEN_LLM_S)


def _llamadas(relativa: str, nombre: str) -> list[ast.Call]:
    arbol = ast.parse((RAIZ / relativa).read_text(encoding="utf-8"))
    return [
        n for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == nombre
    ]


def test_el_app_context_le_da_el_gestor_de_admisiones_al_registro_y_lo_cierra() -> None:
    # Sin el gestor, la admisión del agente falla cerrada; sin el cleanup, un apagado la dejaría colgada.
    registros = _llamadas("sky_claw/app_context.py", "AsyncToolRegistry")
    assert registros, "el detector tiene que ver la construcción del registro"
    for llamada in registros:
        assert "admisiones_nativas" in {kw.arg for kw in llamada.keywords}
    fuente = (RAIZ / "sky_claw/app_context.py").read_text(encoding="utf-8")
    cierres = [
        n
        for n in ast.walk(ast.parse(fuente))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_push_startup_cleanup"
        and n.args
        and isinstance(n.args[0], ast.Attribute)
        and n.args[0].attr == "cerrar"
        and isinstance(n.args[0].value, ast.Name)
        and n.args[0].value.id == "admisiones_nativas"
    ]
    assert len(cierres) == 1


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
