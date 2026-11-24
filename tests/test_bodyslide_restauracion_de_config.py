"""Restore byte a byte de la config — PR 1, invariante transaccional (P0-1/P0-2).

Orden transaccional del runner (post-auditoría):

1. argv / capacidad — validación pura, sin tocar disco.
2. Snapshot del ORIGINAL (solo lectura) — ANTES de cualquier siembra de
   Config.xml.
3. Siembra — mutación de Config.xml.
4. Spawn — evidencia + veredicto.
5. Restore byte a byte del ORIGINAL — garantizado en todo caso (éxito, falla
   de seed, falla del spawn).

Si el snapshot se toma DESPUÉS de la siembra (como antes), el restore revierte
solo la versión sembrada y el Config.xml del usuario queda mutado con claves
que la herramienta escribió en su lugar — la siembra se vuelve no-verificable.
"""

from __future__ import annotations

import pathlib
import stat
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.bodyslide_headless import ManifiestoArtefactos
from sky_claw.local.tools.bodyslide_runner import (
    BodySlideConfig,
    BodySlideExecutionError,
    BodySlideRunner,
)

_CONFIG_VÁLIDA = "<Config>\n  <TargetGame>4</TargetGame>\n  <ProjectPath>/mis/proyectos</ProjectPath>\n</Config>\n"


def _runner(tmp_path: pathlib.Path, *, quiet: bool = True, expected_game: int | None = 4) -> BodySlideRunner:
    """Runner de prueba. El plan SIEMPRE declara el esperado (P1-5): por defecto
    4, igual que el TargetGame del Config.xml de la fixture."""
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    (exe.parent / "Config.xml").write_text(_CONFIG_VÁLIDA, encoding="utf-8")
    return BodySlideRunner(
        BodySlideConfig(
            bodyslide_exe=exe,
            game_path=tmp_path / "game",
            quiet_verified=quiet,
            expected_target_game=expected_game,
        ),
    )


def _destino(tmp_path: pathlib.Path) -> pathlib.Path:
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    return destino


def _effector_realista(destino: pathlib.Path, exe: pathlib.Path, archivos: list[str] | None = None):
    """Simula un proceso realista: crea .nif + muta BodySlide.xml + escribe log."""

    def _efecto(*_a, **_k):
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        for ruta in archivos or ["BodySlide.xml"]:
            (exe.parent / ruta).write_text(
                "<BodySlideConfig><SelectedPreset>Zeroed Sliders</SelectedPreset></BodySlideConfig>\n",
                encoding="utf-8",
            )
        (exe.parent / "Log_BS.txt").write_text(
            "[#Log 2026-11-22]\nAll sets processed successfully!\n", encoding="utf-8"
        )
        return (b"", b"", 0)

    return _efecto


# --------------------------------------------------------------------------- #
# P0-1: snapshot original ANTES de la siembra; restore original (post-spawn)
# --------------------------------------------------------------------------- #


async def test_successful_run_restores_original_config_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """El runner restaura Config.xml al ORIGINAL byte a byte — no al sembrado.

    La siembra es un efecto transitorio del spawn: si no se revierte, el
    Config.xml del usuario queda mutado con claves que Sky-Claw escribió, lo
    que rompe el aislamiento del build headless y oculta la config del usuario.
    """
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)

    with patch(
        "sky_claw.local.tools.bodyslide_runner.run_capture",
        AsyncMock(side_effect=_effector_realista(destino, exe)),
    ):
        result = await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert (exe.parent / "Config.xml").read_bytes() == original, (
        "Config.xml no debe quedar mutado con la siembra — el restore revierte al ORIGINAL"
    )
    assert result.success is True


