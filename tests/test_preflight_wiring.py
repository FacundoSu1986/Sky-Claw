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
from unittest.mock import AsyncMock, MagicMock, patch

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

        crudo = resolver.get_skyrim_path_raw()
        assert crudo is not None
        assert crudo == enlace
        assert crudo.is_symlink()

    def test_sin_variable_devuelve_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MO2_PATH", raising=False)

        resolver = PathResolutionService(path_validator=MagicMock())

        assert resolver.get_mo2_path_raw() is None

    def test_valor_con_byte_nulo_degrada_a_none(self) -> None:
        """Un env corrupto no debe tumbar el preflight: los bytes nulos
        explotan recién en los os-calls del checker (review Copilot #240).
        (putenv no acepta '\\0', así que se mockea environ.get directamente.)"""
        resolver = PathResolutionService(path_validator=MagicMock())

        with patch(
            "sky_claw.antigravity.core.path_resolver.os.environ.get",
            return_value="C:\\Juegos\x00\\Skyrim",
        ):
            assert resolver.get_skyrim_path_raw() is None


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
        resolver.get_mo2_path = MagicMock(return_value=None)
        resolver.detect_mo2_path = MagicMock(return_value=None)
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
        resolver.get_mo2_path = MagicMock(return_value=None)
        resolver.detect_mo2_path = MagicMock(return_value=None)
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


class TestCallSiteDelAgente:
    """El path del agente (sin path_resolver) también queda protegido (P1 #240)."""

    @_symlink_guard
    async def test_construye_preflight_desde_mo2_root(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.validators.preflight import PreflightStatus

        mo2 = tmp_path / "MO2"
        (mo2 / "mods").mkdir(parents=True)
        real = tmp_path / "ModReal"
        real.mkdir()
        (mo2 / "mods" / "ModEnlazado").symlink_to(real, target_is_directory=True)

        svc = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            loot_runner=MagicMock(),
            mo2_root=mo2,
        )

        preflight = svc._ensure_preflight()
        assert preflight is not None

        reporte = await preflight.run()
        vfs = next(c for c in reporte.checks if c.name == "vfs")
        assert vfs.status is PreflightStatus.YELLOW
        assert any("ModEnlazado" in d for d in vfs.details)


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

    async def test_runs_concurrentes_no_duplican_la_deteccion(self) -> None:
        """Dos run() en paralelo no deben disparar `loot --version` dos veces
        (review Copilot #240): el lock serializa el primer llenado del caché."""
        import asyncio

        detecciones = 0

        async def detector_lento() -> tuple[int, int, int]:
            nonlocal detecciones
            detecciones += 1
            await asyncio.sleep(0.05)
            return (0, 29, 0)

        checker = MagicMock()
        checker.check.return_value = []
        servicio = PreflightService(vfs_checker=checker, loot_version_detector=detector_lento)

        await asyncio.gather(servicio.run(), servicio.run())

        assert detecciones == 1


