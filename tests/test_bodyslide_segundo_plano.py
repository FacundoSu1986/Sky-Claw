"""PR 2c-2a: ``run_bodyslide`` corre como trabajo en segundo plano (R-8).

El router LLM corta cada round de tools a ``DEFAULT_TOOL_ROUND_TIMEOUT`` (120 s)
**cancelando** la llamada. Desde 2c-1, cancelar con el proceso lanzado deja el
journal de la config en ``LANZADO`` (HOLD sin restaurar). Un build largo
corriendo dentro del round terminaba entonces SIEMPRE en HOLD.

Propiedad: *el round del router nunca contiene la corrida*. La tool responde
con un ``trabajo_id`` antes de que la corrida termine, la corrida vive en una
task del gestor (una a la vez) y el aviso de fin viaja por ``notify_operator``.
El apagado (``cerrar``) cancela la task viva: eso sigue la regla de 2c-1 (HOLD
si el proceso estaba lanzado), nunca una restauración a ciegas.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.bodyslide_tools import (
    GestorDeTrabajosDeBodySlide,
    bodyslide_status,
    iniciar_run_bodyslide,
)
from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner

_RAIZ = pathlib.Path(__file__).resolve().parents[1]


class _Avisos:
    """Doble de ``HITLGuard.notify_operator``: registra cada aviso."""

    def __init__(self) -> None:
        self.mensajes: list[str] = []

    async def __call__(self, mensaje: str) -> None:
        self.mensajes.append(mensaje)


def _ejecutar_bloqueado() -> tuple[Callable[[], Awaitable[str]], asyncio.Event, asyncio.Event, list[str]]:
    """Una corrida que no termina hasta que el test la suelta."""
    entro = asyncio.Event()
    soltar = asyncio.Event()
    eventos: list[str] = []

    async def _ejecutar() -> str:
        entro.set()
        try:
            await soltar.wait()
        except asyncio.CancelledError:
            eventos.append("cancelada")
            raise
        eventos.append("terminada")
        return json.dumps({"success": True, "message": ""})

    return _ejecutar, entro, soltar, eventos


async def _terminar(gestor: GestorDeTrabajosDeBodySlide) -> None:
    trabajo = gestor.actual
    assert trabajo is not None and trabajo.tarea is not None
    await asyncio.wait_for(trabajo.tarea, timeout=5)


# ---------------------------------------------------------------------------
# El gestor
# ---------------------------------------------------------------------------


async def test_iniciar_responde_con_trabajo_id_antes_de_que_termine_la_corrida() -> None:
    # Arrange
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())
    ejecutar, entro, soltar, eventos = _ejecutar_bloqueado()

    # Act
    respuesta = gestor.iniciar(ejecutar, parametros={"group": "CBBE"})
    await asyncio.wait_for(entro.wait(), timeout=5)

    # Assert: la corrida está viva y la respuesta ya volvió.
    assert respuesta["success"] is True
    assert respuesta["message"] == ""
    assert respuesta["trabajo_id"].startswith("bodyslide-")
    assert respuesta["estado"] == "en_curso"
    assert eventos == []
    soltar.set()
    await _terminar(gestor)


async def test_una_segunda_corrida_con_la_primera_viva_se_rechaza_sin_lanzar() -> None:
    # Arrange
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())
    ejecutar, entro, soltar, _ = _ejecutar_bloqueado()
    primera = gestor.iniciar(ejecutar, parametros={"group": "CBBE"})
    await asyncio.wait_for(entro.wait(), timeout=5)
    segunda_ejecutar = AsyncMock(return_value=json.dumps({"success": True, "message": ""}))

    # Act
    segunda = gestor.iniciar(segunda_ejecutar, parametros={"group": "3BA"})

    # Assert
    assert segunda["success"] is False
    assert segunda["trabajo_id"] == primera["trabajo_id"]
    assert "en curso" in segunda["message"]
    segunda_ejecutar.assert_not_called()
    soltar.set()
    await _terminar(gestor)


@pytest.mark.parametrize(
    ("resultado", "estado", "marca"),
    [
        ({"success": True, "message": ""}, "exitoso", "✅"),
        ({"success": False, "message": "CAPABILITY_UNKNOWN: -q sin verificar"}, "fallido", "❌"),
        ({"error": "BodySlide requiere protección; faltan: lock_manager"}, "fallido", "❌"),
    ],
)
async def test_al_terminar_registra_el_resultado_y_avisa_una_vez(
    resultado: dict[str, Any], estado: str, marca: str
) -> None:
    # Arrange
    avisos = _Avisos()
    gestor = GestorDeTrabajosDeBodySlide(notificar=avisos)

    # Act
    gestor.iniciar(AsyncMock(return_value=json.dumps(resultado)), parametros={"group": "CBBE"})
    await _terminar(gestor)

    # Assert
    trabajo = gestor.actual
    assert trabajo is not None
    assert trabajo.estado == estado
    assert trabajo.resultado == resultado
    assert len(avisos.mensajes) == 1
    assert avisos.mensajes[0].startswith(marca)
    assert "CBBE" in avisos.mensajes[0]
    if estado == "fallido":
        detalle = resultado.get("message") or resultado.get("error")
        assert str(detalle) in avisos.mensajes[0]


async def test_un_resultado_que_no_es_json_es_fallido_no_exitoso() -> None:
    avisos = _Avisos()
    gestor = GestorDeTrabajosDeBodySlide(notificar=avisos)

    gestor.iniciar(AsyncMock(return_value="no es json"), parametros={"group": "CBBE"})
    await _terminar(gestor)

    assert gestor.actual is not None and gestor.actual.estado == "fallido"
    assert avisos.mensajes[0].startswith("❌")


async def test_una_excepcion_de_la_corrida_es_fallida_y_se_avisa() -> None:
    avisos = _Avisos()
    gestor = GestorDeTrabajosDeBodySlide(notificar=avisos)

    gestor.iniciar(AsyncMock(side_effect=RuntimeError("boom")), parametros={"group": "CBBE"})
    await _terminar(gestor)

    assert gestor.actual is not None and gestor.actual.estado == "fallido"
    assert "boom" in avisos.mensajes[0]


async def test_un_aviso_que_falla_no_cambia_el_resultado() -> None:
    gestor = GestorDeTrabajosDeBodySlide(notificar=AsyncMock(side_effect=ConnectionError("telegram caído")))

    gestor.iniciar(AsyncMock(return_value=json.dumps({"success": True, "message": ""})), parametros={})
    await _terminar(gestor)

    assert gestor.actual is not None and gestor.actual.estado == "exitoso"


async def test_terminada_una_corrida_se_puede_lanzar_otra() -> None:
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())
    gestor.iniciar(AsyncMock(return_value=json.dumps({"success": True, "message": ""})), parametros={})
    await _terminar(gestor)

    segunda = gestor.iniciar(AsyncMock(return_value=json.dumps({"success": True, "message": ""})), parametros={})

    assert segunda["success"] is True
    await _terminar(gestor)


async def test_cerrar_cancela_la_corrida_viva_y_avisa_la_cancelacion() -> None:
    # Arrange
    avisos = _Avisos()
    gestor = GestorDeTrabajosDeBodySlide(notificar=avisos)
    ejecutar, entro, _soltar, eventos = _ejecutar_bloqueado()
    gestor.iniciar(ejecutar, parametros={"group": "CBBE"})
    await asyncio.wait_for(entro.wait(), timeout=5)

    # Act
    await asyncio.wait_for(gestor.cerrar(), timeout=5)

    # Assert: la cancelación llegó a la corrida (el runner decide HOLD o restaurar).
    assert eventos == ["cancelada"]
    assert gestor.actual is not None and gestor.actual.estado == "cancelado"
    assert len(avisos.mensajes) == 1
    assert avisos.mensajes[0].startswith("⏹️")
    assert gestor.en_curso() is None


async def test_cerrar_sin_trabajos_no_hace_nada() -> None:
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())

    await asyncio.wait_for(gestor.cerrar(), timeout=5)

    assert gestor.actual is None


async def test_cerrar_cancela_una_tarea_que_no_llego_a_arrancar() -> None:
    """Una task cancelada antes de su primer paso no corre su handler: ``cerrar`` la cierra igual."""
    avisos = _Avisos()
    gestor = GestorDeTrabajosDeBodySlide(notificar=avisos)
    ejecutar, _entro, _soltar, eventos = _ejecutar_bloqueado()
    gestor.iniciar(ejecutar, parametros={"group": "CBBE"})

    # Sin ``wait_for``: envolver ``cerrar`` en otra task le daría a la corrida su primer paso.
    await gestor.cerrar()

    assert eventos == []
    assert gestor.actual is not None and gestor.actual.estado == "cancelado"
    assert len(avisos.mensajes) == 1


# ---------------------------------------------------------------------------
# La tool
# ---------------------------------------------------------------------------


async def test_sin_gestor_la_tool_falla_cerrada_sin_correr() -> None:
    runner = MagicMock(spec=BodySlideRunner)
    runner.run_batch = AsyncMock()

    salida = json.loads(
        await iniciar_run_bodyslide(
            None,
            runner,
            group="CBBE",
            output_path="meshes",
            preset=None,
            build_morphs=True,
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
        )
    )

    assert salida["success"] is False
    assert "segundo plano" in salida["message"]
    runner.run_batch.assert_not_called()


async def test_sin_runner_responde_no_configurado_sin_lanzar_trabajo() -> None:
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())

    salida = json.loads(
        await iniciar_run_bodyslide(
            gestor,
            None,
            group="CBBE",
            output_path="meshes",
            preset=None,
            build_morphs=True,
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
        )
    )

    assert "not configured" in salida["error"]
    assert gestor.actual is None


def _registry(tmp_path: pathlib.Path, runner: Any, gestor: GestorDeTrabajosDeBodySlide | None) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
        loot_exe=None,
        bodyslide_runner=runner,
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        bodyslide_jobs=gestor,
    )


async def test_el_registry_lanza_la_corrida_en_segundo_plano_con_los_parametros_validados(
    tmp_path: pathlib.Path,
) -> None:
    from sky_claw.local.tools.output_targets import bodyslide_output_target

    # Arrange: el runner no termina hasta que el test lo suelta.
    game = tmp_path / "game"
    entro = asyncio.Event()
    soltar = asyncio.Event()

    async def _run_batch(*_args: Any, **_kwargs: Any) -> MagicMock:
        entro.set()
        await soltar.wait()
        return MagicMock(success=True, message="", return_code=0, stdout="", stderr="", duration_seconds=1.0)

    runner = MagicMock(spec=BodySlideRunner)
    runner.config = BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=game)
    runner.run_batch = AsyncMock(side_effect=_run_batch)
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())
    reg = _registry(tmp_path, runner, gestor)
    transaction = MagicMock()
    transaction.lease_lost = False
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=None)

    with patch("sky_claw.app.db.locks.SnapshotTransactionLock", return_value=transaction):
        # Act
        respuesta = json.loads(
            await reg.execute("run_bodyslide", {"group": "3BA", "preset": "Zeroed Sliders", "build_morphs": False})
        )
        await asyncio.wait_for(entro.wait(), timeout=5)

        # Assert: la tool ya respondió y la corrida sigue viva.
        assert respuesta["success"] is True
        assert respuesta["trabajo_id"].startswith("bodyslide-")
        assert gestor.en_curso() is not None
        soltar.set()
        await _terminar(gestor)

    esperado = bodyslide_output_target(game=game, group="3BA")
    runner.run_batch.assert_awaited_once_with("3BA", str(esperado), preset="Zeroed Sliders", build_morphs=False)
    assert gestor.actual is not None and gestor.actual.parametros == {
        "group": "3BA",
        "preset": "Zeroed Sliders",
        "build_morphs": False,
    }


def test_el_descriptor_anuncia_el_segundo_plano(tmp_path: pathlib.Path) -> None:
    reg = _registry(tmp_path, None, GestorDeTrabajosDeBodySlide(notificar=_Avisos()))

    assert "background" in reg.tools["run_bodyslide"].description


# ---------------------------------------------------------------------------
# Cableado del AppContext (único constructor productivo del registry)
# ---------------------------------------------------------------------------


def _llamadas(arbol: ast.AST, nombre: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == nombre)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre)
        )
    ]


def test_todo_registry_productivo_recibe_el_gestor_y_su_cierre_esta_registrado() -> None:
    """Enumera los constructores productivos de ``AsyncToolRegistry``: todos reciben ``bodyslide_jobs``.

    Sin el gestor la tool falla cerrada; sin el cierre registrado, el apagado dejaría la corrida
    huérfana en vez de cancelarla (y llevar el journal a HOLD).
    """
    constructores: dict[str, list[ast.Call]] = {}
    for ruta in sorted((_RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        llamadas = _llamadas(arbol, "AsyncToolRegistry")
        if llamadas:
            constructores[ruta.relative_to(_RAIZ).as_posix()] = llamadas

    assert set(constructores) == {"sky_claw/app_context.py"}
    for llamadas in constructores.values():
        for llamada in llamadas:
            assert "bodyslide_jobs" in {k.arg for k in llamada.keywords}

    arbol = ast.parse((_RAIZ / "sky_claw" / "app_context.py").read_text(encoding="utf-8"))
    assert _llamadas(arbol, "GestorDeTrabajosDeBodySlide"), "el AppContext no construye el gestor"
    cierres = [
        llamada
        for llamada in _llamadas(arbol, "_push_startup_cleanup")
        if llamada.args
        and isinstance(llamada.args[0], ast.Attribute)
        and llamada.args[0].attr == "cerrar"
        and isinstance(llamada.args[0].value, ast.Name)
        and llamada.args[0].value.id == "bodyslide_jobs"
    ]
    assert len(cierres) == 1, "el cierre del gestor de BodySlide no está registrado en el cleanup"


# ---------------------------------------------------------------------------
# Review Codex #1024 (P2): el resultado es consultable, no solo un aviso
# ---------------------------------------------------------------------------
#
# ``notify_operator`` solo entrega por Telegram: desde ``/api/chat`` sin ese canal,
# el aviso de fin se descartaba y el resultado de la corrida —incluido el de una
# config que quedó en HOLD— no le llegaba a nadie. Mismo cierre que la etapa 9
# (``lods_status``): una tool de solo lectura con el trabajo en curso o el último.


async def test_estado_sin_trabajos_lo_dice_sin_fallar() -> None:
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())

    estado = json.loads(await bodyslide_status(gestor))

    assert estado["success"] is True
    assert estado["trabajo"] is None


async def test_estado_sin_gestor_falla_cerrada() -> None:
    estado = json.loads(await bodyslide_status(None))

    assert estado["success"] is False
    assert estado["message"]


async def test_estado_muestra_la_corrida_viva() -> None:
    gestor = GestorDeTrabajosDeBodySlide(notificar=_Avisos())
    ejecutar, entro, soltar, _ = _ejecutar_bloqueado()
    respuesta = gestor.iniciar(ejecutar, parametros={"group": "CBBE"})
    await asyncio.wait_for(entro.wait(), timeout=5)

    estado = json.loads(await bodyslide_status(gestor))

    assert estado["trabajo"]["trabajo_id"] == respuesta["trabajo_id"]
    assert estado["trabajo"]["estado"] == "en_curso"
    assert "resultado" not in estado["trabajo"]
    soltar.set()
    await _terminar(gestor)


async def test_estado_devuelve_el_resultado_de_un_fallo_sin_canal_de_avisos() -> None:
    """Sin ``notificar`` (sin Telegram) el resultado sigue siendo recuperable."""
    gestor = GestorDeTrabajosDeBodySlide(notificar=None)
    resultado = {"success": False, "message": "CAPABILITY_UNKNOWN: -q sin verificar", "return_code": -1}
    gestor.iniciar(AsyncMock(return_value=json.dumps(resultado)), parametros={"group": "CBBE"})
    await _terminar(gestor)

    estado = json.loads(await bodyslide_status(gestor))

    assert estado["trabajo"]["estado"] == "fallido"
    assert estado["trabajo"]["resultado"] == resultado
    assert estado["trabajo"]["parametros"] == {"group": "CBBE"}


def test_el_registry_expone_el_estado_y_run_bodyslide_lo_menciona(tmp_path: pathlib.Path) -> None:
    reg = _registry(tmp_path, None, GestorDeTrabajosDeBodySlide(notificar=_Avisos()))

    assert "bodyslide_status" in reg.tools
    assert reg.tools["bodyslide_status"].params_model is None
    assert "bodyslide_status" in reg.tools["run_bodyslide"].description
