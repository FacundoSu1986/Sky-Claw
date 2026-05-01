"""Tests for sky_claw.antigravity.agent.tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pydantic
import pytest

from sky_claw.antigravity.agent.tools import (
    AsyncToolRegistry,
    InstallModParams,
    ProfileParams,
    SearchModParams,
)
from sky_claw.antigravity.db.async_registry import AsyncModRegistry
from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine
from sky_claw.antigravity.scraper.masterlist import MasterlistClient
from sky_claw.antigravity.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.mo2.vfs import MO2Controller

if TYPE_CHECKING:
    import pathlib

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_mo2(tmp_path: pathlib.Path, lines: str) -> MO2Controller:
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text(lines, encoding="utf-8")
    validator = PathValidator(roots=[tmp_path])
    return MO2Controller(tmp_path, path_validator=validator)


@pytest.fixture()
async def adb(tmp_path: pathlib.Path) -> AsyncModRegistry:
    registry = AsyncModRegistry(db_path=tmp_path / "test_tools.db")
    await registry.open()
    yield registry  # type: ignore[misc]
    await registry.close()


@pytest.fixture()
def mo2(tmp_path: pathlib.Path) -> MO2Controller:
    return _make_mo2(
        tmp_path,
        "+SKSE-30150-v2-2-6\n-DisabledMod-9999\n+SkyUI-3863-v5-2\n",
    )


@pytest.fixture()
def tool_registry(
    adb: AsyncModRegistry,
    mo2: MO2Controller,
    tmp_path: pathlib.Path,
) -> AsyncToolRegistry:
    gw = NetworkGateway(EgressPolicy(block_private_ips=False))
    masterlist = MasterlistClient(gateway=gw, api_key="fake")
    engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=adb)
    return AsyncToolRegistry(
        registry=adb,
        mo2=mo2,
        sync_engine=engine,
        loot_exe=None,
    )


# ------------------------------------------------------------------
# Pydantic parameter validation
# ------------------------------------------------------------------


class TestPydanticModels:
    def test_search_mod_params_valid(self) -> None:
        p = SearchModParams(mod_name="SKSE")
        assert p.mod_name == "SKSE"

    def test_search_mod_params_empty_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            SearchModParams(mod_name="")

    def test_profile_params_valid(self) -> None:
        p = ProfileParams(profile="Default")
        assert p.profile == "Default"

    def test_profile_params_empty_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ProfileParams(profile="")

    def test_install_mod_params_valid(self) -> None:
        p = InstallModParams(nexus_id=1234, version="1.0")
        assert p.nexus_id == 1234

    def test_install_mod_params_zero_nexus_id(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            InstallModParams(nexus_id=0, version="1.0")

    def test_install_mod_params_negative_nexus_id(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            InstallModParams(nexus_id=-1, version="1.0")

    def test_install_mod_params_empty_version(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            InstallModParams(nexus_id=1, version="")


# ------------------------------------------------------------------
# AsyncToolRegistry
# ------------------------------------------------------------------


class TestToolRegistry:
    def test_tool_schemas_returns_all(
        self,
        tool_registry: AsyncToolRegistry,
    ) -> None:
        schemas = tool_registry.tool_schemas()
        # Validate that all registered tools have a non-empty schema and description
        assert len(schemas) > 0
        for s in schemas:
            assert "name" in s
            assert "description" in s
            assert "input_schema" in s
            assert isinstance(s["input_schema"], dict)
            assert "type" in s["input_schema"]

        # Ensure essential mod management tools are present
        names = {s["name"] for s in schemas}
        essential = {
            "search_mod",
            "check_load_order",
            "detect_conflicts",
            "run_loot_sort",
            "install_mod",
            "setup_tools",
        }
        assert essential.issubset(names)

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_raises(self, tool_registry: AsyncToolRegistry) -> None:
        with pytest.raises(KeyError, match="Unknown tool"):
            await tool_registry.execute("nonexistent", {})


# ------------------------------------------------------------------
# search_mod
# ------------------------------------------------------------------


class TestSearchMod:
    @pytest.mark.asyncio
    async def test_search_mod_no_results(self, tool_registry: AsyncToolRegistry) -> None:
        result = json.loads(await tool_registry.execute("search_mod", {"mod_name": "SKSE"}))
        assert result["matches"] == []

    @pytest.mark.asyncio
    async def test_search_mod_with_results(self, tool_registry: AsyncToolRegistry, adb: AsyncModRegistry) -> None:
        await adb.upsert_mod(nexus_id=30150, name="SKSE", version="2.2.6")
        result = json.loads(await tool_registry.execute("search_mod", {"mod_name": "SKSE"}))
        assert len(result["matches"]) == 1
        assert result["matches"][0]["name"] == "SKSE"
        assert result["matches"][0]["version"] == "2.2.6"

    @pytest.mark.asyncio
    async def test_search_mod_partial_match(self, tool_registry: AsyncToolRegistry, adb: AsyncModRegistry) -> None:
        await adb.upsert_mod(nexus_id=100, name="SkyUI", version="5.2")
        await adb.upsert_mod(nexus_id=101, name="Sky Tweaks", version="1.0")
        result = json.loads(await tool_registry.execute("search_mod", {"mod_name": "Sky"}))
        assert len(result["matches"]) == 2

    @pytest.mark.asyncio
    async def test_search_mod_empty_name_rejected(self, tool_registry: AsyncToolRegistry) -> None:
        with pytest.raises(pydantic.ValidationError):
            await tool_registry.execute("search_mod", {"mod_name": ""})


# ------------------------------------------------------------------
# check_load_order
# ------------------------------------------------------------------


class TestCheckLoadOrder:
    @pytest.mark.asyncio
    async def test_check_load_order(self, tool_registry: AsyncToolRegistry) -> None:
        result = json.loads(await tool_registry.execute("check_load_order", {"profile": "Default"}))
        assert result["profile"] == "Default"
        entries = result["load_order"]
        assert len(entries) == 3
        assert entries[0]["name"] == "SKSE-30150-v2-2-6"
        assert entries[0]["enabled"] is True
        assert entries[1]["name"] == "DisabledMod-9999"
        assert entries[1]["enabled"] is False

    @pytest.mark.asyncio
    async def test_check_load_order_empty_profile_rejected(self, tool_registry: AsyncToolRegistry) -> None:
        with pytest.raises(pydantic.ValidationError):
            await tool_registry.execute("check_load_order", {"profile": ""})


# ------------------------------------------------------------------
# detect_conflicts
# ------------------------------------------------------------------


class TestDetectConflicts:
    @pytest.mark.asyncio
    async def test_detect_no_conflicts(self, tool_registry: AsyncToolRegistry) -> None:
        result = json.loads(await tool_registry.execute("detect_conflicts", {"profile": "Default"}))
        assert result["conflicts"] == []

    @pytest.mark.asyncio
    async def test_detect_missing_master(self, tool_registry: AsyncToolRegistry, adb: AsyncModRegistry) -> None:
        mod_id = await adb.upsert_mod(nexus_id=30150, name="SKSE-30150-v2-2-6", version="2.2.6")
        await adb.insert_deps_batch([(mod_id, 99999, "MissingMod")])
        result = json.loads(await tool_registry.execute("detect_conflicts", {"profile": "Default"}))
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["missing_master_nexus_id"] == 99999


# ------------------------------------------------------------------
# run_loot_sort
# ------------------------------------------------------------------


class TestRunLootSort:
    @pytest.mark.asyncio
    async def test_run_loot_sort_missing_exe(self, tool_registry: AsyncToolRegistry) -> None:
        result = json.loads(await tool_registry.execute("run_loot_sort", {"profile": "Default"}))
        assert "error" in result
        assert "not configured" in result["error"] or "not found" in result["error"]


# ------------------------------------------------------------------
# install_mod
# ------------------------------------------------------------------


class TestInstallMod:
    @pytest.mark.asyncio
    async def test_install_mod_registers(self, tool_registry: AsyncToolRegistry, adb: AsyncModRegistry) -> None:
        result = json.loads(await tool_registry.execute("install_mod", {"nexus_id": 5555, "version": "1.2.3"}))
        assert result["status"] == "registered"
        assert result["nexus_id"] == 5555
        assert result["version"] == "1.2.3"

        row = await adb.get_mod(5555)
        assert row is not None

    @pytest.mark.asyncio
    async def test_install_mod_invalid_params(self, tool_registry: AsyncToolRegistry) -> None:
        with pytest.raises(pydantic.ValidationError):
            await tool_registry.execute("install_mod", {"nexus_id": -1, "version": "1.0"})


# ------------------------------------------------------------------
# search_mod — special characters (SQL injection / LIKE wildcards)
# ------------------------------------------------------------------


class TestSearchModSpecialChars:
    @pytest.mark.asyncio
    async def test_percent_in_name(self, tool_registry: AsyncToolRegistry, adb: AsyncModRegistry) -> None:
        await adb.upsert_mod(nexus_id=200, name="100%MorePower", version="1.0")
        await adb.upsert_mod(nexus_id=201, name="NormalMod", version="1.0")
        result = json.loads(await tool_registry.execute("search_mod", {"mod_name": "100%"}))
        assert len(result["matches"]) == 1
        assert result["matches"][0]["name"] == "100%MorePower"

    @pytest.mark.asyncio
    async def test_underscore_in_name(self, tool_registry: AsyncToolRegistry, adb: AsyncModRegistry) -> None:
        await adb.upsert_mod(nexus_id=300, name="My_Mod", version="1.0")
        await adb.upsert_mod(nexus_id=301, name="MyXMod", version="1.0")
        result = json.loads(await tool_registry.execute("search_mod", {"mod_name": "My_"}))
        # Should only match "My_Mod", not "MyXMod" (since _ is escaped)
        assert len(result["matches"]) == 1
        assert result["matches"][0]["name"] == "My_Mod"

    @pytest.mark.asyncio
    async def test_single_quote_in_name(self, tool_registry: AsyncToolRegistry, adb: AsyncModRegistry) -> None:
        await adb.upsert_mod(nexus_id=400, name="Skyrim's Edge", version="1.0")
        result = json.loads(await tool_registry.execute("search_mod", {"mod_name": "Skyrim's"}))
        assert len(result["matches"]) == 1


# ------------------------------------------------------------------
# detect_conflicts — large dataset
# ------------------------------------------------------------------


class TestDetectConflictsLargeDataset:
    @pytest.mark.asyncio
    async def test_500_mods_with_missing_masters(self, tmp_path: pathlib.Path, adb: AsyncModRegistry) -> None:
        # Build modlist with 500 enabled mods.
        lines = "".join(f"+Mod-{i}\n" for i in range(500))
        mo2 = _make_mo2(tmp_path, lines)

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=adb)
        registry = AsyncToolRegistry(
            registry=adb,
            mo2=mo2,
            sync_engine=engine,
            loot_exe=None,
        )

        # Insert 500 mods with one missing-master dep each.
        for i in range(500):
            mod_id = await adb.upsert_mod(nexus_id=10000 + i, name=f"Mod-{i}", version="1.0")
            await adb.insert_deps_batch([(mod_id, 90000 + i, f"MissingDep-{i}")])

        result = json.loads(await registry.execute("detect_conflicts", {"profile": "Default"}))
        assert len(result["conflicts"]) == 500
