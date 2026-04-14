"""Tests for ToolsInstaller, local_config, and setup_tools tool."""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from sky_claw.security.hitl import HITLGuard
from sky_claw.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.security.path_validator import PathValidator
from sky_claw.tools_installer import (
    ToolInstallError,
    ToolsInstaller,
    find_exe_in_dir,
    scan_common_paths,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def validator(tmp_path: pathlib.Path) -> PathValidator:
    return PathValidator(roots=[tmp_path])


@pytest.fixture
def gateway() -> NetworkGateway:
    return NetworkGateway(EgressPolicy(block_private_ips=False))


@pytest.fixture
def hitl_guard() -> HITLGuard:
    return HITLGuard(notify_fn=None, timeout=5)


@pytest.fixture
def installer(
    hitl_guard: HITLGuard, gateway: NetworkGateway, validator: PathValidator
) -> ToolsInstaller:
    return ToolsInstaller(hitl=hitl_guard, gateway=gateway, path_validator=validator)


def _github_release_json(
    tag: str = "0.22.4",
    asset_name: str = "loot_0.22.4-win64.zip",
    size: int = 50_000_000,
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": asset_name,
                "size": size,
                "browser_download_url": f"https://github.com/loot/loot/releases/download/{tag}/{asset_name}",
            },
            {
                "name": "loot_0.22.4-linux.tar.gz",
                "size": 40_000_000,
                "browser_download_url": f"https://github.com/loot/loot/releases/download/{tag}/loot_0.22.4-linux.tar.gz",
            },
        ],
    }


def _xedit_release_json(
    tag: str = "4.1.5",
    asset_name: str = "SSEEdit_4.1.5.7z",
    size: int = 30_000_000,
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": asset_name,
                "size": size,
                "browser_download_url": f"https://github.com/TES5Edit/TES5Edit/releases/download/{tag}/{asset_name}",
            },
        ],
    }


# ---------------------------------------------------------------------------
# find_exe_in_dir / scan_common_paths
# ---------------------------------------------------------------------------


class TestFindExeInDir:
    def test_finds_exe_in_flat_dir(self, tmp_path: pathlib.Path) -> None:
        exe = tmp_path / "loot.exe"
        exe.write_text("fake", encoding="utf-8")
        assert find_exe_in_dir(tmp_path, "loot.exe") == exe

    def test_finds_exe_in_subdir(self, tmp_path: pathlib.Path) -> None:
        subdir = tmp_path / "LOOT" / "bin"
        subdir.mkdir(parents=True)
        exe = subdir / "loot.exe"
        exe.write_text("fake", encoding="utf-8")
        assert find_exe_in_dir(tmp_path, "loot.exe") == exe

    def test_returns_none_when_not_found(self, tmp_path: pathlib.Path) -> None:
        assert find_exe_in_dir(tmp_path, "loot.exe") is None

    def test_returns_none_for_nonexistent_dir(self) -> None:
        assert find_exe_in_dir(pathlib.Path("/nonexistent"), "loot.exe") is None


class TestScanCommonPaths:
    def test_finds_in_common_path(self, tmp_path: pathlib.Path) -> None:
        loot_dir = tmp_path / "LOOT"
        loot_dir.mkdir()
        (loot_dir / "loot.exe").write_text("fake", encoding="utf-8")
        result = scan_common_paths((loot_dir,), "loot.exe")
        assert result is not None
        assert result.name == "loot.exe"

    def test_returns_none_when_no_match(self) -> None:
        result = scan_common_paths(
            (pathlib.Path("/nonexistent1"), pathlib.Path("/nonexistent2")),
            "loot.exe",
        )
        assert result is None


# ---------------------------------------------------------------------------
# ToolsInstaller.ensure_loot
# ---------------------------------------------------------------------------