class TestSensoresDeModlistCableados:
    """T-30w: _ensure_preflight cablea los sensores de masters/límites cuando
    las fuentes de plugins son resolubles (antes quedaban inertes: Codex #250)."""

    @staticmethod
    def _fixture_mo2(tmp_path: pathlib.Path, *, master_faltante: bool) -> tuple[pathlib.Path, pathlib.Path]:
        """Arma una instancia MO2 mínima con un plugin y su plugins.txt.

        Con ``master_faltante`` el plugin declara un master que no está en disco
        → el sensor de masters debe marcarlo crítico.
        """
        import struct

        def _tes4(path: pathlib.Path, masters: list[str]) -> None:
            subrecords = b"HEDR" + struct.pack("<H", 12) + struct.pack("<fiI", 1.7, 0, 0x800)
            for m in masters:
                data = m.encode("cp1252") + b"\x00"
                subrecords += b"MAST" + struct.pack("<H", len(data)) + data
                subrecords += b"DATA" + struct.pack("<H", 8) + struct.pack("<Q", 0)
            path.write_bytes(b"TES4" + struct.pack("<IIIIHH", len(subrecords), 0, 0, 0, 44, 0) + subrecords)

        game_data = tmp_path / "Skyrim" / "Data"
        game_data.mkdir(parents=True)
        _tes4(game_data / "Skyrim.esm", [])

        mo2 = tmp_path / "MO2"
        mod = mo2 / "mods" / "MiMod"
        mod.mkdir(parents=True)
        masters = ["Skyrim.esm", "NoInstalado.esm"] if master_faltante else ["Skyrim.esm"]
        _tes4(mod / "MiMod.esp", masters)

        lo = tmp_path / "plugins.txt"
        lo.write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n*MiMod.esp\r\n")
        return mo2, lo

    def _resolver(self, *, skyrim: pathlib.Path, mo2: pathlib.Path) -> MagicMock:
        resolver = MagicMock()
        resolver.get_skyrim_path_raw = MagicMock(return_value=skyrim)
        resolver.get_skyrim_path = MagicMock(return_value=skyrim)
        resolver.get_mo2_path_raw = MagicMock(return_value=mo2)
        resolver.get_mo2_path = MagicMock(return_value=mo2)
        resolver.detect_mo2_path = MagicMock(return_value=mo2)
        resolver.get_active_profile = MagicMock(return_value="Default")
        resolver.get_loot_exe = MagicMock(return_value=None)
        return resolver

    async def test_master_faltante_pone_el_preflight_rojo(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.validators.preflight import PreflightStatus

        mo2, lo = self._fixture_mo2(tmp_path, master_faltante=True)
        resolver = self._resolver(skyrim=tmp_path / "Skyrim", mo2=mo2)
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(files=(lo,), sources=("override",))

        svc = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
            loot_runner=MagicMock(),
            load_order_resolver=load_order,
        )

        reporte = await svc._ensure_preflight().run()

        masters = next(c for c in reporte.checks if c.name == "masters")
        assert masters.status is PreflightStatus.RED
        assert any("NoInstalado.esm" in d for d in masters.details)
        assert reporte.blocks_mutations is True

    async def test_modlist_sana_no_bloquea_por_masters(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.validators.preflight import PreflightStatus

        mo2, lo = self._fixture_mo2(tmp_path, master_faltante=False)
        resolver = self._resolver(skyrim=tmp_path / "Skyrim", mo2=mo2)
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(files=(lo,), sources=("override",))

        svc = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
            loot_runner=MagicMock(),
            load_order_resolver=load_order,
        )

        reporte = await svc._ensure_preflight().run()

        masters = next(c for c in reporte.checks if c.name == "masters")
        limites = next(c for c in reporte.checks if c.name == "plugin_limits")
        assert masters.status is PreflightStatus.GREEN
        assert limites.status is PreflightStatus.GREEN
        assert "2/254 full" in limites.summary

    async def test_sin_load_order_los_sensores_dicen_no_configurado(self, tmp_path: pathlib.Path) -> None:
        """Sin archivo de load order resoluble, no se miente verde: los checks
        de masters/límites reportan 'no configurado'."""
        resolver = MagicMock()
        resolver.get_skyrim_path_raw = MagicMock(return_value=tmp_path / "Skyrim")
        resolver.get_skyrim_path = MagicMock(return_value=tmp_path / "Skyrim")
        resolver.get_mo2_path_raw = MagicMock(return_value=None)
        resolver.get_mo2_path = MagicMock(return_value=None)
        resolver.detect_mo2_path = MagicMock(return_value=None)
        resolver.get_active_profile = MagicMock(return_value="Default")
        resolver.get_loot_exe = MagicMock(return_value=None)
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(files=(), sources=())

        svc = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
            loot_runner=MagicMock(),
            load_order_resolver=load_order,
        )

        reporte = await svc._ensure_preflight().run()

        masters = next(c for c in reporte.checks if c.name == "masters")
        assert "no configurado" in masters.summary.lower()


