"""H-11: ``lods_status`` del agente muestra el progreso del trabajo vivo, sin poder romperse por eso.

La superficie del agente (Telegram / ``/api/chat``) es el único consumidor de ``servicio.progreso_actual`` que existe
hoy; la GUI se sumó en P-3c-2. Por eso el conjunto de consumidores está CONGELADO por AST al final de este archivo:
un consumidor nuevo (o el de la GUI) rompe el ancla hasta que alguien decide su receta.

Reglas ancladas:

- ``progreso`` aparece solo mientras la corrida DEL trabajo está ``en_curso``, aunque el servicio guarde un snapshot
  (antes de lanzarla sería el de otra corrida; después de terminar, el rastro de algo que ya acabó);
- sin snapshot, sin servicio o con un proveedor que falla, ``lods_status`` responde igual y sin ``progreso``;
- son hechos de la muestra (tamaño del log, segundos sin cambios) más cuán vieja es la muestra: un latido que dejó de
  llegar también es información. No hay un veredicto «colgado».
"""

from __future__ import annotations

import ast
import json
import pathlib
import time
import types
import typing

import pytest

from sky_claw.app.agent.tools.lod_tools import EstadoDeTrabajo, GestorDeTrabajosDeLods, TrabajoDeLods, lods_status
from sky_claw.app.core.event_payloads import DynDOLODPipelineProgressPayload

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _servicio_con(progreso: DynDOLODPipelineProgressPayload | None) -> types.SimpleNamespace:
    return types.SimpleNamespace(progreso_actual=progreso)


def _gestor_con_trabajo(estado: str, servicio: object | None) -> GestorDeTrabajosDeLods:
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio)
    gestor._actual = TrabajoDeLods(  # noqa: SLF001 -- se arma el estado sin lanzar una corrida real
        id="lods-1",
        parametros={"preset": "Medium"},
        iniciado_en=time.time() - 60,
        estado=estado,  # type: ignore[arg-type]
    )
    return gestor


def _muestra(**cambios: object) -> DynDOLODPipelineProgressPayload:
    base: dict[str, object] = {
        "tool_name": "DynDOLOD",
        "elapsed_seconds": 3725.46,
        "log_path": "C:/DynDOLOD/Logs/DynDOLOD_SSE_log.txt",
        "log_bytes": 8192,
        "log_idle_seconds": 41.04,
        "sampled_at": time.time() - 30,
    }
    base.update(cambios)
    return DynDOLODPipelineProgressPayload(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Qué muestra
# ---------------------------------------------------------------------------


def test_un_trabajo_vivo_muestra_el_progreso_con_hechos_y_la_edad_de_la_muestra() -> None:
    gestor = _gestor_con_trabajo("en_curso", _servicio_con(_muestra()))

    progreso = gestor.estado()["progreso"]

    assert progreso["herramienta"] == "DynDOLOD"
    assert progreso["transcurrido_segundos"] == 3725.5
    assert progreso["log_bytes"] == 8192
    assert progreso["log_sin_cambios_segundos"] == 41.0
    assert progreso["muestra_hace_segundos"] == pytest.approx(30.0, abs=3.0)
    assert not {"colgado", "veredicto", "stalled"} & set(progreso), "son hechos, no un veredicto"


def test_sin_log_el_progreso_informa_solo_el_tiempo() -> None:
    muestra = _muestra(log_path=None, log_bytes=None, log_idle_seconds=None)
    gestor = _gestor_con_trabajo("en_curso", _servicio_con(muestra))

    progreso = gestor.estado()["progreso"]

    assert progreso["log_bytes"] is None and progreso["log_sin_cambios_segundos"] is None
    assert progreso["transcurrido_segundos"] == 3725.5


def test_el_progreso_se_expone_solo_cuando_la_corrida_del_trabajo_esta_en_curso() -> None:
    assert "progreso" in _gestor_con_trabajo("en_curso", _servicio_con(_muestra())).estado()


@pytest.mark.parametrize("sin_corrida", ["iniciado", "esperando_aprobacion"])
def test_antes_de_que_el_trabajo_lance_su_corrida_el_snapshot_no_es_suyo(sin_corrida: str) -> None:
    """Review CodeRabbit #905: mientras espera la aprobación no hay corrida de ESTE trabajo; un snapshot del servicio
    sería el de una corrida directa (GUI) y se le atribuiría al trabajo remoto."""
    gestor = _gestor_con_trabajo(sin_corrida, _servicio_con(_muestra()))

    estado = gestor.estado()

    assert estado["estado"] == sin_corrida
    assert "progreso" not in estado


# ---------------------------------------------------------------------------
# Qué NO muestra
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["exitoso", "fallido", "cancelado"])
def test_un_trabajo_terminado_no_muestra_un_snapshot_rancio(terminal: str) -> None:
    gestor = _gestor_con_trabajo(terminal, _servicio_con(_muestra()))

    assert "progreso" not in gestor.estado()


