"""Tests for sky_claw.orchestrator.sync_engine."""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from tenacity import wait_none

from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import (
    SyncConfig,
    SyncEngine,
    SyncMetrics,
    SyncResult,
    _extract_nexus_id,
)
from sky_claw.scraper.masterlist import MasterlistClient, MasterlistFetchError
from sky_claw.security.hitl import Decision
from sky_claw.security.network_gateway import NetworkGateway
from sky_claw.security.path_validator import PathValidator

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_mo2(tmp_path: pathlib.Path, lines: str) -> MO2Controller:
    """Create a minimal MO2 layout with the given modlist content."""
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text(lines, encoding="utf-8")
    validator = PathValidator(roots=[tmp_path])
    return MO2Controller(tmp_path, path_validator=validator)


def _fake_mod_info(mod_id: int, name: str = "TestMod") -> dict[str, Any]:
    """Dict compatible con la respuesta de ``MasterlistClient.fetch_mod_info``."""
    return {
        "mod_id": mod_id,
        "name": name,
        "version": "1.0",
        "author": "author",
        "category_id": "5",
    }


# ------------------------------------------------------------------
# _extract_nexus_id
# ------------------------------------------------------------------


class TestExtractNexusId:
    def test_standard_pattern(self) -> None:
        assert _extract_nexus_id("SkyUI-3863-v5-2") == 3863

    def test_first_numeric_part(self) -> None:
        assert _extract_nexus_id("SKSE-30150-v2-2-6") == 30150

    def test_no_id_returns_none(self) -> None:
        assert _extract_nexus_id("JustAName") is None

    def test_single_digit_skipped(self) -> None:
        # Single-digit parts are not considered valid Nexus IDs
        assert _extract_nexus_id("Mod-v1-0") is None

    def test_plain_number(self) -> None:
        assert _extract_nexus_id("Mod-12345") == 12345


# ------------------------------------------------------------------
# _extract_nexus_id: meta.ini path (lines 845-857)
# ------------------------------------------------------------------


class TestExtractNexusIdMeta:
    """Cubre el path de lectura de meta.ini en _extract_nexus_id."""

    def test_reads_nexus_id_from_meta_ini(self, tmp_path: pathlib.Path) -> None:
        """_extract_nexus_id lee modid desde meta.ini cuando no hay ID en el nombre."""
        mod_name = "NoNumbersHere"
        meta_dir = tmp_path / "MO2" / "mods" / mod_name
        meta_dir.mkdir(parents=True)
        (meta_dir / "meta.ini").write_text("[General]\nmodid=42001\n", encoding="utf-8")

        with patch("sky_claw.orchestrator.sync_engine.SystemPaths.modding_root", return_value=tmp_path):
            result = _extract_nexus_id(mod_name)

        assert result == 42001

    def test_meta_ini_zero_modid_returns_none(self, tmp_path: pathlib.Path) -> None:
        """modid=0 en meta.ini no se considera válido → retorna None."""
        mod_name = "ZeroIdMod"
        meta_dir = tmp_path / "MO2" / "mods" / mod_name
        meta_dir.mkdir(parents=True)
        (meta_dir / "meta.ini").write_text("[General]\nmodid=0\n", encoding="utf-8")

        with patch("sky_claw.orchestrator.sync_engine.SystemPaths.modding_root", return_value=tmp_path):
            result = _extract_nexus_id(mod_name)

        assert result is None

    def test_meta_ini_without_general_section_returns_none(self, tmp_path: pathlib.Path) -> None:
        """meta.ini sin sección [General] retorna None sin crash."""
        mod_name = "NoSectionMod"
        meta_dir = tmp_path / "MO2" / "mods" / mod_name
        meta_dir.mkdir(parents=True)
        (meta_dir / "meta.ini").write_text("[OtherSection]\nkey=value\n", encoding="utf-8")

        with patch("sky_claw.orchestrator.sync_engine.SystemPaths.modding_root", return_value=tmp_path):
            result = _extract_nexus_id(mod_name)

        assert result is None

    def test_meta_ini_parse_error_returns_none(self, tmp_path: pathlib.Path) -> None:
        """meta.ini corrupto (sin section headers) → configparser.Error capturado → None.

        Cubre el bloque ``except (OSError, PermissionError, configparser.Error,
        UnicodeDecodeError)`` en ``_extract_nexus_id`` (líneas 852-857).
        ``configparser.read()`` lanza ``MissingSectionHeaderError`` (subclase de
        ``configparser.Error``) para archivos sin encabezados de sección.
        """
        mod_name = "BadIniMod"
        meta_dir = tmp_path / "MO2" / "mods" / mod_name
        meta_dir.mkdir(parents=True)
        # Sin section header → MissingSectionHeaderError al parsear
        (meta_dir / "meta.ini").write_text("key = value_without_section\n", encoding="utf-8")

        with patch("sky_claw.orchestrator.sync_engine.SystemPaths.modding_root", return_value=tmp_path):
            result = _extract_nexus_id(mod_name)

        assert result is None


