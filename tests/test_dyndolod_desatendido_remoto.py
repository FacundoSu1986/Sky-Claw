"""Etapa 9 operable desde la superficie REMOTA — Telegram / ``/api/chat`` (#661).

Tres propiedades, cada una con su ancla:

* **Responde enseguida y avisa al terminar.** El round de tools del router corta
  a los 120 s; la etapa 9 dura horas. La tool lanza un trabajo en segundo plano y
  el resultado llega por la superficie de avisos del guard.
* **Un recurso, una autoridad.** El trabajo delega en el servicio que la GUI
  publica (el del composition root), nunca en uno propio.
* **Dos superficies, el mismo parámetro.** La estrategia del dispatcher (GUI) y la
  tool del agente (remota) exponen ``interaction_mode`` hacia el MISMO
  ``DynDOLODPipelineService.execute``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import pathlib
from unittest.mock import MagicMock

import pydantic
import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.lod_tools import (
    MOTIVO_SIN_SERVICIO,
    GestorDeTrabajosDeLods,
    generate_lods,
    lods_status,
)
from sky_claw.app.agent.tools.schemas import GenerateLodsParams
from sky_claw.app.orchestrator.dyndolod_desatendido_hitl import (
    AprobadorDesatendidoHITL,
    texto_de_configuracion,
    texto_de_modal,
)
from sky_claw.app.orchestrator.tool_strategies.generate_lods import _VALID_LOD_KEYS
from sky_claw.app.security.hitl import CATEGORIA_DYNDOLOD_DESATENDIDO, Decision, HITLGuard
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.dyndolod_uia_driver import (
    ResumenDeHerramienta,
    SolicitudAprobacionDesatendida,
    SolicitudModalConocido,
)
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion

RAIZ = pathlib.Path(__file__).resolve().parents[1]


class ServicioFalso:
    def __init__(self, resultado: dict | None = None, *, bloquear: bool = False, lanzar: bool = False) -> None:
        self.llamadas: list[dict] = []
        self._resultado = resultado if resultado is not None else {"success": True, "message": ""}
        self._liberar = asyncio.Event()
        self._bloquear = bloquear
        self._lanzar = lanzar

    async def execute(self, **kwargs):
        self.llamadas.append(kwargs)
        if self._bloquear:
            await self._liberar.wait()
        if self._lanzar:
            raise RuntimeError("explotó")
        return self._resultado

    def liberar(self) -> None:
        self._liberar.set()


class Avisos:
    def __init__(self) -> None:
        self.mensajes: list[str] = []

    async def __call__(self, mensaje: str) -> None:
        self.mensajes.append(mensaje)


async def _esperar(gestor: GestorDeTrabajosDeLods) -> None:
    assert gestor.actual is not None and gestor.actual.tarea is not None
    await asyncio.wait_for(gestor.actual.tarea, timeout=5)


# =============================================================================
# Gestor de trabajos
# =============================================================================


async def test_sin_servicio_publicado_la_tool_lo_dice_y_no_lanza_nada():
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: None)
    respuesta = gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido")
    assert respuesta == {"success": False, "message": MOTIVO_SIN_SERVICIO}
    assert gestor.actual is None


async def test_el_trabajo_responde_enseguida_y_avisa_al_terminar():
    servicio = ServicioFalso(bloquear=True)
    avisos = Avisos()
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio, notificar=avisos)

    respuesta = gestor.iniciar(preset="High", run_texgen=False, create_snapshot=True, interaction_mode="desatendido")

    assert respuesta["success"] is True and respuesta["message"] == ""
    assert respuesta["estado"] == "iniciado"
    await asyncio.sleep(0)
    assert gestor.en_curso()
    assert avisos.mensajes == []
    servicio.liberar()
    await _esperar(gestor)
    assert servicio.llamadas == [
        {"preset": "High", "run_texgen": False, "create_snapshot": True, "interaction_mode": "desatendido"}
    ]
    assert gestor.actual.estado == "exitoso"
    assert len(avisos.mensajes) == 1 and avisos.mensajes[0].startswith("✅")


async def test_un_solo_trabajo_a_la_vez():
    servicio = ServicioFalso(bloquear=True)
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio)
    primero = gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido")
    segundo = gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido")
    assert segundo["success"] is False
    assert segundo["trabajo_id"] == primero["trabajo_id"]
    servicio.liberar()
    await _esperar(gestor)
    assert len(servicio.llamadas) == 1


async def test_un_fallo_desatendido_llega_al_operador_con_su_codigo():
    servicio = ServicioFalso(
        {
            "success": False,
            "message": "TexGen execution failed: TexGen: KNOWN_MODAL_NOT_APPROVED: denegada",
            "interaction_mode": "desatendido",
            "unattended_error_codes": ["KNOWN_MODAL_NOT_APPROVED"],
        }
    )
    avisos = Avisos()
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio, notificar=avisos)
    gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido")
    await _esperar(gestor)
    assert gestor.actual.estado == "fallido"
    assert "[KNOWN_MODAL_NOT_APPROVED]" in avisos.mensajes[0]
    estado = gestor.estado()
    assert estado["resultado"]["unattended_error_codes"] == ["KNOWN_MODAL_NOT_APPROVED"]


async def test_un_servicio_que_lanza_no_deja_el_trabajo_colgado():
    avisos = Avisos()
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: ServicioFalso(lanzar=True), notificar=avisos)
    gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="asistido")
    await _esperar(gestor)
    assert gestor.actual.estado == "fallido"
    assert avisos.mensajes and avisos.mensajes[0].startswith("❌")


async def test_cerrar_cancela_el_trabajo_vivo():
    servicio = ServicioFalso(bloquear=True)
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio)
    gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido")
    await asyncio.sleep(0)
    await gestor.cerrar()
    assert gestor.actual.estado == "cancelado"
    assert gestor.actual.tarea.done()


async def test_un_aviso_que_falla_no_cambia_el_veredicto():
    async def _roto(_mensaje: str) -> None:
        raise RuntimeError("Telegram caído")

    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: ServicioFalso(), notificar=_roto)
    gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido")
    await _esperar(gestor)
    assert gestor.actual.estado == "exitoso"


async def test_estado_sin_trabajos_y_handlers_sin_gestor():
    assert GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: None).estado()["estado"] == "sin_trabajos"
    assert json.loads(await lods_status(None))["success"] is False
    respuesta = json.loads(
        await generate_lods(
            None, preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido"
        )
    )
    assert respuesta == {"success": False, "message": MOTIVO_SIN_SERVICIO}


# =============================================================================
# Tools del agente
# =============================================================================


def _registry(gestor: GestorDeTrabajosDeLods | None) -> AsyncToolRegistry:
    return AsyncToolRegistry(registry=MagicMock(), mo2=MagicMock(), sync_engine=MagicMock(), lod_jobs=gestor)


async def test_la_tool_remota_lanza_el_trabajo_por_el_registro():
    servicio = ServicioFalso()
    registro = _registry(GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio))
    respuesta = json.loads(await registro.execute("generate_lods", {"interaction_mode": "desatendido"}))
    assert respuesta["success"] is True
    await asyncio.sleep(0.05)
    assert servicio.llamadas == [
        {"preset": "Medium", "run_texgen": True, "create_snapshot": True, "interaction_mode": "desatendido"}
    ]
    estado = json.loads(await registro.execute("lods_status", {}))
    assert estado["trabajo_id"] == respuesta["trabajo_id"]


async def test_el_modo_es_obligatorio_y_cerrado_en_el_schema():
    registro = _registry(GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: ServicioFalso()))
    with pytest.raises(pydantic.ValidationError):
        await registro.execute("generate_lods", {})
    with pytest.raises(pydantic.ValidationError):
        await registro.execute("generate_lods", {"interaction_mode": "unattended"})
    with pytest.raises(pydantic.ValidationError):
        await registro.execute("generate_lods", {"interaction_mode": "desatendido", "preset": "Ultra"})


def test_el_schema_de_la_tool_es_el_que_ve_el_llm():
    esquema = GenerateLodsParams.model_json_schema()
    assert esquema["required"] == ["interaction_mode"]
    assert set(esquema["properties"]) == {"interaction_mode", "preset", "run_texgen", "create_snapshot"}


def test_las_dos_superficies_piden_el_mismo_parametro_al_mismo_servicio():
    """Paridad enumerada: GUI (dispatcher) y remota (agente) → ``execute`` del servicio."""
    del_servicio = set(inspect.signature(DynDOLODPipelineService.execute).parameters) - {"self"}
    de_la_tool = set(GenerateLodsParams.model_fields)
    assert de_la_tool <= del_servicio
    assert de_la_tool <= _VALID_LOD_KEYS
    assert "interaction_mode" in _VALID_LOD_KEYS & de_la_tool


# =============================================================================
# Cableado: AppContext / bootloader
# =============================================================================


def _arbol(relativa: str) -> ast.Module:
    return ast.parse((RAIZ / relativa).read_text(encoding="utf-8"))


def test_el_app_context_le_da_el_gestor_al_registro_del_agente():
    arbol = _arbol("sky_claw/app_context.py")
    registros = [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "AsyncToolRegistry"
    ]
    assert registros, "el detector tiene que ver la construcción del registro"
    for llamada in registros:
        assert "lod_jobs" in {kw.arg for kw in llamada.keywords}


def test_el_bootloader_publica_el_servicio_del_supervisor():
    arbol = _arbol("sky_claw/app/gui/_bootloader.py")
    publicaciones = [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "publicar_servicio_de_etapa9"
    ]
    assert len(publicaciones) == 1
    argumento = publicaciones[0].args[0]
    assert isinstance(argumento, ast.Attribute) and argumento.attr == "dyndolod_service"


def test_ningun_modulo_del_agente_construye_un_servicio_de_etapa9():
    culpables = []
    for archivo in sorted((RAIZ / "sky_claw" / "app" / "agent").rglob("*.py")):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Name)
                and nodo.func.id in {"DynDOLODPipelineService", "DynDOLODRunner"}
            ):
                culpables.append(str(archivo.relative_to(RAIZ)))
    assert culpables == []


# =============================================================================
# Adaptador HITL remoto
# =============================================================================


def _solicitud_configuracion() -> SolicitudAprobacionDesatendida:
    return SolicitudAprobacionDesatendida(
        herramientas=(
            ResumenDeHerramienta(
                tool="DynDOLOD",
                version="3.0.0.209",
                exe_sha256="b67625eb" + "0" * 56,
                preset=r"C:\Modding\DynDOLOD\Edit Scripts\DynDOLOD\Presets\DynDOLOD_SSE_Default.ini",
                preset_huella="abcdef0123456789" * 4,
                preset_de_calidad="Medium",
                output_esperado=r"E:\Work\DynDOLOD\DynDOLOD",
            ),
        ),
        preset_pedido="Medium",
        run_texgen=False,
        timeout_seconds=900.0,
    )


@pytest.mark.parametrize(
    ("decision", "esperado"),
    [
        (Decision.APPROVED, ResultadoConfirmacion.APROBADA),
        (Decision.DENIED, ResultadoConfirmacion.DENEGADA),
        (Decision.TIMEOUT, ResultadoConfirmacion.TIMEOUT),
    ],
)
async def test_el_aprobador_remoto_traduce_la_decision_del_guard(decision, esperado):
    guard = HITLGuard(timeout=5)
    vistos: list[object] = []

    async def _notificar(req):
        vistos.append(req)
        await guard.respond(req.request_id, decision is Decision.APPROVED) if decision is not Decision.TIMEOUT else None

    guard._notify = _notificar  # noqa: SLF001 -- el seam de notificación del guard
    aprobador = AprobadorDesatendidoHITL(hitl_guard=guard)
    solicitud = _solicitud_configuracion()
    if decision is Decision.TIMEOUT:
        solicitud = SolicitudAprobacionDesatendida(
            herramientas=solicitud.herramientas, preset_pedido="Medium", run_texgen=False, timeout_seconds=0.05
        )
    assert await aprobador.aprobar_configuracion(solicitud) is esperado
    assert vistos[0].category == CATEGORIA_DYNDOLOD_DESATENDIDO
    assert vistos[0].request_id.startswith("dyndolod-desatendido-")


async def test_sin_guard_el_aprobador_es_canal_no_disponible():
    aprobador = AprobadorDesatendidoHITL(hitl_guard=None)
    assert (
        await aprobador.aprobar_configuracion(_solicitud_configuracion()) is ResultadoConfirmacion.CANAL_NO_DISPONIBLE
    )
    modal = SolicitudModalConocido(
        tool="TexGen", pid=1, modal_id="m", instruccion="x", descripcion="y", timeout_seconds=1.0
    )
    assert await aprobador.aprobar_modal(modal) is ResultadoConfirmacion.CANAL_NO_DISPONIBLE


def test_el_prompt_de_configuracion_muestra_lo_que_se_aprueba():
    reason, detail = texto_de_configuracion(_solicitud_configuracion())
    assert "DESATENDIDA" in reason and "sin intervención en la PC" in reason
    assert "Preset=Medium" in detail
    assert "huella abcdef012345" in detail
    assert r"E:\Work\DynDOLOD\DynDOLOD" in detail


def test_el_prompt_del_modal_cita_la_instruccion_exacta():
    reason, detail = texto_de_modal(
        SolicitudModalConocido(
            tool="TexGen",
            pid=7,
            modal_id="texgen_stitched_lod_previo",
            instruccion="Found stitched object LOD textures from earlier TexGen generation installed in game folder.",
            descripcion="explicación",
            timeout_seconds=900.0,
        )
    )
    assert "'Ignore'" in reason
    assert "Found stitched object LOD textures" in detail


def test_la_gui_no_intercepta_la_categoria_desatendida():
    """La categoría cae al canal remoto: la GUI sólo estaciona sus cuatro categorías propias."""
    fuente = (RAIZ / "sky_claw" / "app" / "gui" / "controllers" / "ritual_runner.py").read_text(encoding="utf-8")
    assert "CATEGORIA_DYNDOLOD_DESATENDIDO" not in fuente
    assert "dyndolod_desatendido" not in fuente
