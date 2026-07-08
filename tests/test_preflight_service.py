"""Tests del agregador de preflight (T-15 de TECHNICAL_REVIEW_TASKS.md).

T-13 (VfsHealthChecker) y T-14 (versión de LOOT) son sensores; el
PreflightService es el actuador: compone las señales en un semáforo
verde/amarillo/rojo con una regla de composición no trivial — un symlink NO
crítico (amarillo) combinado con LOOT <0.29 (amarillo) fuerza ROJO, porque esa
combinación es exactamente el escenario documentado de LOOT ciego ante el VFS
(informe mmodding §3). Rojo bloquea a los rituales mutantes salvo override
explícito; el primer consumidor cableado es LootSortingService.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

from sky_claw.local.validators.preflight import (
    PreflightReport,
    PreflightService,
    PreflightStatus,
)
from sky_claw.local.validators.vfs_health import VfsIssue


def _issue(severity: str) -> VfsIssue:
    return VfsIssue(
        path=pathlib.Path("/mo2/mods/ModEnlazado"),
        kind="symlink",
        severity=severity,
        remediation="usar carpeta real",
    )


def _servicio(
    issues: list[VfsIssue] | None = None,
    version: tuple[int, int, int] | None = None,
) -> PreflightService:
    checker = MagicMock()
    checker.check.return_value = issues or []
    return PreflightService(
        vfs_checker=checker,
        loot_version_detector=AsyncMock(return_value=version),
    )


class TestSemaforo:
    async def test_todo_limpio_es_verde(self) -> None:
        reporte = await _servicio(issues=[], version=(0, 29, 0)).run()

        assert reporte.status is PreflightStatus.GREEN
        assert reporte.blocks_mutations is False

    async def test_symlink_warning_con_loot_seguro_es_amarillo(self) -> None:
        reporte = await _servicio(issues=[_issue("warning")], version=(0, 29, 1)).run()

        assert reporte.status is PreflightStatus.YELLOW
        assert reporte.blocks_mutations is False

    async def test_loot_viejo_sin_symlinks_es_amarillo(self) -> None:
        reporte = await _servicio(issues=[], version=(0, 28, 0)).run()

        assert reporte.status is PreflightStatus.YELLOW
        assert reporte.blocks_mutations is False

    async def test_composicion_symlink_mas_loot_viejo_fuerza_rojo(self) -> None:
        """La regla clave de T-15: dos amarillos que juntos son el escenario
        de LOOT ciego (symlinks presentes + libloot que los resuelve)."""
        reporte = await _servicio(issues=[_issue("warning")], version=(0, 28, 0)).run()

        assert reporte.status is PreflightStatus.RED
        assert reporte.blocks_mutations is True
        # La razón de la promoción debe ser explícita para el usuario.
        assert any("0.29" in c.summary for c in reporte.checks if c.status is PreflightStatus.RED)

    async def test_vfs_critico_es_rojo_aunque_loot_sea_seguro(self) -> None:
        reporte = await _servicio(issues=[_issue("critical")], version=(0, 29, 0)).run()

        assert reporte.status is PreflightStatus.RED
        assert reporte.blocks_mutations is True

    async def test_version_desconocida_con_symlink_es_amarillo(self) -> None:
        """Sin confirmación de versión no se bloquea, pero se advierte fuerte."""
        reporte = await _servicio(issues=[_issue("warning")], version=None).run()

        assert reporte.status is PreflightStatus.YELLOW
        assert reporte.blocks_mutations is False

    async def test_sensores_sin_configurar_es_verde(self) -> None:
        """Sin checker ni detector no hay señales: no inventar problemas."""
        reporte = await PreflightService().run()

        assert reporte.status is PreflightStatus.GREEN
        # Pero sin mentir: "sin symlinks" implicaría que se verificó
        # (review Copilot PR #239) — debe decir que el sensor no está.
        vfs = next(c for c in reporte.checks if c.name == "vfs")
        assert "no configurad" in vfs.summary.lower()


class TestContratoDeDatos:
    async def test_to_dict_es_serializable_y_estable(self) -> None:
        reporte = await _servicio(issues=[_issue("warning")], version=(0, 28, 0)).run()

        datos = reporte.to_dict()

        assert datos["status"] == "red"
        assert datos["blocks_mutations"] is True
        assert isinstance(datos["checks"], list)
        for check in datos["checks"]:
            assert set(check) == {"name", "status", "summary", "details"}
            assert isinstance(check["details"], list)

    async def test_reporte_incluye_ambos_sensores(self) -> None:
        reporte = await _servicio(issues=[], version=(0, 29, 0)).run()

        nombres = {c.name for c in reporte.checks}
        assert "vfs" in nombres
        assert "loot_version" in nombres


class TestBloqueoDeMutantes:
    """El primer mutante cableado: LootSortingService respeta el semáforo."""

    def _loot_service(self, reporte: PreflightReport):
        from sky_claw.antigravity.db.locks import DistributedLockManager
        from sky_claw.local.mo2.load_order import LoadOrderPaths
        from sky_claw.local.tools.loot_service import LootSortingService

        runner = MagicMock()
        runner.sort = AsyncMock()
        preflight = MagicMock()
        preflight.run = AsyncMock(return_value=reporte)
        resolver = MagicMock()
        resolver.resolve.return_value = LoadOrderPaths(files=(), sources=())
        svc = LootSortingService(
            lock_manager=MagicMock(spec=DistributedLockManager),
            snapshot_manager=MagicMock(),
            path_resolver=MagicMock(),
            loot_runner=runner,
            load_order_resolver=resolver,
            preflight=preflight,
        )
        return svc, runner

    async def test_preflight_rojo_bloquea_el_sort(self) -> None:
        reporte = await _servicio(issues=[_issue("warning")], version=(0, 28, 0)).run()
        svc, runner = self._loot_service(reporte)

        resultado = await svc.sort_load_order()

        assert resultado["success"] is False
        assert "preflight" in resultado["message"].lower()
        assert resultado["preflight"]["status"] == "red"
        runner.sort.assert_not_awaited()

    async def test_override_explicito_permite_correr(self) -> None:
        reporte = await _servicio(issues=[_issue("warning")], version=(0, 28, 0)).run()
        svc, runner = self._loot_service(reporte)
        from sky_claw.local.loot.parser import LOOTResult

        runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"]))

        resultado = await svc.sort_load_order(override_preflight=True)

        assert resultado["success"] is True
        runner.sort.assert_awaited_once()

    async def test_preflight_amarillo_no_bloquea(self) -> None:
        reporte = await _servicio(issues=[], version=(0, 28, 0)).run()
        svc, runner = self._loot_service(reporte)
        from sky_claw.local.loot.parser import LOOTResult

        runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"]))

        resultado = await svc.sort_load_order()

        assert resultado["success"] is True
        runner.sort.assert_awaited_once()


class TestSensorDeMasters:
    """T-30·1 (cableado): el sensor de masters compone en el semáforo."""

    @staticmethod
    def _issue_master(severity: str, kind: str = "missing") -> object:
        from sky_claw.local.validators.missing_masters import MasterIssue

        return MasterIssue(
            plugin="Mod.esp",
            master="NoInstalado.esm",
            kind=kind,  # type: ignore[arg-type]
            severity=severity,  # type: ignore[arg-type]
            remediation="instalá el mod que lo provee",
        )

    async def test_master_faltante_fuerza_rojo(self) -> None:
        checker = MagicMock()
        checker.check.return_value = []
        servicio = PreflightService(
            vfs_checker=checker,
            loot_version_detector=AsyncMock(return_value=(0, 29, 0)),
            masters_check=lambda: [self._issue_master("critical")],
        )

        reporte = await servicio.run()

        assert reporte.status is PreflightStatus.RED
        assert reporte.blocks_mutations is True
        masters = next(c for c in reporte.checks if c.name == "masters")
        assert masters.status is PreflightStatus.RED
        assert any("NoInstalado.esm" in d for d in masters.details)

    async def test_masters_limpios_es_verde(self) -> None:
        checker = MagicMock()
        checker.check.return_value = []
        servicio = PreflightService(
            vfs_checker=checker,
            loot_version_detector=AsyncMock(return_value=(0, 29, 0)),
            masters_check=lambda: [],
        )

        reporte = await servicio.run()

        assert reporte.status is PreflightStatus.GREEN
        masters = next(c for c in reporte.checks if c.name == "masters")
        assert masters.status is PreflightStatus.GREEN

    async def test_solo_warnings_de_masters_es_amarillo(self) -> None:
        checker = MagicMock()
        checker.check.return_value = []
        servicio = PreflightService(
            vfs_checker=checker,
            loot_version_detector=AsyncMock(return_value=(0, 29, 0)),
            masters_check=lambda: [self._issue_master("warning", kind="plugin_not_found")],
        )

        reporte = await servicio.run()

        assert reporte.status is PreflightStatus.YELLOW
        assert reporte.blocks_mutations is False

    async def test_sin_sensor_de_masters_no_miente(self) -> None:
        """Sin sensor configurado el check es verde pero dice 'no configurado'
        — misma regla de honestidad que vfs/loot_version."""
        reporte = await _servicio(issues=[], version=(0, 29, 0)).run()

        masters = next(c for c in reporte.checks if c.name == "masters")
        assert masters.status is PreflightStatus.GREEN
        assert "no configurado" in masters.summary.lower()