# ------------------------------------------------------------------
# Fixture: AsyncModRegistry con SQLite temporal
# ------------------------------------------------------------------


@pytest.fixture()
async def adb(tmp_path: pathlib.Path) -> AsyncModRegistry:
    registry = AsyncModRegistry(db_path=tmp_path / "sync_test.db")
    await registry.open()
    yield registry  # type: ignore[misc]
    await registry.close()


# ------------------------------------------------------------------
# SyncEngine — ciclo run() (producer-consumer)
# ------------------------------------------------------------------


class TestSyncEngineRun:
    @pytest.mark.asyncio
    async def test_full_sync_processes_mods(self, tmp_path: pathlib.Path, adb: AsyncModRegistry) -> None:
        mo2 = _make_mo2(
            tmp_path,
            "+ModA-1001-v1\n+ModB-1002-v2\n-ModC-1003-v3\n",
        )
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def fake_fetch(mod_id: int, session: aiohttp.ClientSession) -> dict[str, Any]:
            return _fake_mod_info(mod_id, f"Mod-{mod_id}")

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=2, batch_size=2, max_retries=1),
            fetch_retry_wait=wait_none(),
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        with patch.object(masterlist, "fetch_mod_info", side_effect=fake_fetch):
            result = await engine.run(session, profile="Default")

        assert result.processed == 3
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_network_failure_skips_mod(self, tmp_path: pathlib.Path, adb: AsyncModRegistry) -> None:
        mo2 = _make_mo2(tmp_path, "+FailMod-2001-v1\n+GoodMod-2002-v1\n")
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def flaky_fetch(mod_id: int, session: aiohttp.ClientSession) -> dict[str, Any]:
            if mod_id == 2001:
                raise MasterlistFetchError("API 503")
            return _fake_mod_info(mod_id)

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=1, batch_size=10, max_retries=1),
            fetch_retry_wait=wait_none(),
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        with patch.object(masterlist, "fetch_mod_info", side_effect=flaky_fetch):
            result = await engine.run(session, profile="Default")

        assert result.processed == 1
        assert result.failed == 1
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_no_extractable_id_skips(self, tmp_path: pathlib.Path, adb: AsyncModRegistry) -> None:
        mo2 = _make_mo2(tmp_path, "+NoIdMod\n")
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=1, batch_size=10, max_retries=1),
            fetch_retry_wait=wait_none(),
        )

        mock_fetch = AsyncMock(side_effect=AssertionError("should not be called"))
        session = MagicMock(spec=aiohttp.ClientSession)
        with patch.object(masterlist, "fetch_mod_info", mock_fetch):
            result = await engine.run(session, profile="Default")
        assert result.skipped == 1
        assert result.processed == 0

    @pytest.mark.asyncio
    async def test_empty_modlist(self, tmp_path: pathlib.Path, adb: AsyncModRegistry) -> None:
        mo2 = _make_mo2(tmp_path, "")
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=2, batch_size=5),
            fetch_retry_wait=wait_none(),
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await engine.run(session, profile="Default")
        assert result.processed == 0
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_info_missing_mod_id_skips_mod(self, tmp_path: pathlib.Path, adb: AsyncModRegistry) -> None:
        """Si fetch_mod_info retorna dict sin 'mod_id', el mod se marca como skipped."""
        mo2 = _make_mo2(tmp_path, "+SomeMod-7001-v1\n")
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def fetch_without_mod_id(mod_id: int, session: aiohttp.ClientSession) -> dict[str, Any]:
            return {"name": "SomeMod", "version": "1.0", "author": "auth"}

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=1, batch_size=10, max_retries=1),
            fetch_retry_wait=wait_none(),
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        with patch.object(masterlist, "fetch_mod_info", side_effect=fetch_without_mod_id):
            result = await engine.run(session, profile="Default")

        assert result.skipped == 1
        assert result.processed == 0

    @pytest.mark.asyncio
    async def test_network_error_degrades_gracefully(self, tmp_path: pathlib.Path, adb: AsyncModRegistry) -> None:
        """Errores de red en un mod no impiden el procesamiento de los demás.

        El mod 5001 agota 5 reintentos con ``wait_none()`` (reraise=True) →
        ``_process_batch`` lo captura por-mod → continúa con los siguientes.
        ``result.failed == 1``, ``result.processed == 2``.
        """
        mo2 = _make_mo2(
            tmp_path,
            "+FailMod-5001-v1\n+GoodMod-5002-v1\n+GoodMod-5003-v1\n",
        )
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def selective_fetch(mod_id: int, session: aiohttp.ClientSession) -> dict[str, Any]:
            if mod_id == 5001:
                raise aiohttp.ClientError("network failure")
            return _fake_mod_info(mod_id, f"Mod-{mod_id}")

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=1, batch_size=10, max_retries=1),
            fetch_retry_wait=wait_none(),
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        with patch.object(masterlist, "fetch_mod_info", side_effect=selective_fetch):
            result = await engine.run(session, profile="Default")

        assert result.failed == 1
        assert result.processed == 2
        assert len(result.errors) >= 1


