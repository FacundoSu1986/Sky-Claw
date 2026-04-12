"""Tests for sky_claw.orchestrator.sync_engine."""

from __future__ import annotations

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
    SyncResult,
    _extract_nexus_id,
)
from sky_claw.scraper.masterlist import MasterlistClient, MasterlistFetchError
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
# SyncEngine integration
# ------------------------------------------------------------------


@pytest.fixture()
async def adb(tmp_path: pathlib.Path) -> AsyncModRegistry:
    registry = AsyncModRegistry(db_path=tmp_path / "sync_test.db")
    await registry.open()
    yield registry  # type: ignore[misc]
    await registry.close()


class TestSyncEngineRun:
    @pytest.mark.asyncio
    async def test_full_sync_processes_mods(
        self, tmp_path: pathlib.Path, adb: AsyncModRegistry
    ) -> None:
        mo2 = _make_mo2(
            tmp_path,
            "+ModA-1001-v1\n+ModB-1002-v2\n-ModC-1003-v3\n",
        )
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def fake_fetch(
            mod_id: int, session: aiohttp.ClientSession
        ) -> dict[str, Any]:
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
    async def test_network_failure_skips_mod(
        self, tmp_path: pathlib.Path, adb: AsyncModRegistry
    ) -> None:
        mo2 = _make_mo2(tmp_path, "+FailMod-2001-v1\n+GoodMod-2002-v1\n")
        gw = NetworkGateway()
        masterlist = MasterlistClient(gateway=gw, api_key="fake")

        async def flaky_fetch(
            mod_id: int, session: aiohttp.ClientSession
        ) -> dict[str, Any]:
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
    async def test_no_extractable_id_skips(
        self, tmp_path: pathlib.Path, adb: AsyncModRegistry
    ) -> None:
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
    async def test_empty_modlist(
        self, tmp_path: pathlib.Path, adb: AsyncModRegistry
    ) -> None:
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


class TestSyncResult:
    def test_defaults(self) -> None:
        r = SyncResult()
        assert r.processed == 0
        assert r.failed == 0
        assert r.skipped == 0
        assert r.errors == []


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
