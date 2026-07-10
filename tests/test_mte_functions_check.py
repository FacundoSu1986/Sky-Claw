"""Detección de mteFunctions.pas (T-09 de TECHNICAL_REVIEW_TASKS.md).

Los scripts Pascal de Sky-Claw (estáticos y generados) declaran
``uses mteFunctions``, pero xEdit NO trae ``mteFunctions.pas`` de fábrica: sin
él en ``Edit Scripts/`` la compilación falla a mitad de un Ritual con un error
críptico. El faltante debe detectarse en discovery (con link de descarga) y el
runner debe fallar rápido con un mensaje accionable antes de lanzar xEdit.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sky_claw.local.discovery.scanner import EnvironmentScanner
from sky_claw.local.xedit.runner import XEditRunner, XEditScriptError

SCRIPT_CON_MTE = "unit Prueba;\nuses mteFunctions, SysUtils;\nend.\n"
SCRIPT_SIN_MTE = "unit Prueba;\nuses SysUtils;\nend.\n"


def _touch_exe(directory: Path, name: str) -> Path:
    """Crea un ejecutable stub; el scan solo verifica existencia."""
    exe = directory / name
    exe.write_bytes(b"MZ")
    return exe


def _instalar_mte_functions(xedit_dir: Path) -> Path:
    scripts_dir = xedit_dir / "Edit Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    mte = scripts_dir / "mteFunctions.pas"
    mte.write_text("unit mteFunctions;\nend.\n", encoding="utf-8")
    return mte


class TestDiscovery:
    """El scanner reporta el faltante con link accionable."""

    async def test_reporta_mtefunctions_faltante_junto_a_xedit(self, tmp_path: Path) -> None:
        xedit_exe = _touch_exe(tmp_path, "SSEEdit.exe")
        scanner = EnvironmentScanner(tool_paths={"xedit": str(xedit_exe)})

        snap = await scanner.scan()

        faltantes = [m for m in snap.missing if "mtefunctions" in m.technical_name.lower()]
        assert len(faltantes) == 1
        assert faltantes[0].download_url.startswith("https://")
        assert any("mteFunctions" in msg for msg in snap.health_messages)

    async def test_no_reporta_si_mtefunctions_presente(self, tmp_path: Path) -> None:
        xedit_exe = _touch_exe(tmp_path, "SSEEdit.exe")
        _instalar_mte_functions(tmp_path)
        scanner = EnvironmentScanner(tool_paths={"xedit": str(xedit_exe)})

        snap = await scanner.scan()

        assert not any("mtefunctions" in m.technical_name.lower() for m in snap.missing)

    async def test_sin_xedit_no_opina_sobre_mtefunctions(self, tmp_path: Path) -> None:
        """Sin xEdit detectado no hay dónde buscar Edit Scripts: no reportar."""
        scanner = EnvironmentScanner(tool_paths={"loot": str(_touch_exe(tmp_path, "loot.exe"))})

        snap = await scanner.scan()

        assert not any("mtefunctions" in m.technical_name.lower() for m in snap.missing)


class TestRunnerFailFast:
    """El runner corta ANTES de lanzar xEdit si el script requiere mteFunctions."""

    def _runner(self, tmp_path: Path) -> XEditRunner:
        xedit = _touch_exe(tmp_path, "SSEEdit.exe")
        game = tmp_path / "game"
        game.mkdir(exist_ok=True)
        runner = XEditRunner(xedit_path=xedit, game_path=game, timeout=5)
        # El subproceso nunca debe llegar a ejecutarse en estos tests.
        runner._execute_process = AsyncMock(return_value=("", "", 0))  # type: ignore[method-assign]
        return runner

    async def test_falla_rapido_con_mensaje_accionable(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)

        with pytest.raises(XEditScriptError, match="mteFunctions"):
            await runner.run_dynamic_script(SCRIPT_CON_MTE, plugins=["Prueba.esp"])

        runner._execute_process.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_ejecuta_si_mtefunctions_presente(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        _instalar_mte_functions(tmp_path)

        resultado = await runner.run_dynamic_script(SCRIPT_CON_MTE, plugins=["Prueba.esp"])

        assert resultado.exit_code == 0
        runner._execute_process.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_script_sin_mtefunctions_no_exige_el_archivo(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)

        resultado = await runner.run_dynamic_script(SCRIPT_SIN_MTE, plugins=["Prueba.esp"])

        assert resultado.exit_code == 0

    async def test_deteccion_es_case_insensitive(self, tmp_path: Path) -> None:
        """Pascal no distingue mayúsculas: 'uses MTEFUNCTIONS' también requiere la librería."""
        runner = self._runner(tmp_path)
        script = "unit Prueba;\nuses MTEFUNCTIONS, SysUtils;\nend.\n"

        with pytest.raises(XEditScriptError, match="mteFunctions"):
            await runner.run_dynamic_script(script, plugins=["Prueba.esp"])


class TestDynamicScriptNameValidation:
    """H-6: run_dynamic_script valida script_name contra path traversal."""

    def _runner(self, tmp_path: Path) -> XEditRunner:
        xedit = _touch_exe(tmp_path, "SSEEdit.exe")
        game = tmp_path / "game"
        game.mkdir(exist_ok=True)
        _instalar_mte_functions(tmp_path)
        runner = XEditRunner(xedit_path=xedit, game_path=game, timeout=5)
        runner._execute_process = AsyncMock(return_value=("", "", 0))  # type: ignore[method-assign]
        return runner

    @pytest.mark.parametrize(
        "malicious",
        [
            "../../evil.pas",
            "../escape.pas",
            "sub/dir.pas",
            "name;rm.pas",
            "sin_extension",
        ],
    )
    async def test_script_name_malicioso_rechazado(self, tmp_path: Path, malicious: str) -> None:
        from sky_claw.local.xedit.runner import XEditValidationError

        runner = self._runner(tmp_path)
        with pytest.raises(XEditValidationError, match="Invalid script name"):
            await runner.run_dynamic_script(SCRIPT_SIN_MTE, plugins=["Prueba.esp"], script_name=malicious)

        # No debió llegar a lanzar el proceso.
        runner._execute_process.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_script_name_valido_pasa(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        resultado = await runner.run_dynamic_script(
            SCRIPT_SIN_MTE, plugins=["Prueba.esp"], script_name="patch_create_merged_patch.pas"
        )
        assert resultado.exit_code == 0
