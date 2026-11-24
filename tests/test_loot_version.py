"""Tests de detección de versión de LOOT (T-14 de TECHNICAL_REVIEW_TASKS.md).

libloot <0.29 resuelve symlinks y "sale" del VFS de MO2 (LOOT ciego ante los
mods virtualizados — informe mmodding §3). El preflight (T-15) necesita saber
la versión instalada para advertir; la detección lee el ``VERSIONINFO`` del PE
(M1, sin lanzar el binario) y el parseo/advisory son funciones puras.
"""

import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.loot.version import (
    LOOT_MIN_SYMLINK_SAFE,
    detect_loot_version,
    parse_loot_version,
    symlink_advisory,
)
from tests._loot_pe import escribir_loot_exe


class TestParseo:
    @pytest.mark.parametrize(
        ("salida", "esperado"),
        [
            ("LOOT v0.28.0", (0, 28, 0)),
            ("0.29.1+3f2a build 2026", (0, 29, 1)),
            ("loot 1.0.12\n", (1, 0, 12)),
            ("", None),
            ("sin version aca", None),
        ],
    )
    def test_parsea_formatos_conocidos(self, salida: str, esperado: tuple[int, int, int] | None) -> None:
        assert parse_loot_version(salida) == esperado


class TestAdvisory:
    def test_version_vieja_advierte_symlinks(self) -> None:
        aviso = symlink_advisory((0, 28, 0))

        assert aviso is not None
        assert "symlink" in aviso.lower()
        assert "0.29" in aviso

    def test_version_segura_no_advierte(self) -> None:
        assert symlink_advisory(LOOT_MIN_SYMLINK_SAFE) is None
        assert symlink_advisory((1, 0, 0)) is None

    def test_version_desconocida_advierte_con_cautela(self) -> None:
        """Si no se pudo detectar, mejor avisar que asumir que está todo bien."""
        aviso = symlink_advisory(None)

        assert aviso is not None
        assert "0.29" in aviso


class TestDeteccion:
    """M1: la versión sale del ``VERSIONINFO`` del PE, sin lanzar ``LOOT.exe``.

    ``LOOT.exe --version`` toma el mutex global ``LOOT.Shell.Instance`` antes de
    parsear argumentos (loot/loot 0.29.1 ``src/gui/qt/main.cpp``: el
    ``ApplicationMutexGuard`` se construye antes del ``QCommandLineParser``):
    con el GUI del operador abierto sale 0 sin imprimir y la versión quedaba
    "desconocida". La lectura del PE es la misma que atestigua ``lastVersion``
    (``binary_version.read_loot_binary_version``).
    """

    async def test_detecta_desde_el_versioninfo_del_binario(self, tmp_path: pathlib.Path) -> None:
        exe = escribir_loot_exe(tmp_path / "LOOT" / "LOOT.exe", (0, 28, 0))

        assert await detect_loot_version(exe) == (0, 28, 0)

    async def test_no_lanza_ningun_proceso(self, tmp_path: pathlib.Path) -> None:
        exe = escribir_loot_exe(tmp_path / "LOOT" / "LOOT.exe", (0, 29, 2))
        lanzador = AsyncMock(side_effect=AssertionError("no debe lanzar LOOT.exe"))

        with patch("asyncio.create_subprocess_exec", lanzador):
            assert await detect_loot_version(exe) == (0, 29, 2)
        lanzador.assert_not_awaited()

    async def test_binario_ausente_devuelve_none(self, tmp_path: pathlib.Path) -> None:
        assert await detect_loot_version(tmp_path / "loot.exe") is None

    async def test_binario_sin_versioninfo_devuelve_none(self, tmp_path: pathlib.Path) -> None:
        exe = tmp_path / "loot.exe"
        exe.write_bytes(b"MZ no es un PE completo")

        assert await detect_loot_version(exe) is None

    def test_el_modulo_no_importa_lanzadores_de_procesos(self) -> None:
        import ast

        import sky_claw.local.loot.version as modulo

        arbol = ast.parse(pathlib.Path(modulo.__file__).read_text(encoding="utf-8"))
        nombres = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)
        }
        assert not nombres & {"run_capture", "create_subprocess_exec", "Popen", "run"}