class TestEnsureLoot:
    @pytest.mark.asyncio
    async def test_returns_existing_without_download(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """When loot.exe already exists, return it immediately."""
        exe = tmp_path / "loot.exe"
        exe.write_text("fake", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_loot(tmp_path, session)

        assert result.already_existed is True
        assert result.exe_path == exe
        assert result.tool_name == "LOOT"

    @pytest.mark.asyncio
    async def test_downloads_and_extracts_when_missing(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """When loot.exe is absent, download from GitHub and extract."""
        import zipfile

        # Prepare a fake zip containing loot.exe.
        zip_path = tmp_path / "_fake_asset.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("LOOT/loot.exe", "fake-binary")

        zip_bytes = zip_path.read_bytes()
        zip_path.unlink()

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _github_release_json(size=len(zip_bytes))

        # Mock aiohttp responses.
        mock_api_resp = AsyncMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)
        mock_api_resp.__aenter__ = AsyncMock(return_value=mock_api_resp)
        mock_api_resp.__aexit__ = AsyncMock(return_value=False)

        # Simulate streaming download.
        async def _iter_chunks(size: int):
            yield zip_bytes

        mock_dl_resp = AsyncMock()
        mock_dl_resp.status = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_dl_resp.content = MagicMock()
        mock_dl_resp.content.iter_chunked = _iter_chunks
        mock_dl_resp.__aenter__ = AsyncMock(return_value=mock_dl_resp)
        mock_dl_resp.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def _mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_api_resp  # GitHub API
            return mock_dl_resp  # Asset download

        session = MagicMock(spec=aiohttp.ClientSession)
        session.get = MagicMock(side_effect=_mock_get)

        # Auto-approve HITL.
        async def _auto_approve() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-loot-0.22.4", approved=True)

        asyncio.create_task(_auto_approve())

        result = await installer.ensure_loot(install_dir, session)

        assert result.already_existed is False
        assert result.tool_name == "LOOT"
        assert result.version == "0.22.4"
        assert result.exe_path.name == "loot.exe"
        assert result.exe_path.exists()

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """When operator denies, raise ToolInstallError."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _github_release_json()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=release_json)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock(spec=aiohttp.ClientSession)
        session.get = MagicMock(return_value=mock_resp)

        async def _auto_deny() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-loot-0.22.4", approved=False)

        asyncio.create_task(_auto_deny())

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_loot(install_dir, session)

    @pytest.mark.asyncio
    async def test_github_api_failure_raises(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """When GitHub API fails, raise ToolInstallError."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        mock_resp = AsyncMock()
        mock_resp.status = 403
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock(spec=aiohttp.ClientSession)
        session.get = MagicMock(return_value=mock_resp)

        with pytest.raises(ToolInstallError, match="403"):
            await installer.ensure_loot(install_dir, session)


# ---------------------------------------------------------------------------
# ToolsInstaller.ensure_xedit
# ---------------------------------------------------------------------------


class TestEnsureXedit:
    @pytest.mark.asyncio
    async def test_returns_existing_without_download(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        exe = tmp_path / "SSEEdit.exe"
        exe.write_text("fake", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_xedit(tmp_path, session)

        assert result.already_existed is True
        assert result.exe_path == exe
        assert result.tool_name == "SSEEdit"

    @pytest.mark.asyncio
    async def test_downloads_and_extracts_when_missing(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        import zipfile

        zip_path = tmp_path / "_fake_asset.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("SSEEdit/SSEEdit.exe", "fake-binary")

        zip_bytes = zip_path.read_bytes()
        zip_path.unlink()

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        # Use a .zip asset name to match.
        release_json = _xedit_release_json(
            asset_name="SSEEdit_4.1.5.zip", size=len(zip_bytes)
        )

        mock_api_resp = AsyncMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)
        mock_api_resp.__aenter__ = AsyncMock(return_value=mock_api_resp)
        mock_api_resp.__aexit__ = AsyncMock(return_value=False)

        async def _iter_chunks(size: int):
            yield zip_bytes

        mock_dl_resp = AsyncMock()
        mock_dl_resp.status = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_dl_resp.content = MagicMock()
        mock_dl_resp.content.iter_chunked = _iter_chunks
        mock_dl_resp.__aenter__ = AsyncMock(return_value=mock_dl_resp)
        mock_dl_resp.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def _mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_api_resp
            return mock_dl_resp

        session = MagicMock(spec=aiohttp.ClientSession)
        session.get = MagicMock(side_effect=_mock_get)

        async def _auto_approve() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-xedit-4.1.5", approved=True)

        asyncio.create_task(_auto_approve())

        result = await installer.ensure_xedit(install_dir, session)

        assert result.already_existed is False
        assert result.tool_name == "SSEEdit"
        assert result.version == "4.1.5"
        assert result.exe_path.name == "SSEEdit.exe"

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _xedit_release_json(asset_name="SSEEdit_4.1.5.zip")
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=release_json)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock(spec=aiohttp.ClientSession)
        session.get = MagicMock(return_value=mock_resp)

        async def _auto_deny() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-xedit-4.1.5", approved=False)

        asyncio.create_task(_auto_deny())

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_xedit(install_dir, session)


# ---------------------------------------------------------------------------
# local_config
# ---------------------------------------------------------------------------


class TestLocalConfig:
    def test_load_defaults_when_file_missing(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local_config import load

        cfg = load(tmp_path / "nonexistent.json")
        assert cfg.first_run is True
        assert cfg.loot_exe is None

    def test_save_and_load_roundtrip(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local_config import LocalConfig, load, save

        cfg = LocalConfig(
            loot_exe="C:/LOOT/loot.exe",
            xedit_exe="C:/SSEEdit/SSEEdit.exe",
            first_run=False,
        )
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        assert loaded.loot_exe == "C:/LOOT/loot.exe"
        assert loaded.xedit_exe == "C:/SSEEdit/SSEEdit.exe"
        assert loaded.first_run is False

    def test_load_handles_corrupt_json(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local_config import load

        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        cfg = load(path)
        assert cfg.first_run is True


# ---------------------------------------------------------------------------
# setup_tools tool (via AsyncToolRegistry)
# ---------------------------------------------------------------------------


class TestSetupToolsTool:
    @pytest.mark.asyncio
    async def test_setup_tools_installs_both(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.db.async_registry import AsyncModRegistry
        from sky_claw.mo2.vfs import MO2Controller
        from sky_claw.orchestrator.sync_engine import SyncEngine
        from sky_claw.scraper.masterlist import MasterlistClient

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text(
            "", encoding="utf-8"
        )

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        guard = HITLGuard(notify_fn=None, timeout=5)
        ti = ToolsInstaller(hitl=guard, gateway=gw, path_validator=validator)

        install_dir = tmp_path / "tools"
        install_dir.mkdir()

        # Pre-create the executables so ensure_loot/ensure_xedit find them.
        loot_dir = install_dir / "LOOT"
        loot_dir.mkdir()
        (loot_dir / "loot.exe").write_text("fake", encoding="utf-8")

        xedit_dir = install_dir / "SSEEdit"
        xedit_dir.mkdir()
        (xedit_dir / "SSEEdit.exe").write_text("fake", encoding="utf-8")

        registry = AsyncToolRegistry(
            registry=db,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=guard,
            tools_installer=ti,
            install_dir=install_dir,
        )

        result_str = await registry.execute("setup_tools", {"tools": ["loot", "xedit"]})
        result = json.loads(result_str)

        assert result["loot"]["status"] == "already_installed"
        assert result["xedit"]["status"] == "already_installed"
        assert "loot.exe" in result["loot"]["exe_path"]
        assert "SSEEdit.exe" in result["xedit"]["exe_path"]

        await db.close()

    @pytest.mark.asyncio
    async def test_setup_tools_no_installer_returns_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.db.async_registry import AsyncModRegistry
        from sky_claw.mo2.vfs import MO2Controller
        from sky_claw.orchestrator.sync_engine import SyncEngine
        from sky_claw.scraper.masterlist import MasterlistClient

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text(
            "", encoding="utf-8"
        )

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        registry = AsyncToolRegistry(
            registry=db,
            mo2=mo2,
            sync_engine=sync_engine,
            # tools_installer=None (default)
        )

        result_str = await registry.execute("setup_tools", {})
        result = json.loads(result_str)
        assert "error" in result
        assert "not configured" in result["error"]

        await db.close()

    @pytest.mark.asyncio
    async def test_setup_tools_unknown_tool(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.db.async_registry import AsyncModRegistry
        from sky_claw.mo2.vfs import MO2Controller
        from sky_claw.orchestrator.sync_engine import SyncEngine
        from sky_claw.scraper.masterlist import MasterlistClient

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text(
            "", encoding="utf-8"
        )

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        guard = HITLGuard(notify_fn=None, timeout=5)
        ti = ToolsInstaller(hitl=guard, gateway=gw, path_validator=validator)

        registry = AsyncToolRegistry(
            registry=db,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=guard,
            tools_installer=ti,
            install_dir=tmp_path,
        )

        result_str = await registry.execute("setup_tools", {"tools": ["unknown_tool"]})
        result = json.loads(result_str)
        assert "unknown_tool" in result
        assert "error" in result["unknown_tool"]

        await db.close()


# ---------------------------------------------------------------------------
# AppContext wiring — tools_installer present
# ---------------------------------------------------------------------------


class TestAppContextToolsInstaller:
    @pytest.mark.asyncio
    async def test_tools_installer_wired(self, tmp_path: pathlib.Path) -> None:
        import argparse

        from sky_claw.__main__ import AppContext

        args = argparse.Namespace(
            db_path=tmp_path / "test.db",
            mo2_root=tmp_path,
            loot_exe=pathlib.Path("loot.exe"),
            xedit_exe=None,
            operator_chat_id=None,
            staging_dir=tmp_path / "staging",
            provider=None,
            install_dir=tmp_path / "tools",
        )

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "test-key",
                "NEXUS_API_KEY": "",
                "TELEGRAM_BOT_TOKEN": "",
            },
        ):
            ctx = AppContext(args)
            await ctx.start()

        try:
            assert ctx.tools_installer is not None
        finally:
            await ctx.stop()