# ------------------------------------------------------------------
# Fail-fast: TaskGroup propagation (lines 664-676)
# ------------------------------------------------------------------


class TestSyncEngineBatchError:
    """Verifica que errores en fetch son capturados al nivel de batch."""

    @pytest.mark.asyncio
    async def test_masterlist_error_in_fetch_counts_as_batch_failure(
        self, tmp_path: pathlib.Path, adb: AsyncModRegistry
    ) -> None:
        """MasterlistFetchError (network error) en fetch → contado en result.failed.

        ``MasterlistFetchError`` SÍ está en los except estrechos de
        ``_process_batch`` y ``_consume``.  Con ``max_retries=1`` y
        ``wait_none()``, tenacity agota 1 intento → ``RetryError`` →
        capturado en per-mod → ``result.failed += 1``.  El engine no crashea.
        """
        mo2 = _make_mo2(tmp_path, "+BugMod-6001-v1\n")
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def bug_fetch(mod_id: int, session: aiohttp.ClientSession) -> dict[str, Any]:
            raise MasterlistFetchError("network error in fetch")

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=1, batch_size=10, max_retries=1),
            fetch_retry_wait=wait_none(),
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        with patch.object(masterlist, "fetch_mod_info", side_effect=bug_fetch):
            result = await engine.run(session, profile="Default")

        assert result.failed == 1
        assert result.processed == 0


# ------------------------------------------------------------------
# _consume: batch-level exception handler (lines 766-777)
# ------------------------------------------------------------------


