"""H-11: el servicio convierte el latido del runner en un evento y en un snapshot que lee ``lods_status``.

Complementa ``tests/test_dyndolod_progreso.py`` (el runner mide y entrega). Acá se ancla la mitad del servicio:

- el snapshot se guarda ANTES de publicar: si el bus está caído, ``lods_status`` igual ve el progreso;
- el snapshot es de UNA corrida: se limpia al iniciar la siguiente y al terminar, para no mostrar el progreso de
  la corrida anterior como si fuera de la actual;
- el único sitio de producción que construye el runner lo cablea (``on_progress``); si no, el progreso moriría
  en silencio y los demás tests seguirían verdes.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

import sky_claw.local.tools.dyndolod_service as modulo_servicio
from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.core.event_payloads import DynDOLODPipelineProgressPayload
from sky_claw.local.tools.dyndolod_runner import ProgresoDeHerramienta
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.dyndolod_uia_contrato import ModoDeInteraccion

TOPIC_DE_PROGRESO = "pipeline.dyndolod.progress"


def _servicio(bus: AsyncMock) -> DynDOLODPipelineService:
    return DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=bus,
        mo2_profile="Default",
    )


def _bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


def _muestra(**cambios: object) -> ProgresoDeHerramienta:
    base: dict[str, object] = {
        "tool_name": "DynDOLOD",
        "elapsed_seconds": 125.5,
        "log_path": "C:/DynDOLOD/Logs/DynDOLOD_SSE_log.txt",
        "log_bytes": 4096,
        "log_idle_seconds": 7.25,
    }
    base.update(cambios)
    return ProgresoDeHerramienta(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# El payload
# ---------------------------------------------------------------------------


def test_el_payload_de_progreso_es_inmutable_y_estricto() -> None:
    payload = DynDOLODPipelineProgressPayload(tool_name="TexGen", elapsed_seconds=1.0)

    assert payload.log_bytes is None and payload.log_idle_seconds is None and payload.log_path is None
    assert payload.sampled_at > 0
    with pytest.raises(ValidationError):
        payload.tool_name = "otro"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DynDOLODPipelineProgressPayload(tool_name="TexGen", elapsed_seconds="mucho")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Guardar y publicar
# ---------------------------------------------------------------------------


async def test_sin_corrida_no_hay_progreso() -> None:
    assert _servicio(_bus()).progreso_actual is None


async def test_el_latido_guarda_el_snapshot_y_publica_el_evento() -> None:
    bus = _bus()
    servicio = _servicio(bus)

    await servicio._al_progresar(_muestra())  # noqa: SLF001 -- es el callback que recibe el runner

    actual = servicio.progreso_actual
    assert actual is not None
    assert (actual.tool_name, actual.elapsed_seconds, actual.log_bytes, actual.log_idle_seconds) == (
        "DynDOLOD",
        125.5,
        4096,
        7.25,
    )
    bus.publish.assert_awaited_once()
    evento = bus.publish.await_args.args[0]
    assert evento.topic == TOPIC_DE_PROGRESO
    assert evento.payload["tool_name"] == "DynDOLOD"
    assert evento.payload["log_bytes"] == 4096
    assert evento.payload["sampled_at"] == actual.sampled_at


async def test_si_el_bus_falla_el_snapshot_ya_quedo_guardado() -> None:
    """``lods_status`` no depende de que el bus esté vivo: el snapshot va primero."""
    bus = _bus()
    bus.publish.side_effect = RuntimeError("dispatcher caído")
    servicio = _servicio(bus)

    with pytest.raises(RuntimeError):  # el guard best-effort vive en el runner; el servicio no lo duplica
        await servicio._al_progresar(_muestra())  # noqa: SLF001

    assert servicio.progreso_actual is not None
    assert servicio.progreso_actual.elapsed_seconds == 125.5


# ---------------------------------------------------------------------------
# El snapshot es de UNA corrida
# ---------------------------------------------------------------------------


async def test_iniciar_una_corrida_descarta_el_progreso_de_la_anterior() -> None:
    servicio = _servicio(_bus())
    await servicio._al_progresar(_muestra())  # noqa: SLF001
    assert servicio.progreso_actual is not None

    await servicio._publish_started(preset="Medium", run_texgen=True, modo=ModoDeInteraccion.ASISTIDO)  # noqa: SLF001

    assert servicio.progreso_actual is None


async def test_terminar_la_corrida_descarta_su_progreso() -> None:
    servicio = _servicio(_bus())
    await servicio._al_progresar(_muestra())  # noqa: SLF001

    await servicio._publish_completed(  # noqa: SLF001
        preset="Medium",
        run_texgen=True,
        success=True,
        texgen_success=True,
        dyndolod_success=True,
        errors=(),
        duration_seconds=10.0,
        rolled_back=False,
    )

    assert servicio.progreso_actual is None


async def test_la_corrida_descarta_el_progreso_rancio_antes_del_preflight() -> None:
    """Review PR-Agent #905: ``lods_status`` atribuye el snapshot al trabajo ``en_curso``.

    El gestor marca ``en_curso`` y llama a ``execute`` sin ningún ``await`` en medio, pero ``_publish_started`` (que
    también descarta el snapshot) llega recién DESPUÉS del preflight, que es un ``await``. Si una corrida anterior no
    llegó a limpiar su rastro, durante ese preflight se vería como progreso del trabajo nuevo. ``execute`` lo descarta
    de forma síncrona, ANTES del primer ``await``.
    """
    visto_en_el_preflight: list[object] = []

    class _PreflightEnRojo:
        async def run(self) -> MagicMock:
            visto_en_el_preflight.append(servicio.progreso_actual)
            return MagicMock(blocks_mutations=True, checks=[])

    servicio = DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=_bus(),
        mo2_profile="Default",
        preflight=_PreflightEnRojo(),  # type: ignore[arg-type]
    )
    await servicio._al_progresar(_muestra())  # noqa: SLF001 -- el rastro de una corrida que no se limpió
    assert servicio.progreso_actual is not None

    resultado = await servicio.execute()

    assert resultado["reason"] == "PreflightBlocked"
    assert visto_en_el_preflight == [None]


async def test_un_preview_no_borra_el_progreso_de_una_corrida_viva(monkeypatch: pytest.MonkeyPatch) -> None:
    """El preview (``dry_run``) es de solo lectura: no puede dejar sin muestra a la corrida que está en curso."""
    servicio = _servicio(_bus())
    monkeypatch.setattr(servicio, "_preview", AsyncMock(return_value={"success": True, "message": ""}))
    await servicio._al_progresar(_muestra())  # noqa: SLF001

    await servicio.execute(dry_run=True)

    assert servicio.progreso_actual is not None


# ---------------------------------------------------------------------------
# TODA salida de una corrida descarta el snapshot (hallazgo de CodeRabbit en #937)
# ---------------------------------------------------------------------------
#
# Antes el snapshot se descartaba al iniciar y en ``_publish_completed``, que es lo único que las salidas «normales»
# alcanzan. Una salida que no llega ahí (una excepción, una cancelación dentro de un handler de fallo, un retorno
# temprano) dejaba la última muestra de una corrida que ya terminó, y ``lods_status`` / la GUI la seguían mostrando.
# El mecanismo es UNO, en el límite de ``execute`` (``_descarta_el_progreso_al_salir``): no depende de qué handler
# fue el que salió. La matriz de abajo lo ejercita sobre el ``execute`` REAL, una fila por clase de salida.


class _PreflightQueSiembraYSale:
    """Entre el inicio de la corrida y su salida el latido del runner deja una muestra: acá la deja el preflight.

    ``execute`` descarta la muestra de forma síncrona al arrancar, así que sembrarla antes de llamarlo no prueba nada:
    tiene que aparecer DURANTE la corrida, como un latido real, y la salida tiene que ocurrir sin pasar por
    ``_publish_completed``.
    """

    def __init__(self, servicio_ref: list[DynDOLODPipelineService], desenlace: str) -> None:
        self._servicio_ref = servicio_ref
        self._desenlace = desenlace
        self.muestra_sembrada = False

    async def run(self) -> MagicMock:
        servicio = self._servicio_ref[0]
        await servicio._al_progresar(_muestra())  # noqa: SLF001
        self.muestra_sembrada = servicio.progreso_actual is not None
        if self._desenlace == "excepcion":
            raise RuntimeError("falla inesperada del preflight")
        if self._desenlace == "cancelacion":
            raise asyncio.CancelledError
        return MagicMock(blocks_mutations=True, checks=[])  # "retorno_temprano": PreflightBlocked


def _servicio_con_preflight(desenlace: str) -> tuple[DynDOLODPipelineService, _PreflightQueSiembraYSale]:
    ref: list[DynDOLODPipelineService] = []
    preflight = _PreflightQueSiembraYSale(ref, desenlace)
    servicio = DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=_bus(),
        mo2_profile="Default",
        preflight=preflight,  # type: ignore[arg-type]
    )
    ref.append(servicio)
    return servicio, preflight


@pytest.mark.parametrize(
    ("desenlace", "esperado"),
    [
        ("retorno_temprano", None),
        ("excepcion", RuntimeError),
        ("cancelacion", asyncio.CancelledError),
    ],
)
async def test_toda_salida_de_una_corrida_descarta_el_progreso(desenlace: str, esperado: object) -> None:
    servicio, preflight = _servicio_con_preflight(desenlace)

    if esperado is None:
        await servicio.execute()
    else:
        with pytest.raises(esperado):  # type: ignore[call-overload]
            await servicio.execute()

    assert preflight.muestra_sembrada, "precondición: la corrida llegó a tener una muestra"
    assert servicio.progreso_actual is None


@pytest.mark.parametrize("pedido", [{"dry_run": True}, {"interaction_mode": "no-es-un-modo"}])
async def test_lo_que_no_es_una_corrida_no_descarta_la_muestra_de_la_que_esta_en_curso(
    pedido: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """El preview (``dry_run``) y un modo desconocido no corren nada: la muestra de otra corrida en curso no es suya."""
    servicio = _servicio(_bus())
    monkeypatch.setattr(servicio, "_preview", AsyncMock(return_value={"success": True, "message": ""}))
    await servicio._al_progresar(_muestra())  # noqa: SLF001

    await servicio.execute(**pedido)  # type: ignore[arg-type]

    assert servicio.progreso_actual is not None


def _arbol_del_servicio() -> ast.Module:
    return ast.parse(pathlib.Path(modulo_servicio.__file__).read_text(encoding="utf-8"))


def _funciones_con_su_clase(arbol: ast.Module) -> list[tuple[str, ast.AST]]:
    encontradas: list[tuple[str, ast.AST]] = []

    def _recorrer(nodo: ast.AST, prefijo: str) -> None:
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, ast.ClassDef):
                _recorrer(hijo, f"{prefijo}{hijo.name}.")
            elif isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                encontradas.append((f"{prefijo}{hijo.name}", hijo))
                _recorrer(hijo, f"{prefijo}{hijo.name}.")

    _recorrer(arbol, "")
    return encontradas


def test_execute_es_el_unico_camino_que_corre_el_pipeline_y_lleva_el_descarte_al_salir() -> None:
    """Si otro método corriera el pipeline, tendría una salida sin descarte: se enumera por el LLAMADOR del runner."""
    funciones = _funciones_con_su_clase(_arbol_del_servicio())
    corren = {
        nombre
        for nombre, nodo in funciones
        if any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run_full_pipeline"
            for n in ast.walk(nodo)
        )
    }
    assert corren == {"DynDOLODPipelineService.execute"}
    (execute,) = [nodo for nombre, nodo in funciones if nombre == "DynDOLODPipelineService.execute"]
    decoradores = [ast.unparse(d) for d in execute.decorator_list]  # type: ignore[attr-defined]
    assert decoradores == ["_descarta_el_progreso_al_salir"]


def test_quien_escribe_la_muestra_de_progreso_esta_enumerado() -> None:
    """Un escritor nuevo de ``_progreso_actual`` rompe esto hasta decidir si participa del ciclo de vida de la corrida."""

    def _propios(nodo: ast.AST) -> list[ast.AST]:
        """Los nodos de ``nodo`` SIN entrar a las funciones anidadas: cada escritura se atribuye a la más interna."""
        propios: list[ast.AST] = []
        pendientes = list(ast.iter_child_nodes(nodo))
        while pendientes:
            n = pendientes.pop()
            propios.append(n)
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pendientes.extend(ast.iter_child_nodes(n))
        return propios

    escritores: set[str] = set()
    for nombre, nodo in _funciones_con_su_clase(_arbol_del_servicio()):
        for n in _propios(nodo):
            objetivos = []
            if isinstance(n, ast.Assign):
                objetivos = n.targets
            elif isinstance(n, ast.AnnAssign):
                objetivos = [n.target]
            if any(isinstance(t, ast.Attribute) and t.attr == "_progreso_actual" for t in objetivos):
                escritores.add(nombre)
    assert escritores == {
        "DynDOLODPipelineService.__init__",
        "DynDOLODPipelineService.execute",  # el descarte síncrono al arrancar (H-11)
        "DynDOLODPipelineService._al_progresar",
        "DynDOLODPipelineService._publish_started",
        "DynDOLODPipelineService._publish_completed",
        "_descarta_el_progreso_al_salir.envuelto",  # el descarte en TODA salida (este mecanismo)
    }


# ---------------------------------------------------------------------------
# El cableado: sin esto nada de lo anterior llega a producción
# ---------------------------------------------------------------------------


def test_el_unico_sitio_que_construye_el_runner_lo_cablea_con_el_callback_del_servicio() -> None:
    arbol = ast.parse(pathlib.Path(modulo_servicio.__file__).read_text(encoding="utf-8"))
    construcciones = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "DynDOLODRunner"
    ]

    assert len(construcciones) == 1, "cambió la familia de sitios que construyen el runner: decidir su cableado"
    cableado = {
        k.arg: ast.unparse(k.value) for k in construcciones[0].keywords if k.arg is not None and k.arg == "on_progress"
    }
    assert cableado == {"on_progress": "self._al_progresar"}
