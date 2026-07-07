"""Tests de detección de versión de LOOT (T-14 de TECHNICAL_REVIEW_TASKS.md).

libloot <0.29 resuelve symlinks y "sale" del VFS de MO2 (LOOT ciego ante los
mods virtualizados — informe mmodding §3). El preflight (T-15) necesita saber
la versión instalada para advertir; la detección corre ``loot --version`` y
el parseo/advisory son funciones puras testeables sin el binario.
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
    async def test_detecta_desde_el_binario(self, tmp_path: pathlib.Path) -> None:
        run_capture = AsyncMock(return_value=(b"LOOT v0.28.0\n", b"", 0))

        with patch("sky_claw.local.loot.version.run_capture", run_capture):
            version = await detect_loot_version(tmp_path / "loot.exe")

        assert version == (0, 28, 0)
        argv = run_capture.await_args.args[0]
        assert argv[-1] == "--version"

    async def test_fallo_del_subproceso_devuelve_none(self, tmp_path: pathlib.Path) -> None:
        run_capture = AsyncMock(side_effect=OSError("no ejecutable"))

        with patch("sky_claw.local.loot.version.run_capture", run_capture):
            assert await detect_loot_version(tmp_path / "loot.exe") is None

    async def test_timeout_devuelve_none(self, tmp_path: pathlib.Path) -> None:
        run_capture = AsyncMock(side_effect=TimeoutError())

        with patch("sky_claw.local.loot.version.run_capture", run_capture):
            assert await detect_loot_version(tmp_path / "loot.exe") is None

    async def test_exit_non_zero_devuelve_none_aunque_haya_version(self, tmp_path: pathlib.Path) -> None:
        """Un exit != 0 es detección fallida: un falso verde suprimiría el
        advisory/bloqueo del preflight (review Copilot PR #239)."""
        run_capture = AsyncMock(return_value=(b"LOOT v0.29.0\n", b"error inesperado", 1))

        with patch("sky_claw.local.loot.version.run_capture", run_capture):
            assert await detect_loot_version(tmp_path / "loot.exe") is None