def test_sin_snapshot_no_hay_clave_progreso() -> None:
    assert "progreso" not in _gestor_con_trabajo("en_curso", _servicio_con(None)).estado()


def test_sin_servicio_no_hay_clave_progreso_y_no_falla() -> None:
    assert "progreso" not in _gestor_con_trabajo("en_curso", None).estado()


def test_un_servicio_sin_el_atributo_no_rompe_la_consulta() -> None:
    """Un doble o un servicio viejo sin ``progreso_actual``: la consulta sigue siendo la de siempre."""
    gestor = _gestor_con_trabajo("en_curso", types.SimpleNamespace())

    estado = gestor.estado()

    assert estado["estado"] == "en_curso" and "progreso" not in estado


def test_un_proveedor_que_falla_no_rompe_lods_status(caplog: pytest.LogCaptureFixture) -> None:
    def roto() -> object:
        raise RuntimeError("el servicio no está listo")

    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=roto)
    gestor._actual = TrabajoDeLods(id="lods-2", parametros={}, iniciado_en=time.time(), estado="en_curso")  # noqa: SLF001

    estado = gestor.estado()

    assert estado["success"] is True and estado["estado"] == "en_curso" and "progreso" not in estado


async def test_el_handler_del_agente_serializa_el_progreso() -> None:
    gestor = _gestor_con_trabajo("en_curso", _servicio_con(_muestra()))

    respuesta = json.loads(await lods_status(gestor))

    assert respuesta["progreso"]["herramienta"] == "DynDOLOD"


# ---------------------------------------------------------------------------
# Con la cuarentena del broker (#904): dos campos aditivos de la MISMA respuesta
# ---------------------------------------------------------------------------
#
# ``progreso`` (#905) cuelga de la rama «hay trabajo» y ``cuarentena_vfs`` (#904) de la respuesta entera. Cada PR por
# separado cubría su campo; el árbol fusionado es el que tiene que cumplir las dos reglas a la vez.

_CUARENTENA = {"activa": True, "razon": "terminalidad desconocida", "como_liberar": "cerrá Sky-Claw y liberala"}


def _gestor_con_cuarentena(estado: str | None, *, servicio: object | None = None, cuarentena: object = _CUARENTENA):
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio, consultar_cuarentena=lambda: cuarentena)  # type: ignore[arg-type,return-value]
    if estado is not None:
        gestor._actual = TrabajoDeLods(  # noqa: SLF001
            id="lods-q",
            parametros={},
            iniciado_en=time.time() - 60,
            estado=estado,  # type: ignore[arg-type]
        )
    return gestor


def test_un_trabajo_en_curso_informa_el_progreso_y_la_cuarentena_a_la_vez() -> None:
    gestor = _gestor_con_cuarentena("en_curso", servicio=_servicio_con(_muestra()))

    estado = gestor.estado()

    assert estado["progreso"]["herramienta"] == "DynDOLOD"
    assert estado["cuarentena_vfs"] == _CUARENTENA


@pytest.mark.parametrize("estado_del_trabajo", typing.get_args(EstadoDeTrabajo))
def test_la_cuarentena_se_informa_en_todos_los_estados_y_el_progreso_solo_en_curso(estado_del_trabajo: str) -> None:
    """Enumera los estados del trabajo: ninguno puede perder ``cuarentena_vfs`` ni ganar un ``progreso`` ajeno."""
    gestor = _gestor_con_cuarentena(estado_del_trabajo, servicio=_servicio_con(_muestra()))

    estado = gestor.estado()

    assert estado["cuarentena_vfs"] == _CUARENTENA
    assert ("progreso" in estado) is (estado_del_trabajo == "en_curso")


