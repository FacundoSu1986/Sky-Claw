"""Cableado del preflight en producción (reviews Codex P1/P2 del PR #239).

Dos hallazgos compuestos: (P1) el guard solo corría si el caller inyectaba
``PreflightService`` — y ningún call site de producción lo hacía, así que el
bloqueo era un no-op fuera de los tests; (P2) las rutas de
``PathResolutionService`` salen RESUELTAS por el validator, siguiendo los
symlinks — o sea que un checker cableado con ellas inspeccionaría la ruta real
y nunca vería el enlace que debe detectar.

La solución: accessors crudos (``get_*_path_raw``) y construcción perezosa del
preflight dentro de ``LootSortingService`` con esas rutas — todos los call
sites existentes quedan protegidos sin cambios.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.path_resolver import PathResolutionService
from sky_claw.local.mo2.load_order import LoadOrderPaths
from sky_claw.local.tools.loot_service import LootSortingService
from sky_claw.local.validators.preflight import PreflightService


def _puede_crear_symlinks() -> bool:
    """Mismo guard que tests/test_path_validator.py (privilegios en Windows)."""
    try:
        with tempfile.TemporaryDirectory() as td:
            origen = pathlib.Path(td) / "src.txt"
            origen.touch()
            (pathlib.Path(td) / "link.txt").symlink_to(origen)
        return True
    except (OSError, NotImplementedError):
        return False


_symlink_guard = pytest.mark.skipif(
    sys.platform == "win32" and not _puede_crear_symlinks(),
    reason="Crear symlinks requiere privilegios elevados en Windows",
)


class TestAccessorsCrudos:
    """get_*_path_raw devuelve la ruta configurada SIN resolver symlinks."""

    @_symlink_guard
    def test_skyrim_path_raw_preserva_el_symlink(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real = tmp_path / "SkyrimReal"
        real.mkdir()
        enlace = tmp_path / "Skyrim"
        enlace.symlink_to(real, target_is_directory=True)
        monkeypatch.setenv("SKYRIM_PATH", str(enlace))

        resolver = PathResolutionService(path_validator=MagicMock())

        assert resolver.get_skyrim_path_raw() == enlace
        assert resolver.get_skyrim_path_raw().is_symlink()  # type: ignore[union-attr]

    def test_sin_variable_devuelve_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MO2_PATH", raising=False)

        resolver = PathResolutionService(path_validator=MagicMock())

        assert resolver.get_mo2_path_raw() is None


class TestCableadoPerezoso:
    """Sin preflight inyectado, el servicio lo construye desde las rutas crudas."""

    def _service(self, resolver: MagicMock) -> LootSortingService:
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(files=(), sources=())
        return LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
            loot_runner=MagicMock(),
            load_order_resolver=load_order,
        )

    @_symlink_guard
    async def test_sort_en_produccion_queda_bloqueado_por_symlink_critico(self, tmp_path: pathlib.Path) -> None:
        """El escenario del P1: call site real (sin inyección) + game path
        symlinkeado (crudo) → el guard corre y bloquea."""
        real = tmp_path / "SkyrimReal"
        real.mkdir()
        enlace = tmp_path / "Skyrim"
        enlace.symlink_to(real, target_is_directory=True)

        resolver = MagicMock()
        resolver.get_skyrim_path_raw = MagicMock(return_value=enlace)
        resolver.get_mo2_path_raw = MagicMock(return_value=None)
        resolver.get_loot_exe = MagicMock(return_value=None)

        svc = self._service(resolver)
        resultado = await svc.sort_load_order()

        assert resultado["success"] is False
        assert resultado["preflight"]["status"] == "red"

    async def test_rutas_crudas_limpias_no_bloquean(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.antigravity.db.locks import DistributedLockManager
        from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
        from sky_claw.local.loot.parser import LOOTResult

        game = tmp_path / "Skyrim"
        game.mkdir()
        (tmp_path / "snapshots").mkdir()

        resolver = MagicMock()
        resolver.get_skyrim_path_raw = MagicMock(return_value=game)
        resolver.get_mo2_path_raw = MagicMock(return_value=None)
        resolver.get_loot_exe = MagicMock(return_value=None)

        runner = MagicMock()
        runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"]))
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(files=(), sources=())

        # Lock manager real: el camino verde llega hasta el lock (a diferencia
        # del bloqueado, que corta antes).
        lock_manager = DistributedLockManager(
            tmp_path / "locks.db",
            default_ttl=5.0,
            max_retries=2,
            backoff_base=0.05,
            backoff_max=0.2,
        )
        await lock_manager.initialize()
        try:
            svc = LootSortingService(
                lock_manager=lock_manager,
                snapshot_manager=FileSnapshotManager(snapshot_dir=tmp_path / "snapshots"),
                path_resolver=resolver,
                loot_runner=runner,
                load_order_resolver=load_order,
            )
            resultado = await svc.sort_load_order()
        finally:
            await lock_manager.close()

        assert resultado["success"] is True


class TestCacheDeVersion:
    """La versión de LOOT se detecta una vez por servicio, no por ritual."""

    async def test_detector_se_awaitea_una_sola_vez(self) -> None:
        detector = AsyncMock(return_value=(0, 29, 0))
        checker = MagicMock()
        checker.check.return_value = []
        servicio = PreflightService(vfs_checker=checker, loot_version_detector=detector)

        await servicio.run()
        await servicio.run()

        detector.assert_awaited_once()