class TestConsumeExceptionHandling:
    @pytest.mark.asyncio
    async def test_aiohttp_client_error_caught_at_batch_level(
        self, tmp_path: pathlib.Path, adb: AsyncModRegistry
    ) -> None:
        """aiohttp.ClientError en fetch → capturado en _consume batch-level.

        ``aiohttp.ClientError`` SÍ está en el handler estrecho de ``_consume``.
        Cuando escapa de ``_process_batch`` (agotado de ``_safe_fetch_info``),
        el batch-level handler actualiza ``result.failed`` y métricas.
        El worker continúa procesando batches siguientes — no es fail-fast.
        """
        mo2 = _make_mo2(tmp_path, "+TimeoutMod-8001-v1\n")
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def timeout_fetch(mod_id: int, session: aiohttp.ClientSession) -> dict[str, Any]:
            raise aiohttp.ClientConnectionError("simulated connection error")

        engine = SyncEngine(
            mo2=mo2,
            masterlist=masterlist,
            registry=adb,
            config=SyncConfig(worker_count=1, batch_size=10, max_retries=1),
            fetch_retry_wait=wait_none(),
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        with patch.object(masterlist, "fetch_mod_info", side_effect=timeout_fetch):
            result = await engine.run(session, profile="Default")

        # aiohttp.ClientError capturado en per-mod handler → result.failed incrementado
        assert result.failed == 1
        assert result.processed == 0


# ------------------------------------------------------------------
# check_for_updates: TaskGroup except*, task.exception(), updated status
# ------------------------------------------------------------------


class TestCheckForUpdates:
    """Cubre check_for_updates, _check_and_update_mod, y _do_update."""

    @pytest.mark.asyncio
    async def test_empty_registry_returns_empty_payload(self) -> None:
        """Sin mods instalados, check_for_updates retorna payload vacío (early return)."""
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = []

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=mock_registry,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert payload.total_checked == 0
        assert payload.up_to_date_mods == []

    @pytest.mark.asyncio
    async def test_non_installed_mods_excluded(self) -> None:
        """Mods con installed=False son excluidos del ciclo."""
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "UninstalledMod", "nexus_id": 99999, "version": "1.0", "installed": False},
        ]

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=mock_registry,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert payload.total_checked == 0

    @pytest.mark.asyncio
    async def test_up_to_date_mod_in_payload(self) -> None:
        """Mod con la misma versión en Nexus aparece en up_to_date_mods."""
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "SkyUI", "nexus_id": 12021, "version": "5.2", "installed": True},
        ]
        mock_masterlist = AsyncMock()
        mock_masterlist.fetch_mod_info = AsyncMock(
            return_value={
                "mod_id": 12021,
                "name": "SkyUI",
                "version": "5.2",
                "author": "schlangster",
                "category_id": "2",
                "download_url": None,
            }
        )

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=mock_masterlist,
            registry=mock_registry,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert "SkyUI" in payload.up_to_date_mods
        assert payload.total_checked == 1

    @pytest.mark.asyncio
    async def test_newer_version_no_downloader_goes_to_failed(self) -> None:
        """Versión nueva sin downloader configurado → failed_mods."""
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "SKSE64", "nexus_id": 30150, "version": "2.1.5", "installed": True},
        ]
        mock_masterlist = AsyncMock()
        mock_masterlist.fetch_mod_info = AsyncMock(
            return_value={
                "mod_id": 30150,
                "name": "SKSE64",
                "version": "2.2.0",
                "author": "Ian Patterson",
                "category_id": "1",
                "download_url": None,
            }
        )

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=mock_masterlist,
            registry=mock_registry,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert payload.total_checked == 1
        assert len(payload.failed_mods) == 1
        assert "Downloader not configured" in payload.failed_mods[0]["error"]

    @pytest.mark.asyncio
    async def test_taskgroup_propagates_bug_exception(self) -> None:
        """RuntimeError (bug inesperado) en _check_and_update_mod escapa al TaskGroup.

        ``_wrapped_worker`` solo captura errores esperados por-mod:
        ``MasterlistFetchError, CircuitOpenError, RetryError, aiohttp.ClientError,
        ValueError, OSError``.  Un ``RuntimeError`` (bug de programación) NO está en
        esa lista → escapa al ``asyncio.TaskGroup`` → ``ExceptionGroup`` propagada al
        llamador (fail-fast cooperativo).  Contrasta con ``ValueError`` que SÍ es
        capturado y aislado (otros mods siguen procesándose).
        """
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "BugMod", "nexus_id": 77777, "version": "1.0", "installed": True},
        ]

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=mock_registry,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)

        # Simular bug interno en _check_and_update_mod (no una excepción de red)
        engine._check_and_update_mod = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("Simulated internal programming bug")
        )

        # RuntimeError NO está en la lista de _wrapped_worker → TaskGroup lo propaga
        with pytest.raises(ExceptionGroup) as exc_info:
            await engine.check_for_updates(session)

        # El ExceptionGroup contiene el RuntimeError original
        assert any(isinstance(exc, RuntimeError) for exc in exc_info.value.exceptions)
        assert any("Simulated internal programming bug" in str(exc) for exc in exc_info.value.exceptions)

    @pytest.mark.asyncio
    async def test_network_error_degrades_gracefully_in_check(self) -> None:
        """aiohttp.ClientError escapando TaskGroup → except* (lines 413-417) + task.exception() (line 430).

        Sin rollback_manager, la excepción de red escapa de ``_check_and_update_mod``
        al TaskGroup, que la captura con ``except*``.  En el harvesting loop,
        ``task.exception() is not None`` → línea 430 cubierta.
        """
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "BrokenMod", "nexus_id": 11111, "version": "1.0", "installed": True},
        ]
        mock_masterlist = AsyncMock()
        mock_masterlist.fetch_mod_info = AsyncMock(side_effect=aiohttp.ClientError("network failure"))

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=mock_masterlist,
            registry=mock_registry,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert payload.total_checked == 1
        assert len(payload.failed_mods) == 1
        assert "BrokenMod" in payload.failed_mods[0]["name"]

    @pytest.mark.asyncio
    async def test_null_metadata_goes_to_failed_mods(self) -> None:
        """fetch_mod_info retorna None → _check_and_update_mod regresa 'No metadata returned' (line 438)."""
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "NullMod", "nexus_id": 33333, "version": "1.0", "installed": True},
        ]
        mock_masterlist = AsyncMock()
        mock_masterlist.fetch_mod_info = AsyncMock(return_value=None)

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=mock_masterlist,
            registry=mock_registry,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert payload.total_checked == 1
        assert len(payload.failed_mods) == 1
        assert "No metadata returned" in payload.failed_mods[0]["error"]

    @pytest.mark.asyncio
    async def test_hitl_rejection_goes_to_failed_mods(self) -> None:
        """HITL rechaza la descarga → _check_and_update_mod retorna error (lines 469-478)."""
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "HitlMod", "nexus_id": 44444, "version": "1.0", "installed": True},
        ]
        mock_masterlist = AsyncMock()
        mock_masterlist.fetch_mod_info = AsyncMock(
            return_value={
                "mod_id": 44444,
                "name": "HitlMod",
                "version": "2.0",
                "author": "dev",
                "category_id": "1",
                "download_url": "https://example.com/hitlmod.7z",
            }
        )
        mock_file_info = MagicMock()
        mock_file_info.download_url = "https://example.com/hitlmod.7z"

        mock_downloader = AsyncMock()
        mock_downloader.get_file_info = AsyncMock(return_value=mock_file_info)

        mock_hitl = AsyncMock()
        mock_hitl.request_approval = AsyncMock(return_value=Decision.DENIED)

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=mock_masterlist,
            registry=mock_registry,
            downloader=mock_downloader,
            hitl=mock_hitl,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert payload.total_checked == 1
        assert len(payload.failed_mods) == 1
        assert "HITL" in payload.failed_mods[0]["error"]

    @pytest.mark.asyncio
    async def test_updated_mod_in_updated_mods(self) -> None:
        """Mod con versión nueva + downloader → updated_mods (line 439).

        Cubre ``if status == 'updated': payload.updated_mods.append(result)``
        y el path completo de descarga en ``_do_update`` (lines 553-593).
        """
        mock_registry = AsyncMock()
        mock_registry.search_mods.return_value = [
            {"name": "OldMod", "nexus_id": 22222, "version": "1.0", "installed": True},
        ]
        mock_registry.upsert_mod = AsyncMock()
        mock_registry.log_tasks_batch = AsyncMock()

        mock_masterlist = AsyncMock()
        mock_masterlist.fetch_mod_info = AsyncMock(
            return_value={
                "mod_id": 22222,
                "name": "OldMod",
                "version": "2.0",
                "author": "dev",
                "category_id": "1",
                "download_url": "https://example.com/oldmod.7z",
            }
        )

        mock_file_info = MagicMock()
        mock_file_info.download_url = "https://example.com/oldmod.7z"

        mock_downloader = AsyncMock()
        mock_downloader.get_file_info = AsyncMock(return_value=mock_file_info)
        mock_downloader.download = AsyncMock(return_value=pathlib.Path("OldMod-2.0.7z"))

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=mock_masterlist,
            registry=mock_registry,
            downloader=mock_downloader,
            fetch_retry_wait=wait_none(),
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        payload = await engine.check_for_updates(session)

        assert payload.total_checked == 1
        assert len(payload.updated_mods) == 1
        assert payload.updated_mods[0]["name"] == "OldMod"
        assert payload.updated_mods[0]["new_version"] == "2.0"


