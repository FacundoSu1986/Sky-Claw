"""Tests del staging de scripts Pascal bundleados (PR-2 grass cache).

``run_script`` pasa ``-script:<nombre>`` y xEdit lo resuelve contra SU carpeta
``Edit Scripts/`` — pero nadie copiaba los ``.pas`` bundleados del repo ahí:
``list_all_conflicts.pas`` solo funcionaba si el usuario lo copiaba a mano.
``stage_scripts`` cierra ese gap de forma idempotente (byte-compare, copia
solo si falta o difiere) y ``XEditRunner.ensure_scripts_staged`` lo expone
async. Los analyzers stagean su script en el camino caliente.

Lección #275: nada de discovery real — runners con paths falsos en tmp_path.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer
from sky_claw.local.xedit.output_parser import XEditResult
from sky_claw.local.xedit.runner import XEditRunner
from sky_claw.local.xedit.script_staging import (
    BUNDLED_SCRIPTS_DIR,
    stage_scripts,
)

_PRECEDENTE = "list_all_conflicts.pas"  # script bundleado que ya existe en el repo


@pytest.fixture
def xedit_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Instalación falsa de xEdit: el dir existe, ``Edit Scripts/`` todavía no."""
    d = tmp_path / "xedit"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# stage_scripts (núcleo síncrono)
# ---------------------------------------------------------------------------


def test_copia_script_faltante_y_crea_edit_scripts(xedit_dir: pathlib.Path) -> None:
    destino = xedit_dir / "Edit Scripts"

    resultado = stage_scripts(destino, [_PRECEDENTE])

    assert len(resultado) == 1
    assert resultado[0].action == "copied"
    assert resultado[0].destination == destino / _PRECEDENTE
    assert (destino / _PRECEDENTE).read_bytes() == (BUNDLED_SCRIPTS_DIR / _PRECEDENTE).read_bytes()


def test_no_reescribe_si_bytes_identicos(xedit_dir: pathlib.Path) -> None:
    destino = xedit_dir / "Edit Scripts"
    stage_scripts(destino, [_PRECEDENTE])
    mtime_original = (destino / _PRECEDENTE).stat().st_mtime_ns

    resultado = stage_scripts(destino, [_PRECEDENTE])

    assert resultado[0].action == "unchanged"
    assert (destino / _PRECEDENTE).stat().st_mtime_ns == mtime_original


def test_reemplaza_si_el_contenido_difiere(xedit_dir: pathlib.Path) -> None:
    destino = xedit_dir / "Edit Scripts"
    destino.mkdir()
    (destino / _PRECEDENTE).write_text("{ version vieja desactualizada }", encoding="utf-8")

    resultado = stage_scripts(destino, [_PRECEDENTE])

    assert resultado[0].action == "replaced"
    assert (destino / _PRECEDENTE).read_bytes() == (BUNDLED_SCRIPTS_DIR / _PRECEDENTE).read_bytes()


def test_nombre_fuera_del_bundle_lanza(xedit_dir: pathlib.Path) -> None:
    destino = xedit_dir / "Edit Scripts"

    # Inexistente en el bundle = bug de packaging o typo: fail-closed.
    with pytest.raises(FileNotFoundError):
        stage_scripts(destino, ["no_existe.pas"])

    # Traversal fuera del directorio bundleado: rechazado antes de tocar disco.
    with pytest.raises(ValueError, match="bundle"):
        stage_scripts(destino, ["../evil.pas"])


def test_nombre_con_traversal_que_resuelve_al_bundle_no_escapa_destino(xedit_dir: pathlib.Path) -> None:
    # ``../scripts/list_all_conflicts.pas`` resuelve DENTRO del bundle (pasa el
    # check de traversal), pero si el nombre crudo se reusara para el destino
    # escaparia de Edit Scripts/ hacia xedit_dir/scripts/. El staging debe usar
    # el basename real, no el string tal cual vino.
    destino = xedit_dir / "Edit Scripts"
    nombre_con_traversal = "../scripts/" + _PRECEDENTE

    resultado = stage_scripts(destino, [nombre_con_traversal])

    assert resultado[0].destination == destino / _PRECEDENTE
    assert resultado[0].destination.is_relative_to(destino)
    assert (destino / _PRECEDENTE).exists()
    assert not (xedit_dir / "scripts" / _PRECEDENTE).exists()