async def test_spawn_failure_still_restores_original_config(tmp_path: pathlib.Path) -> None:
    """Si el spawn crashea, el snapshot se restaura igualmente."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)

    def _boom(*_a, **_k):
        raise OSError("boom del proceso")

    with (
        patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=_boom)),
        pytest.raises(BodySlideExecutionError),
    ):
        await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert (exe.parent / "Config.xml").read_bytes() == original


async def test_quiet_gate_failure_leaves_config_untouched(tmp_path: pathlib.Path) -> None:
    """Gate de capacidad fallido antes de mutar → Config.xml sin tocar."""
    runner = _runner(tmp_path, quiet=False)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)

    run_capture = AsyncMock(side_effect=_effector_realista(destino, exe))
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        result = await runner.run_batch("CBBE", str(destino))

    assert run_capture.await_count == 0
    assert result.estado == "CAPABILITY_UNKNOWN"
    assert (exe.parent / "Config.xml").read_bytes() == original, "el gate de capacidad no debe mutar la config"


async def test_argv_failure_leaves_config_untouched(tmp_path: pathlib.Path) -> None:
    """argv inválido antes de mutar → Config.xml sin tocar."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()

    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        result = await runner.run_batch("", str(_destino(tmp_path)))  # group vacío → ARGS_INVALID

    assert run_capture.await_count == 0
    assert result.estado == "ARGS_INVALID"
    assert (exe.parent / "Config.xml").read_bytes() == original


async def test_seed_failure_restores_original_config(tmp_path: pathlib.Path) -> None:
    """Seed abortado → Config.xml queda byte a byte igual que antes de la siembra.

    El seed llega a validar TargetGame antes de mutar; si el TargetGame no
    coincide con el juego objetivo el abort se hace antes de la escritura.
    """
    runner = _runner(tmp_path, expected_game=9)  # config tiene TargetGame=4
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()

    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        result = await runner.run_batch("CBBE", str(_destino(tmp_path)))

    assert run_capture.await_count == 0
    assert result.estado == "CONFIG_PRECONDITION_FAILED"
    assert (exe.parent / "Config.xml").read_bytes() == original, "el seed abortado no puede mutar la config"


async def test_successful_run_restores_bodyslide_and_buildselection(tmp_path: pathlib.Path) -> None:
    """El proceso muta BodySlide.xml/BuildSelection.xml → se restauran byte a byte."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    destino = _destino(tmp_path)

    (exe.parent / "BodySlide.xml").write_text(
        "<BodySlideConfig><SelectedPreset>Original</SelectedPreset></BodySlideConfig>\n", encoding="utf-8"
    )
    (exe.parent / "BuildSelection.xml").write_text("<BuildSelection></BuildSelection>\n", encoding="utf-8")
    bodyslide_original = (exe.parent / "BodySlide.xml").read_bytes()
    buildselection_original = (exe.parent / "BuildSelection.xml").read_bytes()

    with patch(
        "sky_claw.local.tools.bodyslide_runner.run_capture",
        AsyncMock(side_effect=_effector_realista(destino, exe)),
    ):
        result = await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert (exe.parent / "BodySlide.xml").read_bytes() == bodyslide_original
    assert (exe.parent / "BuildSelection.xml").read_bytes() == buildselection_original
    assert "BodySlide.xml" in result.config_restored
    assert result.success is True


async def test_process_creates_new_config_and_runner_deletes_it(tmp_path: pathlib.Path) -> None:
    """Si el proceso CREA un archivo de config nuevo, se quita al terminar."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    destino = _destino(tmp_path)

    def _efecto(*_a, **_k):
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        (exe.parent / "BodySlide.xml").write_text(
            "<BodySlideConfig><SelectedPreset>Zeroed Sliders</SelectedPreset></BodySlideConfig>\n", encoding="utf-8"
        )
        (exe.parent / "Log_BS.txt").write_text(
            "[#Log 2026-11-22]\nAll sets processed successfully!\n", encoding="utf-8"
        )
        return (b"", b"", 0)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=_efecto)):
        await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert not (exe.parent / "BodySlide.xml").exists()


# --------------------------------------------------------------------------- #
# P0-2: symlinks — el archivo externo enlazado se restaura byte a byte
# --------------------------------------------------------------------------- #