# ------------------------------------------------------------------
# Shutdown lifecycle (lines 234-245)
# ------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_no_tasks_completes_cleanly(self) -> None:
        """shutdown() con cero tareas pendientes completa sin error."""
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
        )
        await engine.shutdown()
        assert engine._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_pending_download_tasks(self) -> None:
        """shutdown() cancela tareas de descarga pendientes."""
        registry = AsyncMock()
        registry.log_tasks_batch.return_value = None
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=registry,
        )

        async def long_download() -> None:
            await asyncio.sleep(9999)

        engine.enqueue_download(long_download(), context="slow-test")
        await asyncio.sleep(0)

        assert len(engine._download_tasks) > 0
        await engine.shutdown()
        assert len(engine._download_tasks) == 0


# ------------------------------------------------------------------
# execute_file_operation & _passive_pruning (sin rollback_manager)
# ------------------------------------------------------------------


class TestExecuteFileOperation:
    @pytest.mark.asyncio
    async def test_no_rollback_manager_runs_operation_directly(self, tmp_path: pathlib.Path) -> None:
        """Sin rollback_manager, execute_file_operation delega directo y retorna el resultado."""
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
        )
        assert engine._rollback_manager is None

        async def simple_op() -> str:
            return "done"

        result = await engine.execute_file_operation(
            operation_type=None,  # type: ignore[arg-type]
            target_path=tmp_path / "dummy.txt",
            operation=simple_op(),
        )
        assert result == "done"

    @pytest.mark.asyncio
    async def test_passive_pruning_no_rollback_manager_noop(self) -> None:
        """_passive_pruning retorna inmediatamente cuando no hay rollback_manager."""
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
        )
        await engine._passive_pruning()

    def test_get_max_backup_size_bytes_uses_config(self) -> None:
        """_get_max_backup_size_bytes retorna rollback_max_size_mb × 1024²."""
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
            config=SyncConfig(rollback_max_size_mb=256),
        )
        assert engine._get_max_backup_size_bytes() == 256 * 1024 * 1024