def test_dir_xedit_inexistente_lanza(tmp_path: pathlib.Path) -> None:
    # El parent de Edit Scripts es el dir de xEdit: si no existe, NO se
    # materializa una instalación fantasma.
    destino = tmp_path / "no_existe" / "Edit Scripts"

    with pytest.raises(FileNotFoundError):
        stage_scripts(destino, [_PRECEDENTE])


# ---------------------------------------------------------------------------
# Integración con XEditRunner y ConflictAnalyzer
# ---------------------------------------------------------------------------


async def test_runner_ensure_scripts_staged_delega(tmp_path: pathlib.Path) -> None:
    runner = XEditRunner(
        xedit_path=tmp_path / "SSEEdit.exe",
        game_path=tmp_path,
    )

    resultado = await runner.ensure_scripts_staged([_PRECEDENTE])

    assert resultado[0].action == "copied"
    assert (tmp_path / "Edit Scripts" / _PRECEDENTE).exists()


async def test_runner_ensure_scripts_staged_valida_xedit_path_antes_de_escribir(
    tmp_path: pathlib.Path,
) -> None:
    # run_script consulta al validator ANTES de tocar el ejecutable; el
    # staging debia hacer lo mismo. Sin este guard, un xedit_path fuera del
    # sandbox (config mala o comprometida) crea/sobreescribe "Edit Scripts/"
    # fuera de los roots permitidos antes de que el validator diga que no.
    from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError

    fuera_del_sandbox = tmp_path / "fuera"
    fuera_del_sandbox.mkdir()
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    validator = PathValidator(roots=[sandbox_root])
    runner = XEditRunner(
        xedit_path=fuera_del_sandbox / "SSEEdit.exe",
        game_path=tmp_path,
        path_validator=validator,
    )

    with pytest.raises(PathViolationError):
        await runner.ensure_scripts_staged([_PRECEDENTE])

    assert not (fuera_del_sandbox / "Edit Scripts").exists()


async def test_conflict_analyzer_stagea_su_script() -> None:
    # Cierra el gap existente de paso: analyze() garantiza su .pas en destino
    # ANTES de lanzar xEdit.
    runner = MagicMock()
    runner.attach_mock(AsyncMock(return_value=[]), "ensure_scripts_staged")
    runner.attach_mock(
        AsyncMock(
            return_value=XEditResult(
                return_code=0,
                raw_stdout="SUMMARY|total_conflicts=0|critical=0|minor=0\n",
                raw_stderr="",
            )
        ),
        "run_script",
    )

    await ConflictAnalyzer().analyze(["Skyrim.esm"], runner)

    nombres = [nombre for nombre, _args, _kwargs in runner.mock_calls]
    assert nombres.index("ensure_scripts_staged") < nombres.index("run_script")
    runner.ensure_scripts_staged.assert_awaited_once_with([_PRECEDENTE])


# ---------------------------------------------------------------------------
# run_script: timeout por llamada (el scan de LAND excede los 120s default)
# ---------------------------------------------------------------------------


async def test_run_script_sin_timeout_usa_el_del_constructor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "SSEEdit.exe").write_bytes(b"MZ")
    capturado = AsyncMock(return_value=(b"", b"", 0))
    monkeypatch.setattr("sky_claw.local.xedit.runner.run_capture", capturado)
    runner = XEditRunner(xedit_path=tmp_path / "SSEEdit.exe", game_path=tmp_path, timeout=77)

    await runner.run_script(_PRECEDENTE, ["Skyrim.esm"])

    assert capturado.await_args is not None
    assert capturado.await_args.kwargs["timeout"] == 77


async def test_run_script_timeout_por_llamada_gana(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "SSEEdit.exe").write_bytes(b"MZ")
    capturado = AsyncMock(return_value=(b"", b"", 0))
    monkeypatch.setattr("sky_claw.local.xedit.runner.run_capture", capturado)
    runner = XEditRunner(xedit_path=tmp_path / "SSEEdit.exe", game_path=tmp_path, timeout=77)

    await runner.run_script(_PRECEDENTE, ["Skyrim.esm"], timeout=1800)

    assert capturado.await_args is not None
    assert capturado.await_args.kwargs["timeout"] == 1800