async def test_buildselection_enlazado_se_rechaza_antes_de_mutar(tmp_path: pathlib.Path) -> None:
    """Un BuildSelection.xml enlazado ⇒ CONFIG_PRECONDITION_FAILED, sin mutar ni spawnear.

    PR 2c-1 (decisión del propietario, 2026-11-24): el journal no registra ni restaura a
    través de enlaces, así que una config enlazada se rechaza antes de la primera
    mutación. Retira el soporte P0-2 de PR 1 (restaurar a través del enlace).
    """
    try:
        (tmp_path / "enlace_probe").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink no disponible")
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    config_original = (exe.parent / "Config.xml").read_bytes()

    externo = tmp_path / "otra" / "BuildSelection.xml"
    externo.parent.mkdir(parents=True)
    externo.write_text("<BuildSelection><zap>original</zap></BuildSelection>\n", encoding="utf-8")
    (exe.parent / "BuildSelection.xml").symlink_to(externo)

    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        result = await runner.run_batch(
            "CBBE",
            str(_destino(tmp_path)),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert result.estado == "CONFIG_PRECONDITION_FAILED"
    assert run_capture.await_count == 0, "configuración enlazada: no se spawnea"
    assert externo.read_text(encoding="utf-8") == "<BuildSelection><zap>original</zap></BuildSelection>\n"
    assert (exe.parent / "BuildSelection.xml").is_symlink(), "el enlace del usuario no se toca"
    assert (exe.parent / "Config.xml").read_bytes() == config_original


# --------------------------------------------------------------------------- #
# Config.xml ilegible — fail-closed antes del spawn
# --------------------------------------------------------------------------- #


async def test_config_unreadable_aborts_before_spawn(tmp_path: pathlib.Path) -> None:
    """Un Config.xml ilegible en cualquier punto del pipeline ⇒ NO se spawnea."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    destino = _destino(tmp_path)

    (exe.parent / "Config.xml").chmod(0o000)

    try:
        run_capture = AsyncMock(return_value=(b"", b"", 0))
        with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
            result = await runner.run_batch("CBBE", str(destino))
    finally:
        (exe.parent / "Config.xml").chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert run_capture.await_count == 0, "Config.xml ilegible: no se lanza el proceso"
    assert result.success is False
    assert result.estado == "CONFIG_PRECONDITION_FAILED"


# --------------------------------------------------------------------------- #
# P0-A — restauración ante excepciones anteriores al spawn
# --------------------------------------------------------------------------- #


async def test_previa_de_escaneo_os_error_restaura_original(tmp_path: pathlib.Path) -> None:
    """OSError durante _conjunto_de_archivos_previos → el ORIGINAL se restaura.

    El escaneo previo al spawn vive DESPUÉS de sembrar Config.xml y fuera del
    perímetro transaccional original — una excepción aquí dejaba la siembra
    persistente y el fallo reportado como éxito.
    """
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)

    with (
        patch(
            "sky_claw.local.tools.bodyslide_runner._conjunto_de_archivos_previos",
            side_effect=OSError("boom durante el escaneo"),
        ),
        pytest.raises(OSError),
    ):
        await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert (exe.parent / "Config.xml").read_bytes() == original, (
        "la siembra no puede persistir cuando el escaneo previo al spawn falla"
    )


async def test_previa_de_escaneo_cancelacion_restaura_original(tmp_path: pathlib.Path) -> None:
    """Cancelación durante _conjunto_de_archivos_previos → el ORIGINAL se restaura."""
    import asyncio

    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)

    with (
        patch(
            "sky_claw.local.tools.bodyslide_runner._conjunto_de_archivos_previos",
            side_effect=asyncio.CancelledError(),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert (exe.parent / "Config.xml").read_bytes() == original


async def test_pre_spawn_unexpected_exception_restores_and_propagates(tmp_path: pathlib.Path) -> None:
    """Cualquier excepción inesperada post-mutación restaura y se propaga (nunca éxito)."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)

    with (
        patch(
            "sky_claw.local.tools.bodyslide_runner.run_capture",
            AsyncMock(return_value=(b"", b"", 0)),
        ),
        patch(
            "sky_claw.local.tools.bodyslide_runner._contar_archivos_nuevos",
            side_effect=RuntimeError("unexpected"),
        ),
        pytest.raises(RuntimeError),
    ):
        await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert (exe.parent / "Config.xml").read_bytes() == original


# --------------------------------------------------------------------------- #
# P0-B — configuración enlazada: bloqueo fail-closed
# --------------------------------------------------------------------------- #


async def test_config_enlazada_no_se_siembra_ni_se_spawnea(tmp_path: pathlib.Path) -> None:
    """Config.xml enlazado ⇒ CONFIG_PRECONDITION_FAILED, sin mutar, sin spawn.

    P0-B: la restauración a través del enlace es insegura (retarget indetectable).
    El fail-closed bloquea la corrida por completo.
    """
    try:
        (tmp_path / "enlace_probe").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink no disponible")
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe

    externo = tmp_path / "otra" / "Config.xml"
    externo.parent.mkdir(parents=True)
    externo.write_text(
        "<Config>\n  <TargetGame>4</TargetGame>\n  <ProjectPath>/mis/proyectos</ProjectPath>\n</Config>\n",
        encoding="utf-8",
    )
    (exe.parent / "Config.xml").unlink()
    (exe.parent / "Config.xml").symlink_to(externo)
    original = externo.read_bytes()

    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        result = await runner.run_batch("CBBE", str(_destino(tmp_path)))

    assert run_capture.await_count == 0, "configuración enlazada: no se spawnea"
    assert result.estado == "CONFIG_PRECONDITION_FAILED"
    assert externo.read_bytes() == original, "el destino externo no puede mutarse"
    assert (exe.parent / "Config.xml").is_symlink(), "el enlace del usuario no puede removerse"


# --------------------------------------------------------------------------- #
# P0-B — re-target del enlace DURANTE el run: la restauración NO puede escribir
# a través de un enlace cuya identidad ya no coincide con el snapshot.
# --------------------------------------------------------------------------- #


async def test_buildselection_retargeteado_no_se_escribe_a_traves_del_enlace(tmp_path: pathlib.Path) -> None:
    """Un enlace que APARECE durante el run nunca recibe la restauración.

    PR 2c-1: la config de partida es regular (una enlazada se rechaza antes de mutar).
    Si durante la corrida ``BuildSelection.xml`` pasa a ser un enlace a B, el journal lo
    clasifica como contenido ajeno: ``TERMINAL`` se registra igual (review CodeRabbit #1021),
    nada se escribe a través del enlace, el resultado es ``CONCURRENT_MODIFICATION`` (nunca
    éxito) y B queda intacto.
    """
    try:
        (tmp_path / "enlace_probe").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink no disponible")
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    destino = _destino(tmp_path)

    externo_b = tmp_path / "otra_b" / "BuildSelection.xml"
    externo_b.parent.mkdir(parents=True)
    externo_b.write_text("<BuildSelection><zap>PRESERVADO-B</zap></BuildSelection>\n", encoding="utf-8")
    enlace = exe.parent / "BuildSelection.xml"

    def _efecto(*_a, **_k):
        enlace.symlink_to(externo_b)
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        (exe.parent / "Log_BS.txt").write_text(
            "[#Log 2026-11-22]\nAll sets processed successfully!\n", encoding="utf-8"
        )
        return (b"", b"", 0)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=_efecto)):
        result = await runner.run_batch(
            "CBBE",
            str(destino),
            manifest=ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture"),
            preset="Zeroed",
            preset_verified=True,
        )

    assert result.success is False
    assert result.estado == "CONCURRENT_MODIFICATION"
    assert externo_b.read_text(encoding="utf-8") == "<BuildSelection><zap>PRESERVADO-B</zap></BuildSelection>\n"
    assert enlace.is_symlink(), "el enlace no se reemplaza ni se borra"