# ------------------------------------------------------------------
# SyncMetrics: concurrencia y record_error
# ------------------------------------------------------------------


class TestSyncMetricsConcurrency:
    @pytest.mark.asyncio
    async def test_sync_metrics_is_thread_safe(self) -> None:
        """100 tareas concurrentes actualizan SyncMetrics sin pérdida de contadores.

        Todas las tareas compiten por el ``asyncio.Lock`` interno de
        ``increment_error_type``.  El contador final debe ser exactamente 100.
        """
        metrics = SyncMetrics()
        n = 100

        await asyncio.gather(*[metrics.increment_error_type("TestError") for _ in range(n)])

        total = await metrics.get_error_count()
        assert total == n, f"Contador esperado {n}, obtenido {total} (race condition)"
        error_types = await metrics.get_error_types()
        assert error_types.get("TestError", 0) == n

    @pytest.mark.asyncio
    async def test_record_error_increments_by_exception_type(self) -> None:
        """record_error incrementa por tipo de excepción real (lines 163-165)."""
        metrics = SyncMetrics()

        await metrics.record_error(ValueError("test"))
        await metrics.record_error(RuntimeError("other"))
        await metrics.record_error(ValueError("second"))

        assert await metrics.get_error_count() == 3
        types = await metrics.get_error_types()
        assert types["ValueError"] == 2
        assert types["RuntimeError"] == 1


