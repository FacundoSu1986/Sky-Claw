"""Tests for ToolsInstaller, local_config, and setup_tools tool."""

from __future__ import annotations

import asyncio
import json
import pathlib
import zipfile
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.app.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.app.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.tools_installer import (
    ToolInstallError,
    ToolsInstaller,
    _extract_zip_safe,
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
def installer(hitl_guard: HITLGuard, gateway: NetworkGateway, validator: PathValidator) -> ToolsInstaller:
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
                "url": "https://api.github.com/repos/loot/loot/releases/assets/1001",
                "browser_download_url": f"https://github.com/loot/loot/releases/download/{tag}/{asset_name}",
            },
            {
                "name": "loot_0.22.4-linux.tar.gz",
                "size": 40_000_000,
                "url": "https://api.github.com/repos/loot/loot/releases/assets/1002",
                "browser_download_url": f"https://github.com/loot/loot/releases/download/{tag}/loot_0.22.4-linux.tar.gz",
            },
        ],
    }


def _xedit_release_json(
    tag: str = "4.1.5",
    asset_name: str = "xEdit_4.1.5.7z",
    size: int = 30_000_000,
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": asset_name,
                "size": size,
                "url": "https://api.github.com/repos/TES5Edit/TES5Edit/releases/assets/2001",
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
    async def test_approval_uses_download_category(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """La aprobación de descarga se etiqueta con category='download' para que el puente
        HITL de la GUI pueda retenerla en el modal (Follow-up C) en vez de caer a Telegram."""
        from unittest.mock import AsyncMock

        from sky_claw.app.security.hitl import Decision
        from sky_claw.local.tools_installer import ReleaseAsset

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        asset = ReleaseAsset(
            name="loot_0.22.4-win64.zip",
            size=1,
            download_url="https://api.github.com/x",
            browser_download_url="https://github.com/loot/loot/releases/download/0.22.4/loot.zip",
        )
        installer._find_github_asset = AsyncMock(return_value=(asset, "0.22.4"))  # type: ignore[method-assign]
        installer._hitl.request_approval = AsyncMock(return_value=Decision.DENIED)  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)
        with pytest.raises(ToolInstallError):
            await installer.ensure_loot(install_dir, session)

        assert installer._hitl.request_approval.call_args.kwargs["category"] == "download"

    @pytest.mark.asyncio
    async def test_returns_existing_without_download(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando loot.exe ya existe, lo devuelve inmediatamente sin descargar."""
        exe = tmp_path / "loot.exe"
        exe.write_text("fake", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_loot(tmp_path, session)

        assert result.already_existed is True
        assert result.exe_path == exe
        assert result.tool_name == "LOOT"

    @pytest.mark.asyncio
    async def test_downloads_and_extracts_when_missing(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando loot.exe está ausente, descarga de GitHub y extrae el archivo."""
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
        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)
        mock_api_resp.__aenter__ = AsyncMock(return_value=mock_api_resp)
        mock_api_resp.__aexit__ = AsyncMock(return_value=False)

        # Simulate streaming download.
        async def _iter_chunks(size: int) -> AsyncIterator[bytes]:
            yield zip_bytes

        mock_dl_resp = MagicMock()
        mock_dl_resp.status = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_dl_resp.content = MagicMock()
        mock_dl_resp.content.iter_chunked = _iter_chunks

        session = MagicMock(spec=aiohttp.ClientSession)

        # Both _find_github_asset and _download_asset now go through gateway.request.
        # First call: GitHub releases API → returns JSON metadata (mock_api_resp).
        # Second call: asset download URL → returns binary stream (mock_dl_resp).
        installer._gateway.request = AsyncMock(side_effect=[mock_api_resp, mock_dl_resp])

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
        assert installer._gateway.request.await_count == 2
        # Second call must be the asset's API URL (redirect-reauth enforced via gateway).
        assert (
            installer._gateway.request.call_args_list[1].args[1]
            == "https://api.github.com/repos/loot/loot/releases/assets/1001"
        )

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando el operador lo deniega, lanza ToolInstallError."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _github_release_json()
        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._gateway.request = AsyncMock(return_value=mock_api_resp)

        async def _auto_deny() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-loot-0.22.4", approved=False)

        asyncio.create_task(_auto_deny())

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_loot(install_dir, session)

    @pytest.mark.asyncio
    async def test_github_api_failure_raises(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando la API de GitHub falla, lanza ToolInstallError."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        mock_resp = MagicMock()
        mock_resp.status = 403

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._gateway.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(ToolInstallError, match="403"):
            await installer.ensure_loot(install_dir, session)


# ---------------------------------------------------------------------------
# ToolsInstaller.ensure_xedit
# ---------------------------------------------------------------------------


class TestEnsureXedit:
    @pytest.mark.asyncio
    async def test_returns_existing_without_download(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        exe = tmp_path / "SSEEdit.exe"
        exe.write_text("fake", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_xedit(tmp_path, session)

        assert result.already_existed is True
        assert result.exe_path == exe
        assert result.tool_name == "SSEEdit"

    @pytest.mark.asyncio
    async def test_downloads_and_extracts_when_missing(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        import zipfile

        zip_path = tmp_path / "_fake_asset.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("SSEEdit/SSEEdit.exe", "fake-binary")

        zip_bytes = zip_path.read_bytes()
        zip_path.unlink()

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        # Use a .zip asset name to match.
        release_json = _xedit_release_json(asset_name="xEdit_4.1.5.zip", size=len(zip_bytes))

        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)
        mock_api_resp.__aenter__ = AsyncMock(return_value=mock_api_resp)
        mock_api_resp.__aexit__ = AsyncMock(return_value=False)

        async def _iter_chunks(size: int) -> AsyncIterator[bytes]:
            yield zip_bytes

        mock_dl_resp = MagicMock()
        mock_dl_resp.status = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_dl_resp.content = MagicMock()
        mock_dl_resp.content.iter_chunked = _iter_chunks

        session = MagicMock(spec=aiohttp.ClientSession)

        # Both _find_github_asset and _download_asset go through gateway.request.
        installer._gateway.request = AsyncMock(side_effect=[mock_api_resp, mock_dl_resp])

        async def _auto_approve() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-xedit-4.1.5", approved=True)

        asyncio.create_task(_auto_approve())

        result = await installer.ensure_xedit(install_dir, session)

        assert result.already_existed is False
        assert result.tool_name == "SSEEdit"
        assert result.version == "4.1.5"
        assert result.exe_path.name == "SSEEdit.exe"
        assert installer._gateway.request.await_count == 2
        assert (
            installer._gateway.request.call_args_list[1].args[1]
            == "https://api.github.com/repos/TES5Edit/TES5Edit/releases/assets/2001"
        )

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _xedit_release_json(asset_name="xEdit_4.1.5.zip")
        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._gateway.request = AsyncMock(return_value=mock_api_resp)

        async def _auto_deny() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-xedit-4.1.5", approved=False)

        asyncio.create_task(_auto_deny())

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_xedit(install_dir, session)

    @pytest.mark.asyncio
    async def test_ensure_xedit_renames_executable(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        install_dir = tmp_path / "xedit"
        install_dir.mkdir()

        monkeypatch.setattr(
            "sky_claw.local.tools_installer.ToolsInstaller._find_github_asset",
            AsyncMock(
                return_value=(MagicMock(name="xEdit.7z", browser_download_url="http://fake", size=1024), "4.1.5")
            ),
        )
        monkeypatch.setattr(
            "sky_claw.local.tools_installer.HITLGuard.request_approval", AsyncMock(return_value=Decision.APPROVED)
        )
        monkeypatch.setattr(
            "sky_claw.local.tools_installer.ToolsInstaller._download_asset",
            AsyncMock(return_value=tmp_path / "xEdit.7z"),
        )

        # Mock _extract to simulate extraction creating xEdit.exe instead of SSEEdit.exe
        def mock_extract(self_obj, archive, directory):
            (directory / "xEdit.exe").touch()

        monkeypatch.setattr("sky_claw.local.tools_installer.ToolsInstaller._extract", mock_extract)
        monkeypatch.setattr("pathlib.Path.unlink", MagicMock())

        session = MagicMock(spec=aiohttp.ClientSession)

        result = await installer.ensure_xedit(install_dir, session)

        assert result.tool_name == "SSEEdit"
        assert result.exe_path == install_dir / "SSEEdit.exe"
        assert (install_dir / "SSEEdit.exe").exists()
        assert not (install_dir / "xEdit.exe").exists()


class TestEnsurePandora:
    @pytest.mark.asyncio
    async def test_ensure_pandora_expects_correct_exe_name(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        install_dir = tmp_path / "pandora"
        install_dir.mkdir()

        call_count = [0]

        # Mock find_exe_in_dir to return None on the first call, but the path on the second call
        def mock_find_exe(directory, name):
            if name == "Pandora Behaviour Engine+.exe":
                call_count[0] += 1
                if call_count[0] == 2:
                    return directory / name
            return None

        monkeypatch.setattr("sky_claw.local.tools_installer.find_exe_in_dir", mock_find_exe)
        monkeypatch.setattr(
            "sky_claw.local.tools_installer.ToolsInstaller._find_github_asset",
            AsyncMock(
                return_value=(MagicMock(name="Pandora.zip", browser_download_url="http://fake", size=1024), "1.0.0")
            ),
        )
        monkeypatch.setattr(
            "sky_claw.local.tools_installer.HITLGuard.request_approval", AsyncMock(return_value=Decision.APPROVED)
        )
        monkeypatch.setattr(
            "sky_claw.local.tools_installer.ToolsInstaller._download_asset",
            AsyncMock(return_value=tmp_path / "Pandora.zip"),
        )
        monkeypatch.setattr("sky_claw.local.tools_installer.ToolsInstaller._extract", MagicMock())
        monkeypatch.setattr("pathlib.Path.unlink", MagicMock())

        session = MagicMock(spec=aiohttp.ClientSession)

        result = await installer.ensure_pandora(install_dir, session)

        assert result.tool_name == "Pandora"
        assert result.exe_path == install_dir / "Pandora Behaviour Engine+.exe"
        assert result.already_existed is False


class TestExtract7zFallback:
    def test_extract_7z_safe_fallback_to_system_7z(self, monkeypatch, tmp_path: pathlib.Path) -> None:
        def mock_sevenzip(*args, **kwargs):
            raise Exception("BCJ2 filter is not supported")

        # Mock py7zr to throw an exception to simulate BCJ2 failure
        monkeypatch.setattr("py7zr.SevenZipFile", mock_sevenzip)

        # Mock subprocess.run to pretend 7z x succeeded
        mock_run = MagicMock()
        monkeypatch.setattr("subprocess.run", mock_run)

        from sky_claw.local.tools_installer import _extract_7z_safe

        archive = tmp_path / "dummy.7z"
        dest = tmp_path / "out"
        _extract_7z_safe(archive, dest)

        mock_run.assert_called_once()
        assert "7z" in mock_run.call_args[0][0]


# ---------------------------------------------------------------------------
# local_config
# ---------------------------------------------------------------------------


class TestLocalConfig:
    def test_load_defaults_when_file_missing(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.local_config import load

        cfg = load(tmp_path / "nonexistent.json")
        assert cfg.first_run is True
        assert cfg.loot_exe is None

    def test_save_and_load_roundtrip(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.local_config import LocalConfig, load, save

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
        from sky_claw.local.local_config import load

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
        from sky_claw.app.agent.tools import AsyncToolRegistry
        from sky_claw.app.db.async_registry import AsyncModRegistry
        from sky_claw.app.orchestrator.sync_engine import SyncEngine
        from sky_claw.app.scraper.masterlist import MasterlistClient
        from sky_claw.local.mo2.vfs import MO2Controller

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

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
            gateway=gw,  # TASK-013 P1: Zero-Trust requires gateway
        )

        result_str = await registry.execute("setup_tools", {"tools": ["loot", "xedit"]})
        result = json.loads(result_str)

        assert result["loot"]["status"] == "already_installed"
        assert result["xedit"]["status"] == "already_installed"
        assert "loot.exe" in result["loot"]["exe_path"]
        assert "SSEEdit.exe" in result["xedit"]["exe_path"]

        await db.close()

    @pytest.mark.asyncio
    async def test_setup_tools_no_installer_returns_error(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.app.agent.tools import AsyncToolRegistry
        from sky_claw.app.db.async_registry import AsyncModRegistry
        from sky_claw.app.orchestrator.sync_engine import SyncEngine
        from sky_claw.app.scraper.masterlist import MasterlistClient
        from sky_claw.local.mo2.vfs import MO2Controller

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

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
        from sky_claw.app.agent.tools import AsyncToolRegistry
        from sky_claw.app.db.async_registry import AsyncModRegistry
        from sky_claw.app.orchestrator.sync_engine import SyncEngine
        from sky_claw.app.scraper.masterlist import MasterlistClient
        from sky_claw.local.mo2.vfs import MO2Controller

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

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
            gateway=gw,  # TASK-013 P1: Zero-Trust requires gateway
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

        from sky_claw.__main__ import AppContext  # type: ignore[attr-defined]

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


# ---------------------------------------------------------------------------
# _extract_zip_safe — zip-slip protection (A-03)
# ---------------------------------------------------------------------------


class TestExtractZipSafe:
    """Tests for zip-slip protection in _extract_zip_safe."""

    def test_normal_extraction_succeeds(self, tmp_path: pathlib.Path) -> None:
        """Un archivo zip limpio se extrae correctamente."""
        zip_path = tmp_path / "archive.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "hello world")

        _extract_zip_safe(zip_path, dest)

        assert (dest / "readme.txt").exists()
        assert (dest / "readme.txt").read_text(encoding="utf-8") == "hello world"

    def test_dotdot_path_raises(self, tmp_path: pathlib.Path) -> None:
        """Zip con '../evil.txt' lanza PathViolationError."""
        zip_path = tmp_path / "malicious.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.txt", "evil content")

        with pytest.raises(PathViolationError):
            _extract_zip_safe(zip_path, dest)

        # No file should have escaped outside dest
        assert not (tmp_path / "evil.txt").exists()

    def test_absolute_path_raises(self, tmp_path: pathlib.Path) -> None:
        """Zip con ruta absoluta '/etc/passwd' lanza PathViolationError."""
        zip_path = tmp_path / "absolute.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("/etc/passwd", "root:x:0:0")

        with pytest.raises(PathViolationError):
            _extract_zip_safe(zip_path, dest)

        # Verify NO file was extracted inside dest
        assert not any(dest.iterdir()), "No files should be extracted from malicious zip"

    def test_nested_normal_path_succeeds(self, tmp_path: pathlib.Path) -> None:
        """Un zip con 'subdir/file.txt' se extrae correctamente."""
        zip_path = tmp_path / "nested.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("subdir/file.txt", "nested content")

        _extract_zip_safe(zip_path, dest)

        assert (dest / "subdir" / "file.txt").exists()
        assert (dest / "subdir" / "file.txt").read_text(encoding="utf-8") == "nested content"
