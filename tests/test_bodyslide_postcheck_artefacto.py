"""El exit code de BodySlide no reporta el resultado del build — el artefacto sí.

**El mecanismo.** ``BodySlideApp::GroupBuild`` captura el resultado del build
(``int ret = BuildListBodies(...)``, línea 4741) y lo usa SÓLO para elegir un
mensaje de log o abrir un diálogo; después sale por ``sliderView->Close(true)``
(línea 4763). No hay ``SetExitCode`` ni override de ``OnRun`` en todo el
ejecutable, así que **el proceso termina en 0 aunque ``ret == 3``** ("the
following sets failed", con ``failedOutfits`` poblado).

Eso deja dos falsos verdes que ``success = return_code == 0`` no distingue:

1. **Build parcial** — algunos outfits fallaron, otros no. El target queda
   NO vacío, así que ``DirectoryRollback._sin_regenerar`` lo cuenta como
   regenerado y descarta el backup. Salida parcial conservada y reportada como
   éxito.
2. **Build vacío** — bajo MO2 sin USVFS, BodySlide no ve ningún ``SliderSet``
   (viven dispersos en ``mods/*/CalienteTools/BodySlide/``), así que
   ``GroupBuild`` itera una lista vacía y sale 0 sin construir nada.
   ``DirectoryRollback`` protege el DATO (restaura el contenido previo al ver el
   target vacío), pero la tool igual reporta ``success=True``.

**Por qué importa más de lo que parece.** ``run_bodyslide_batch`` ya tiene el
puente fallo→excepción correcto (``_BodySlideRunFallidoError`` →
``DirectoryRollback`` restaura), pero se dispara con ``result.success == False``.
Mientras el éxito se derive del exit code, ese puente es **inalcanzable** para el
modo de fallo más común del ejecutable. El post-check del artefacto es lo que lo
vuelve alcanzable.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner


def _runner(tmp_path: pathlib.Path) -> BodySlideRunner:
    return BodySlideRunner(
        BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=tmp_path / "game"),
    )


def _captura(return_code: int = 0) -> AsyncMock:
    """``run_capture`` que simula un proceso GUI: stdout/stderr vacíos.

    No es una simplificación del doble: ``BodySlide.exe`` se linkea
    ``/SUBSYSTEM:Windows`` (``BodySlide.vcxproj``), así que **nunca** escribe a
    stdout/stderr. Un doble que devolviera texto ahí estaría probando un
    ejecutable que no existe.
    """
    return AsyncMock(return_value=(b"", b"", return_code))


async def test_exit_cero_sin_mallas_no_es_exito(tmp_path: pathlib.Path) -> None:
    """Exit 0 con el target vacío ⇒ ``success=False``.

    Es el caso de MO2 sin USVFS: cero ``SliderSets`` visibles, cero outfits
    construidos, exit 0. Hoy se reporta como éxito y las etapas siguientes del
    pipeline consumen mallas que nunca se generaron.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.return_code == 0
    assert result.success is False
    assert result.artifacts_built == 0


async def test_exit_cero_sin_el_directorio_tampoco_es_exito(tmp_path: pathlib.Path) -> None:
    """El target puede no existir siquiera — no debe explotar ni reportar éxito."""
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"  # nunca creado

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is False
    assert result.artifacts_built == 0


async def test_mensaje_nombra_la_causa_en_vez_de_quedar_vacio(tmp_path: pathlib.Path) -> None:
    """El fallo tiene que traer causa legible.

    ``_bodyslide_dict_de_resultado`` deriva ``message`` de ``stderr or stdout``, y
    para un binario ``/SUBSYSTEM:Windows`` los dos son SIEMPRE vacíos: un fallo
    llegaba al LLM como ``success=False, message=""``. El runner es quien tiene
    que poblar la causa.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.message, "un fallo sin mensaje es un fallo sobre el que nadie puede actuar"
    assert str(destino) in result.message


@pytest.mark.parametrize("artefacto", ["cuerpo.nif", "morfos.tri"])
async def test_con_artefacto_es_exito_y_mensaje_vacio(tmp_path: pathlib.Path, artefacto: str) -> None:
    """Con mallas o morfos en el target, el run es exitoso y ``message`` va vacío.

    ``message`` vacío en éxito es el contrato canónico del repo (AGENTS.md).
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / artefacto).write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is True
    assert result.artifacts_built == 1
    assert result.message == ""


async def test_cuenta_artefactos_en_subdirectorios(tmp_path: pathlib.Path) -> None:
    """BodySlide escribe bajo ``meshes/actors/...``: el conteo debe ser recursivo.

    ``BuildListBodies`` compone el destino como ``datapath + currentSet.GetOutputPath()``,
    así que las mallas aterrizan en subárboles, nunca sueltas en la raíz del target.
    Un conteo no recursivo daría cero en TODO build real.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    anidado = destino / "meshes" / "actors" / "character"
    anidado.mkdir(parents=True)
    (anidado / "femalebody_0.nif").write_bytes(b"\x00")
    (anidado / "femalebody_1.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is True
    assert result.artifacts_built == 2


async def test_exit_no_cero_sigue_siendo_fallo_aunque_haya_artefactos(tmp_path: pathlib.Path) -> None:
    """El post-check AGREGA una condición de fallo, no reemplaza la del exit code.

    Un exit non-zero con salida parcial en disco sigue siendo un fallo: es
    exactamente el escenario en que ``DirectoryRollback`` debe restaurar.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / "parcial.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(3)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is False
    assert result.return_code == 3


async def test_log_de_bodyslide_se_incorpora_al_mensaje(tmp_path: pathlib.Path) -> None:
    """``Log_BS.txt`` es el ÚNICO diagnóstico real que produce BodySlide.

    ``Log::Initialize`` (``src/utils/Log.cpp``) instala un ``wxLogStream`` sobre
    ``<dir del exe>/Log_BS.txt``; no hay salida por stdout/stderr. Si el runner no
    lo lee, el operador no tiene con qué diagnosticar un fallo.
    """
    runner = _runner(tmp_path)
    runner.config.bodyslide_exe.parent.mkdir(parents=True, exist_ok=True)
    (runner.config.bodyslide_exe.parent / "Log_BS.txt").write_text(
        "[#Log 2026-08-03]\nFailed to build 'Vestido': missing shape data\n",
        encoding="utf-8",
    )
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is False
    assert "missing shape data" in result.message
