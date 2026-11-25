"""PR 2c-2b: el runner recibe un lanzador inyectable y solo él decide la terminalidad.

Hasta 2c-1 el runner llamaba a ``run_capture`` directo. 2c-2b agrega el backend brokered
(USVFS), que lanza por otro camino y tiene otra forma de probar que el proceso terminó:
el fence ``worker_exit`` del broker. Para que el journal, la siembra, la restauración y el
veredicto sigan siendo UNA sola implementación, el spawn pasa a ser un lanzador inyectable.

Propiedad: *el lanzador reporta; el runner decide*. Contrato de todo lanzador:

- devolver la salida, ``TimeoutError`` o cualquier otro error ⇒ el proceso terminó
  (``TERMINAL`` y restauración);
- ``TerminalidadDesconocidaError`` ⇒ el proceso pudo seguir vivo: el journal queda en
  ``LANZADO`` y la config NO se restaura (``HOLD_TERMINALITY_UNKNOWN``);
- la cancelación sigue la regla de 2c-1: HOLD sin restaurar.

La tabla se enumera desenlace por desenlace y se congela por igualdad literal: un
desenlace nuevo rompe el test hasta que alguien decida qué prueba.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from typing import Any

import pytest

from sky_claw.local.tools import bodyslide_lanzador
from sky_claw.local.tools.bodyslide_headless import ManifiestoArtefactos
from sky_claw.local.tools.bodyslide_journal import AlmacenDeJournal, identidad_de_instancia
from sky_claw.local.tools.bodyslide_lanzador import (
    PedidoDeLanzamiento,
    SalidaDelLanzamiento,
    TerminalidadDesconocidaError,
)
from sky_claw.local.tools.bodyslide_runner import (
    BodySlideConfig,
    BodySlideExecutionError,
    BodySlideRunner,
    BodySlideTimeoutError,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
_CONFIG = "<Config>\n  <TargetGame>4</TargetGame>\n  <ProjectPath>/mis/proyectos</ProjectPath>\n</Config>\n"
_APP = "<BodySlideConfig><SelectedPreset>Viejo</SelectedPreset></BodySlideConfig>\n"
_MANIFIESTO = ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture")


class _Lanzador:
    """Lanzador de prueba: construye la salida y termina con el desenlace pedido."""

    def __init__(self, desenlace: str, destino: pathlib.Path, exe: pathlib.Path) -> None:
        self.desenlace = desenlace
        self.destino = destino
        self.exe = exe
        self.pedidos: list[PedidoDeLanzamiento] = []

    async def lanzar(self, pedido: PedidoDeLanzamiento) -> SalidaDelLanzamiento:
        self.pedidos.append(pedido)
        # La corrida tocó la config antes de terminar (o de perderse el rastro).
        (self.exe.parent / "BodySlide.xml").write_text(
            "<BodySlideConfig><SelectedPreset>Nuevo</SelectedPreset></BodySlideConfig>\n", encoding="utf-8"
        )
        if self.desenlace == "retorno":
            (self.destino / "cuerpo.nif").write_bytes(b"\x00")
            (self.exe.parent / "Log_BS.txt").write_text("[#Log]\nAll sets processed successfully!\n", encoding="utf-8")
            return SalidaDelLanzamiento(stdout=b"", stderr=b"", return_code=0)
        if self.desenlace == "timeout":
            raise TimeoutError
        if self.desenlace == "error":
            raise RuntimeError("el lanzador falló")
        if self.desenlace == "terminalidad_desconocida":
            raise TerminalidadDesconocidaError("el bridge se perdió sin worker_exit")
        if self.desenlace == "cancelacion":
            raise asyncio.CancelledError
        raise AssertionError(self.desenlace)


def _runner(tmp_path: pathlib.Path, lanzador: Any) -> BodySlideRunner:
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    (exe.parent / "Config.xml").write_text(_CONFIG, encoding="utf-8")
    (exe.parent / "BodySlide.xml").write_text(_APP, encoding="utf-8")
    return BodySlideRunner(
        BodySlideConfig(
            bodyslide_exe=exe,
            game_path=tmp_path / "game",
            quiet_verified=True,
            expected_target_game=4,
            almacen_de_journal=AlmacenDeJournal(tmp_path / "state" / "bodyslide_journal", endurecer=lambda _p: None),
            lanzador=lanzador,
        )
    )


def _destino(tmp_path: pathlib.Path) -> pathlib.Path:
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    return destino


async def _observar(tmp_path: pathlib.Path, desenlace: str) -> tuple[str, str, bool]:
    """(resultado o excepción, estado del journal en disco, ¿config restaurada?)."""
    destino = _destino(tmp_path)
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    runner = _runner(tmp_path, _Lanzador(desenlace, destino, exe))
    almacen = runner.config.almacen_de_journal
    assert almacen is not None
    try:
        resultado = await runner.run_batch(
            "CBBE", str(destino), manifest=_MANIFIESTO, preset="Zeroed", preset_verified=True
        )
        salida = resultado.estado
    except asyncio.CancelledError:
        salida = "CancelledError"
    except BodySlideTimeoutError:
        salida = "BodySlideTimeoutError"
    except BodySlideExecutionError:
        salida = "BodySlideExecutionError"
    ruta = almacen.ruta_de(identidad_de_instancia(exe.parent))
    estado = json.loads(ruta.read_text(encoding="utf-8"))["estado"] if ruta.exists() else "CERRADO"
    restaurada = (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG and (
        exe.parent / "BodySlide.xml"
    ).read_text(encoding="utf-8") == _APP
    return salida, estado, restaurada


#: Tabla de terminalidad congelada: desenlace del lanzador → (salida, journal, ¿restaurada?).
TABLA_DE_TERMINALIDAD: dict[str, tuple[str, str, bool]] = {
    "retorno": ("SUCCESS", "CERRADO", True),
    "timeout": ("BodySlideTimeoutError", "CERRADO", True),
    "error": ("BodySlideExecutionError", "CERRADO", True),
    "terminalidad_desconocida": ("HOLD_TERMINALITY_UNKNOWN", "LANZADO", False),
    "cancelacion": ("CancelledError", "LANZADO", False),
}


@pytest.mark.parametrize("desenlace", sorted(TABLA_DE_TERMINALIDAD))
async def test_la_tabla_de_terminalidad(tmp_path: pathlib.Path, desenlace: str) -> None:
    # Act
    observado = await _observar(tmp_path, desenlace)

    # Assert
    assert observado == TABLA_DE_TERMINALIDAD[desenlace]


def test_la_tabla_enumera_todos_los_desenlaces_del_contrato() -> None:
    """Un desenlace nuevo del contrato rompe esto hasta tener su fila en la tabla."""
    assert set(TABLA_DE_TERMINALIDAD) == {"retorno", "timeout", "error", "terminalidad_desconocida", "cancelacion"}
    assert issubclass(TerminalidadDesconocidaError, Exception)
    assert not issubclass(TerminalidadDesconocidaError, TimeoutError), "un timeout SÍ prueba terminalidad"


async def test_el_pedido_lleva_los_campos_del_contrato_y_el_argv_del_runner(tmp_path: pathlib.Path) -> None:
    """El backend brokered reconstruye el argv en el worker: los campos del pedido tienen que alcanzar."""
    destino = _destino(tmp_path)
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    lanzador = _Lanzador("retorno", destino, exe)
    runner = _runner(tmp_path, lanzador)

    await runner.run_batch("CBBE", str(destino), manifest=_MANIFIESTO, preset="Zeroed", preset_verified=True)

    (pedido,) = lanzador.pedidos
    assert pedido.exe == exe
    assert pedido.group == "CBBE"
    assert pedido.target == str(destino)
    assert pedido.preset == "Zeroed"
    assert pedido.preset_verified is True
    assert pedido.build_morphs is True
    assert pedido.quiet_verified is True
    assert pedido.cwd == tmp_path / "game"
    assert pedido.timeout == runner.config.timeout_seconds
    assert pedido.argv[0] == str(exe)
    assert "-gbuild" in pedido.argv and "CBBE" in pedido.argv


def test_sin_lanzador_configurado_el_runner_usa_el_standalone() -> None:
    """El default no cambia: ``run_capture`` con Job, como en 2c-1."""
    config = BodySlideConfig(bodyslide_exe=pathlib.Path("BodySlide.exe"), game_path=pathlib.Path("game"))
    assert config.lanzador is None


def test_run_capture_solo_se_llama_desde_el_lanzador_standalone() -> None:
    """El runner no spawnea por fuera del lanzador: si lo hiciera, ese camino saltearía la tabla."""
    arbol = ast.parse((RAIZ / "sky_claw" / "local" / "tools" / "bodyslide_runner.py").read_text(encoding="utf-8"))
    duenios: set[str] = set()
    for clase_o_funcion in ast.walk(arbol):
        if not isinstance(clase_o_funcion, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for nodo in ast.walk(clase_o_funcion):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "run_capture":
                duenios.add(clase_o_funcion.name)
    assert duenios == {"LanzadorStandalone", "lanzar"}


def test_el_modulo_del_contrato_no_lanza_procesos() -> None:
    fuente = (RAIZ / "sky_claw" / "local" / "tools" / "bodyslide_lanzador.py").read_text(encoding="utf-8")
    assert "run_capture" not in fuente
    assert "subprocess" not in fuente
    assert bodyslide_lanzador.__name__.endswith("bodyslide_lanzador")


async def test_la_cancelacion_con_el_proceso_lanzado_lleva_el_veto_de_rollback(tmp_path: pathlib.Path) -> None:
    """Hermano del review Codex #1025: la cancelación tampoco prueba terminalidad.

    El journal ya quedaba en ``LANZADO``, pero la ``CancelledError`` atravesaba el
    ``DirectoryRollback`` del servicio sin veto y restauraba la salida bajo un proceso
    posiblemente vivo. La cancelación que sale del spawn lleva el veto.
    """
    from sky_claw.app.db.rollback_veto import exception_forbids_rollback

    destino = _destino(tmp_path)
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    runner = _runner(tmp_path, _Lanzador("cancelacion", destino, exe))

    with pytest.raises(asyncio.CancelledError) as info:
        await runner.run_batch("CBBE", str(destino), manifest=_MANIFIESTO, preset="Zeroed", preset_verified=True)

    assert exception_forbids_rollback(info.value)


async def test_una_cancelacion_antes_del_spawn_no_lleva_veto(tmp_path: pathlib.Path) -> None:
    """Contracara: si el spawn nunca se pidió, la terminalidad está probada y la salida se puede restaurar."""
    from sky_claw.app.db.rollback_veto import exception_forbids_rollback

    destino = _destino(tmp_path)
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    runner = _runner(tmp_path, _Lanzador("retorno", destino, exe))

    async def _siembra_cancelada(self: Any) -> None:
        raise asyncio.CancelledError

    from unittest.mock import patch

    with (
        patch("sky_claw.local.tools.bodyslide_journal.SiembraConPlazo.ejecutar", _siembra_cancelada),
        pytest.raises(asyncio.CancelledError) as info,
    ):
        await runner.run_batch("CBBE", str(destino), manifest=_MANIFIESTO, preset="Zeroed", preset_verified=True)

    assert not exception_forbids_rollback(info.value)