def test_sin_ningun_trabajo_la_cuarentena_se_informa_igual() -> None:
    estado = _gestor_con_cuarentena(None, servicio=_servicio_con(_muestra())).estado()

    assert estado["estado"] == "sin_trabajos"
    assert estado["cuarentena_vfs"] == _CUARENTENA
    assert "progreso" not in estado


def test_una_consulta_de_cuarentena_rota_no_apaga_el_progreso(caplog: pytest.LogCaptureFixture) -> None:
    def rota() -> object:
        raise RuntimeError("no se pudo leer el estado del broker")

    gestor = GestorDeTrabajosDeLods(
        proveedor_de_servicio=lambda: _servicio_con(_muestra()),
        consultar_cuarentena=rota,  # type: ignore[arg-type]
    )
    gestor._actual = TrabajoDeLods(id="lods-r", parametros={}, iniciado_en=time.time(), estado="en_curso")  # noqa: SLF001

    estado = gestor.estado()

    assert estado["progreso"]["herramienta"] == "DynDOLOD"
    assert "cuarentena_vfs" not in estado


def test_un_progreso_roto_no_apaga_la_cuarentena() -> None:
    def proveedor_roto() -> object:
        raise RuntimeError("el servicio no está listo")

    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=proveedor_roto, consultar_cuarentena=lambda: _CUARENTENA)
    gestor._actual = TrabajoDeLods(id="lods-p", parametros={}, iniciado_en=time.time(), estado="en_curso")  # noqa: SLF001

    estado = gestor.estado()

    assert estado["cuarentena_vfs"] == _CUARENTENA
    assert "progreso" not in estado


# ---------------------------------------------------------------------------
# Ancla: quién consume el snapshot (instrumento de `RITUAL_TOOL_MAP`)
# ---------------------------------------------------------------------------

#: Los módulos de producción que LEEN ``progreso_actual``: la superficie del agente y, desde P-3c-2, la lectura de la GUI
#: (``etapa9_estado``). Los dos arman los hechos con ``dyndolod_progreso.progreso_en_hechos`` (ancla en
#: ``tests/test_gui_estado_de_la_etapa9.py``). Un consumidor nuevo sin decidir su receta rompe este test.
CONSUMIDORES_DE_PROGRESO_ACTUAL = {
    "sky_claw/app/agent/tools/lod_tools.py",
    "sky_claw/app/gui/controllers/etapa9_estado.py",
}


def _toca_progreso_actual(arbol: ast.AST) -> bool:
    """¿El árbol accede a ``progreso_actual``, directo (``s.progreso_actual``) o por nombre (``getattr``)?"""
    return any(
        (isinstance(nodo, ast.Attribute) and nodo.attr == "progreso_actual")
        or (isinstance(nodo, ast.Constant) and nodo.value == "progreso_actual")
        for nodo in ast.walk(arbol)
    )


def _consumidores_de_progreso_actual() -> set[str]:
    encontrados: set[str] = set()
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        relativa = ruta.relative_to(RAIZ).as_posix()
        if relativa == "sky_claw/local/tools/dyndolod_service.py":
            continue  # el dueño del atributo: lo escribe, no es un consumidor
        if _toca_progreso_actual(ast.parse(ruta.read_text(encoding="utf-8", errors="replace"))):
            encontrados.add(relativa)
    return encontrados


def test_los_consumidores_del_snapshot_de_progreso_estan_congelados() -> None:
    assert _consumidores_de_progreso_actual() == CONSUMIDORES_DE_PROGRESO_ACTUAL


def test_el_detector_del_ancla_ve_los_dos_accesos_y_no_ve_lo_ajeno() -> None:
    """El ancla no puede pasar viendo nada: se prueba la MISMA función que recorre el repo."""
    directo = ast.parse("def f(s):\n    return s.progreso_actual\n")
    por_nombre = ast.parse("def f(s):\n    return getattr(s, 'progreso_actual', None)\n")
    ajeno = ast.parse("def f(s):\n    return s.progreso, 'otro_texto'\n")

    assert _toca_progreso_actual(directo)
    assert _toca_progreso_actual(por_nombre)
    assert not _toca_progreso_actual(ajeno)