# ------------------------------------------------------------------
# Model contracts (Pydantic v2 strict)
# ------------------------------------------------------------------


class TestEnqueueDownloadRegistry:
    """Cubre el handler de fallo del registry en enqueue_download (lines 752-753)."""

    @pytest.mark.asyncio
    async def test_registry_failure_is_logged_not_raised(self) -> None:
        """Si log_tasks_batch falla, el error se loguea pero no se propaga (lines 752-753)."""
        registry = AsyncMock()
        registry.log_tasks_batch = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=registry,
        )

        async def failing_download() -> None:
            raise ValueError("download error")

        engine.enqueue_download(failing_download(), context="registry-fail-test")
        # Esperar que la tarea corra
        await asyncio.sleep(0.1)

        # El engine sigue funcionando — no hubo excepción no capturada
        assert await engine.metrics.get_error_count() >= 1


class TestPassivePruningWithRollback:
    """Cubre _passive_pruning cuando rollback_manager está configurado (lines 310-331)."""

    @pytest.mark.asyncio
    async def test_passive_pruning_stats_under_limit_noop(self) -> None:
        """Si total_size_bytes <= max, no hay pruning."""
        mock_rm = MagicMock()
        stats = MagicMock()
        stats.total_size_bytes = 0
        # Use proxy method (get_snapshot_stats), not the private _snapshots attribute
        mock_rm.get_snapshot_stats = AsyncMock(return_value=stats)
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
            rollback_manager=mock_rm,
        )
        await engine._passive_pruning()
        mock_rm.get_snapshot_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_passive_pruning_exception_is_logged_not_raised(self) -> None:
        """OSError en get_snapshot_stats es capturada y logueada."""
        mock_rm = MagicMock()
        mock_rm.get_snapshot_stats = AsyncMock(side_effect=OSError("disk error"))
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
            rollback_manager=mock_rm,
        )
        await engine._passive_pruning()  # No debe levantar


class TestConsumeCancelledError:
    """Cubre la rama asyncio.CancelledError en _consume (line 800)."""

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_from_process_batch(self) -> None:
        """CancelledError en _process_batch se re-lanza inmediatamente (line 800)."""
        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
        )
        queue: asyncio.Queue[list[tuple[str, bool]] | None] = asyncio.Queue()
        await queue.put([("mod1", True)])  # batch real, no POISON
        semaphore = asyncio.Semaphore(2)
        result = SyncResult()
        session = MagicMock(spec=aiohttp.ClientSession)
        engine._process_batch = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await engine._consume(queue, session, semaphore, result)


class TestSyncResult:
    def test_defaults(self) -> None:
        r = SyncResult()
        assert r.processed == 0
        assert r.failed == 0
        assert r.skipped == 0
        assert r.errors == []

    def test_model_dump_json_serializable(self) -> None:
        """model_dump(mode='json') produce un dict serializable a JSON (Pydantic v2)."""
        import json

        r = SyncResult(processed=5, failed=1, errors=["mod: error"])
        dumped = r.model_dump(mode="json")
        json.dumps(dumped)
        assert dumped["processed"] == 5


class TestSyncConfig:
    def test_defaults(self) -> None:
        c = SyncConfig()
        assert c.worker_count == 4
        assert c.batch_size == 20
        assert c.max_retries == 5

    def test_custom(self) -> None:
        c = SyncConfig(worker_count=8, batch_size=50)
        assert c.worker_count == 8
        assert c.batch_size == 50

    def test_frozen_immutable(self) -> None:
        """SyncConfig es frozen=True — mutaciones lanzan ValidationError (Pydantic v2)."""
        from pydantic import ValidationError

        c = SyncConfig()
        with pytest.raises((ValidationError, TypeError, AttributeError)):
            c.worker_count = 99  # type: ignore[misc]