class TestPreferenciaYCallSiteAgente:
    """Refinamientos del review Copilot #252: enabled desde plugins.txt (no
    loadorder.txt) y el call site del agente (mo2_root sin path_resolver)."""

    @staticmethod
    def _tes4(path: pathlib.Path, masters: list[str]) -> None:
        import struct

        subrecords = b"HEDR" + struct.pack("<H", 12) + struct.pack("<fiI", 1.7, 0, 0x800)
        for m in masters:
            data = m.encode("cp1252") + b"\x00"
            subrecords += b"MAST" + struct.pack("<H", len(data)) + data
            subrecords += b"DATA" + struct.pack("<H", 8) + struct.pack("<Q", 0)
        path.write_bytes(b"TES4" + struct.pack("<IIIIHH", len(subrecords), 0, 0, 0, 44, 0) + subrecords)

    async def test_enabled_sale_de_plugins_txt_no_de_loadorder(self, tmp_path: pathlib.Path) -> None:
        """Un plugin deshabilitado (en loadorder.txt pero no activo en
        plugins.txt) con master faltante NO debe disparar rojo."""
        from sky_claw.local.validators.preflight import PreflightStatus

        game_data = tmp_path / "Skyrim" / "Data"
        game_data.mkdir(parents=True)
        self._tes4(game_data / "Skyrim.esm", [])
        mo2 = tmp_path / "MO2"
        mod = mo2 / "mods" / "MiMod"
        mod.mkdir(parents=True)
        self._tes4(mod / "MiMod.esp", ["Skyrim.esm"])
        self._tes4(mod / "Deshabilitado.esp", ["NoInstalado.esm"])  # rompería si contara

        plugins_txt = tmp_path / "plugins.txt"
        plugins_txt.write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n*MiMod.esp\r\n")  # Deshabilitado NO activo
        loadorder_txt = tmp_path / "loadorder.txt"
        loadorder_txt.write_text("Skyrim.esm\nMiMod.esp\nDeshabilitado.esp\n", encoding="utf-8")

        resolver = MagicMock()
        resolver.get_skyrim_path_raw = MagicMock(return_value=tmp_path / "Skyrim")
        resolver.get_skyrim_path = MagicMock(return_value=tmp_path / "Skyrim")
        resolver.get_mo2_path_raw = MagicMock(return_value=mo2)
        resolver.get_mo2_path = MagicMock(return_value=mo2)
        resolver.detect_mo2_path = MagicMock(return_value=mo2)
        resolver.get_active_profile = MagicMock(return_value="Default")
        resolver.get_loot_exe = MagicMock(return_value=None)
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(files=(loadorder_txt, plugins_txt), sources=("localappdata",))

        svc = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
            loot_runner=MagicMock(),
            load_order_resolver=load_order,
        )

        reporte = await svc._ensure_preflight().run()
        masters = next(c for c in reporte.checks if c.name == "masters")
        assert masters.status is PreflightStatus.GREEN

    async def test_call_site_agente_encuentra_el_load_order_por_mo2_root(self, tmp_path: pathlib.Path) -> None:
        """Sin path_resolver pero con mo2_root, el resolver de load order debe
        usar ese root y encontrar el plugins.txt del profile → sensores activos."""
        from sky_claw.local.validators.preflight import PreflightStatus

        mo2 = tmp_path / "MO2"
        mod = mo2 / "mods" / "MiMod"
        mod.mkdir(parents=True)
        self._tes4(mod / "MiMod.esp", ["NoInstalado.esm"])  # master faltante → rojo
        profile = mo2 / "profiles" / "Default"
        profile.mkdir(parents=True)
        (profile / "plugins.txt").write_bytes(b"\xef\xbb\xbf*MiMod.esp\r\n")

        svc = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            loot_runner=MagicMock(),
            mo2_root=mo2,
        )

        reporte = await svc._ensure_preflight().run()
        masters = next(c for c in reporte.checks if c.name == "masters")
        assert "no configurado" not in masters.summary.lower()
        assert masters.status is PreflightStatus.RED


