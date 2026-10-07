"""Wiring de la observación de actividad de la etapa 9.

El observador (``dyndolod_actividad`` + el ciclo del servicio) tiene sus propios tests. Lo que
ninguno puede decir es si el proceso REAL lo enciende y si algún camino a la herramienta se lo
salta. Propiedades que este archivo fija:

* El composition root —el único sitio productivo que EJECUTA la etapa 9— enciende la
  observación con los defaults de :class:`ConfiguracionDeObservacion`. Sin esto el servicio
  quedaría apagado (``observacion=None``) en producción y nadie lo notaría: no falla nada,
  simplemente no hay avisos.
* El preview NO la enciende: sólo corre ``execute(dry_run=True)``, que retorna antes de lanzar
  ninguna herramienta; observar un log que no existe sería ruido.
* **Hay un único camino al runner.** Una segunda superficie que llamara a
  ``run_full_pipeline`` por su cuenta ejecutaría la etapa 9 sin observador, sin notificación y
  sin el ciclo del servicio (el defecto dominante de este repo: dos superficies, un recurso).
  El censo se congela por igualdad literal.
"""

from __future__ import annotations

import ast
import pathlib

from sky_claw.local.tools.dyndolod_actividad import ConfiguracionDeObservacion
from tests.test_orchestration_composition import _build_composicion_con_dobles

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"
COMPOSITION = PAQUETE / "app" / "orchestrator" / "orchestration_composition.py"
PREVIEW = PAQUETE / "app" / "orchestrator" / "preview" / "chain_preview_service.py"

#: Archivos de producción que llaman a ``run_full_pipeline``. Igualdad literal: un llamador nuevo rompe
#: este ancla hasta que se decida cómo participa del ciclo de vida de la etapa 9 (lock, journal,
#: eventos, observación).
LLAMADORES_DE_RUN_FULL_PIPELINE = frozenset({"sky_claw/local/tools/dyndolod_service.py"})


def _arbol(archivo: pathlib.Path) -> ast.Module:
    return ast.parse(archivo.read_text(encoding="utf-8"))


def _nombre_de(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _llamadas(arbol: ast.Module, nombre: str) -> list[ast.Call]:
    return [nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and _nombre_de(nodo.func) == nombre]


def test_la_composicion_real_enciende_la_observacion_con_los_defaults() -> None:
    composition = _build_composicion_con_dobles()

    assert composition.dyndolod_service._observacion == ConfiguracionDeObservacion()


def test_el_composition_root_pasa_la_observacion_al_servicio() -> None:
    """La misma propiedad vista en el código: el kwarg está y construye la configuración, no un ``None``."""
    [llamada] = _llamadas(_arbol(COMPOSITION), "DynDOLODPipelineService")

    [valor] = [kw.value for kw in llamada.keywords if kw.arg == "observacion"]

    assert isinstance(valor, ast.Call)
    assert _nombre_de(valor.func) == "ConfiguracionDeObservacion"


def test_el_preview_no_observa_porque_solo_hace_dry_run() -> None:
    llamadas = _llamadas(_arbol(PREVIEW), "DynDOLODPipelineService")
    assert llamadas, "el preview dejó de construir el servicio: revisá este ancla"
    for llamada in llamadas:
        assert "observacion" not in {kw.arg for kw in llamada.keywords}, (
            "el preview sólo ejecuta dry_run=True (no lanza ninguna herramienta): no hay log que observar"
        )
    ejecuciones = _llamadas(_arbol(PREVIEW), "execute")
    assert ejecuciones
    for ejecucion in ejecuciones:
        [dry_run] = [kw.value for kw in ejecucion.keywords if kw.arg == "dry_run"]
        assert isinstance(dry_run, ast.Constant) and dry_run.value is True


def test_hay_un_unico_camino_productivo_al_runner() -> None:
    llamadores = {
        archivo.relative_to(RAIZ).as_posix()
        for archivo in sorted(PAQUETE.rglob("*.py"))
        if _llamadas(_arbol(archivo), "run_full_pipeline")
    }

    assert llamadores == LLAMADORES_DE_RUN_FULL_PIPELINE, (
        "apareció (o desapareció) un llamador de run_full_pipeline: ejecutaría la etapa 9 por fuera del ciclo "
        "del servicio (lock, journal, eventos, observación). Decidí cómo participa o declaralo acá"
    )