class TestFreshnessYOverwrite:
    """Hallazgos nuevos del review Codex #252: re-resolución por run y overwrite."""

    @staticmethod
    def _tes4(path: pathlib.Path, masters: list[str]) -> None:
        import struct

        subrecords = b"HEDR" + struct.pack("<H", 12) + struct.pack("<fiI", 1.7, 0, 0x800)
        for m in masters:
            data = m.encode("cp1252") + b"\x00"
            subrecords += b"MAST" + struct.pack("<H", len(data)) + data
            subrecords += b"DATA" + struct.pack("<H", 8) + struct.pack("<Q", 0)
        path.write_bytes(b"TES4" + struct.pack("<IIIIHH", len(subrecords), 0, 0, 0, 44, 0) + subrecords)

    def _svc(self, tmp_path: pathlib.Path, mo2: pathlib.Path, lo: pathlib.Path) -> LootSortingService:
        resolver = MagicMock()
        resolver.get_skyrim_path_raw = MagicMock(return_value=tmp_path / "Skyrim")
        resolver.get_skyrim_path = MagicMock(return_value=tmp_path / "Skyrim")
        resolver.get_mo2_path_raw = MagicMock(return_value=mo2)
        resolver.get_mo2_path = MagicMock(return_value=mo2)
        resolver.detect_mo2_path = MagicMock(return_value=mo2)
        resolver.get_active_profile = MagicMock(return_value="Default")
        resolver.get_loot_exe = MagicMock(return_value=None)
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(files=(lo,), sources=("override",))
        return LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
            loot_runner=MagicMock(),
            load_order_resolver=load_order,
        )

    async def test_preflight_ve_plugins_activados_despues_del_primer_run(self, tmp_path: pathlib.Path) -> None:
        """El PreflightService se cachea, pero los closures re-resuelven: un
        plugin activado tras el primer run debe verse en el segundo."""
        from sky_claw.local.validators.preflight import PreflightStatus

        game_data = tmp_path / "Skyrim" / "Data"
        game_data.mkdir(parents=True)
        self._tes4(game_data / "Skyrim.esm", [])
        mo2 = tmp_path / "MO2"
        mod = mo2 / "mods" / "MiMod"
        mod.mkdir(parents=True)
        self._tes4(mod / "Bad.esp", ["NoInstalado.esm"])  # roto, pero aún NO activo

        lo = tmp_path / "plugins.txt"
        lo.write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n")  # Bad.esp inactivo

        svc = self._svc(tmp_path, mo2, lo)
        preflight = svc._ensure_preflight()

        primero = await preflight.run()
        assert next(c for c in primero.checks if c.name == "masters").status is PreflightStatus.GREEN

        # El usuario activa Bad.esp DESPUÉS del primer run.
        lo.write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n*Bad.esp\r\n")

        segundo = await preflight.run()
        assert next(c for c in segundo.checks if c.name == "masters").status is PreflightStatus.RED

    async def test_plugin_generado_en_overwrite_se_escanea(self, tmp_path: pathlib.Path) -> None:
        """Un plugin activo que vive en el overwrite (bashed/DynDOLOD) se
        encuentra: sin escanear overwrite quedaría como plugin_not_found."""
        from sky_claw.local.validators.preflight import PreflightStatus

        game_data = tmp_path / "Skyrim" / "Data"
        game_data.mkdir(parents=True)
        mo2 = tmp_path / "MO2"
        (mo2 / "mods").mkdir(parents=True)
        overwrite = mo2 / "overwrite"
        overwrite.mkdir(parents=True)
        self._tes4(overwrite / "Bashed Patch, 0.esp", ["NoInstalado.esm"])  # generado + master faltante

        lo = tmp_path / "plugins.txt"
        lo.write_bytes(b"\xef\xbb\xbf*Bashed Patch, 0.esp\r\n")

        svc = self._svc(tmp_path, mo2, lo)
        reporte = await svc._ensure_preflight().run()

        masters = next(c for c in reporte.checks if c.name == "masters")
        assert masters.status is PreflightStatus.RED
        assert any(i in d for d in masters.details for i in ("NoInstalado.esm",))
