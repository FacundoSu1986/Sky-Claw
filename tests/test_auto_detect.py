"""Tests for the zero-config AutoDetector and related endpoints."""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from sky_claw.auto_detect import (
    AutoDetector,
    _parse_steam_library_folders,
    _read_registry_value,
)
from sky_claw.local_config import LocalConfig, save


# ---------------------------------------------------------------------------
# find_mo2
# ---------------------------------------------------------------------------


class TestFindMo2:
    @pytest.mark.asyncio
    async def test_finds_mo2_in_common_path(self, tmp_path: pathlib.Path) -> None:
        mo2_dir = tmp_path / "MO2"
        mo2_dir.mkdir()
        (mo2_dir / "ModOrganizer.exe").touch()

        with patch("sky_claw.auto_detect._MO2_COMMON", (str(mo2_dir),)):
            result = await AutoDetector.find_mo2()

        assert result == mo2_dir

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        with patch("sky_claw.auto_detect._MO2_COMMON", ()):
            with patch("sky_claw.auto_detect.os.environ", {}):
                result = await AutoDetector.find_mo2()

        assert result is None

    @pytest.mark.asyncio
    async def test_finds_mo2_in_appdata(self, tmp_path: pathlib.Path) -> None:
        mo2_dir = tmp_path / "ModOrganizer" / "MyInstance"
        mo2_dir.mkdir(parents=True)
        (mo2_dir / "ModOrganizer.exe").touch()

        with patch("sky_claw.auto_detect._MO2_COMMON", ()):
            with patch.dict("os.environ", {"LOCALAPPDATA": str(tmp_path)}):
                result = await AutoDetector.find_mo2()

        assert result == mo2_dir


# ---------------------------------------------------------------------------
# find_skyrim
# ---------------------------------------------------------------------------


class TestFindSkyrim:
    @pytest.mark.asyncio
    async def test_finds_skyrim_via_registry(self, tmp_path: pathlib.Path) -> None:
        skyrim_dir = tmp_path / "Skyrim Special Edition"
        skyrim_dir.mkdir()
        (skyrim_dir / "SkyrimSE.exe").touch()

        with patch(
            "sky_claw.auto_detect._read_registry_value",
            return_value=str(skyrim_dir),
        ):
            result = await AutoDetector.find_skyrim()

        assert result == skyrim_dir

    @pytest.mark.asyncio
    async def test_finds_skyrim_via_steam(self, tmp_path: pathlib.Path) -> None:
        # Build a fake Steam library with Skyrim.
        steam_dir = tmp_path / "Steam"
        apps_dir = steam_dir / "steamapps"
        apps_dir.mkdir(parents=True)

        skyrim_dir = apps_dir / "common" / "Skyrim Special Edition"
        skyrim_dir.mkdir(parents=True)
        (skyrim_dir / "SkyrimSE.exe").touch()

        vdf_content = (
            f'"libraryfolders"\n{{\n  "0"\n  {{\n    "path"\t\t"{steam_dir}"\n  }}\n}}'
        )
        vdf_path = apps_dir / "libraryfolders.vdf"
        vdf_path.write_text(vdf_content, encoding="utf-8")

        with patch("sky_claw.auto_detect._read_registry_value", return_value=None):
            with patch("sky_claw.auto_detect._STEAM_DEFAULT_PATHS", (str(steam_dir),)):
                result = await AutoDetector.find_skyrim()

        assert result == skyrim_dir

    @pytest.mark.asyncio
    async def test_finds_skyrim_common_path(self, tmp_path: pathlib.Path) -> None:
        skyrim_dir = tmp_path / "Skyrim Special Edition"
        skyrim_dir.mkdir()
        (skyrim_dir / "SkyrimSE.exe").touch()

        with patch("sky_claw.auto_detect._read_registry_value", return_value=None):
            with patch("sky_claw.auto_detect._STEAM_DEFAULT_PATHS", ()):
                with patch("sky_claw.auto_detect._SKYRIM_COMMON", (str(skyrim_dir),)):
                    result = await AutoDetector.find_skyrim()

        assert result == skyrim_dir

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        with patch("sky_claw.auto_detect._read_registry_value", return_value=None):
            with patch("sky_claw.auto_detect._STEAM_DEFAULT_PATHS", ()):
                with patch("sky_claw.auto_detect._SKYRIM_COMMON", ()):
                    result = await AutoDetector.find_skyrim()

        assert result is None


# ---------------------------------------------------------------------------
# find_loot / find_xedit
# ---------------------------------------------------------------------------


class TestFindTools:
    @pytest.mark.asyncio
    async def test_finds_loot(self, tmp_path: pathlib.Path) -> None:
        loot_dir = tmp_path / "LOOT"
        loot_dir.mkdir()
        (loot_dir / "LOOT.exe").touch()

        with patch("sky_claw.auto_detect._LOOT_COMMON", (str(loot_dir),)):
            result = await AutoDetector.find_loot()

        assert result == loot_dir / "LOOT.exe"

    @pytest.mark.asyncio
    async def test_loot_not_found(self) -> None:
        with patch("sky_claw.auto_detect._LOOT_COMMON", ()):
            with patch("shutil.which", return_value=None):
                result = await AutoDetector.find_loot()

        assert result is None

    @pytest.mark.asyncio
    async def test_finds_xedit(self, tmp_path: pathlib.Path) -> None:
        xedit_dir = tmp_path / "SSEEdit"
        xedit_dir.mkdir()
        (xedit_dir / "SSEEdit.exe").touch()

        with patch("sky_claw.auto_detect._XEDIT_COMMON", (str(xedit_dir),)):
            result = await AutoDetector.find_xedit()

        assert result == xedit_dir / "SSEEdit.exe"

    @pytest.mark.asyncio
    async def test_xedit_not_found(self) -> None:
        with patch("sky_claw.auto_detect._XEDIT_COMMON", ()):
            with patch("shutil.which", return_value=None):
                result = await AutoDetector.find_xedit()

        assert result is None


# ---------------------------------------------------------------------------
# detect_all
# ---------------------------------------------------------------------------


class TestDetectAll:
    @pytest.mark.asyncio
    async def test_detect_all_returns_dict(self, tmp_path: pathlib.Path) -> None:
        mo2_dir = tmp_path / "MO2"
        mo2_dir.mkdir()
        (mo2_dir / "ModOrganizer.exe").touch()

        with patch("sky_claw.auto_detect._MO2_COMMON", (str(mo2_dir),)):
            with patch("sky_claw.auto_detect._read_registry_value", return_value=None):
                with patch("sky_claw.auto_detect._STEAM_DEFAULT_PATHS", ()):
                    with patch("sky_claw.auto_detect._SKYRIM_COMMON", ()):
                        with patch("sky_claw.auto_detect._LOOT_COMMON", ()):
                            with patch("sky_claw.auto_detect._XEDIT_COMMON", ()):
                                with patch("shutil.which", return_value=None):
                                    result = await AutoDetector.detect_all()

        assert isinstance(result, dict)
        assert result["mo2_root"] == str(mo2_dir)
        assert result["skyrim_path"] is None
        assert result["loot_exe"] is None
        assert result["xedit_exe"] is None

    @pytest.mark.asyncio
    async def test_detect_all_handles_timeout(self) -> None:
        """detect_all doesn't crash even if individual detectors time out."""
        import asyncio

        async def _slow() -> None:
            await asyncio.sleep(100)

        with patch.object(AutoDetector, "_find_mo2_inner", side_effect=_slow):
            with patch("sky_claw.auto_detect._SEARCH_TIMEOUT", 0.01):
                with patch(
                    "sky_claw.auto_detect._read_registry_value", return_value=None
                ):
                    with patch("sky_claw.auto_detect._STEAM_DEFAULT_PATHS", ()):
                        with patch("sky_claw.auto_detect._SKYRIM_COMMON", ()):
                            with patch("sky_claw.auto_detect._LOOT_COMMON", ()):
                                with patch("sky_claw.auto_detect._XEDIT_COMMON", ()):
                                    with patch("shutil.which", return_value=None):
                                        result = await AutoDetector.detect_all()

        # MO2 should be None (timed out), others should still work.
        assert result["mo2_root"] is None


# ---------------------------------------------------------------------------
# Steam VDF parser
# ---------------------------------------------------------------------------


class TestSteamVdfParser:
    def test_parse_library_folders(self, tmp_path: pathlib.Path) -> None:
        vdf = tmp_path / "libraryfolders.vdf"
        vdf.write_text(
            '"libraryfolders"\n{\n  "0"\n  {\n    "path"\t\t"C:\\\\Steam"\n  }\n}\n',
            encoding="utf-8",
        )
        result = _parse_steam_library_folders(vdf)
        assert len(result) == 1
        assert result[0] == pathlib.Path("C:\\Steam")

    def test_missing_vdf_returns_empty(self, tmp_path: pathlib.Path) -> None:
        result = _parse_steam_library_folders(tmp_path / "nonexistent.vdf")
        assert result == []


# ---------------------------------------------------------------------------
# Registry helper
# ---------------------------------------------------------------------------


class TestRegistryHelper:
    def test_returns_none_without_winreg(self) -> None:
        with patch("sky_claw.auto_detect._HAS_WINREG", False):
            result = _read_registry_value(0, "SOME\\KEY", "Value")
        assert result is None


# ---------------------------------------------------------------------------
# /api/auto-detect endpoint
# ---------------------------------------------------------------------------


class TestAutoDetectEndpoint:
    @pytest.mark.asyncio
    async def test_auto_detect_endpoint(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        mock_result = {
            "mo2_root": "C:/MO2",
            "skyrim_path": "C:/Skyrim",
            "loot_exe": None,
            "xedit_exe": None,
        }
        with patch.object(AutoDetector, "detect_all", return_value=mock_result):
            resp = await client.get("/api/auto-detect")

        assert resp.status == 200
        data = await resp.json()
        assert data["mo2_root"] == "C:/MO2"
        assert data["skyrim_path"] == "C:/Skyrim"
        assert data["loot_exe"] is None


# ---------------------------------------------------------------------------
# /api/install-tools endpoint
# ---------------------------------------------------------------------------


class TestInstallToolsEndpoint:
    @pytest.mark.asyncio
    async def test_install_tools_no_installer(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        web_app = WebApp(
            router=router,
            session=session,
            config_path=config_path,
            tools_installer=None,
        )
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/install-tools", json={})
        assert resp.status == 503


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT updated
# ---------------------------------------------------------------------------


class TestSystemPromptZeroConfig:
    def test_system_prompt_spanish(self) -> None:
        from sky_claw.app_context import SYSTEM_PROMPT

        assert "español" in SYSTEM_PROMPT.lower()

    def test_system_prompt_default_profile(self) -> None:
        from sky_claw.app_context import SYSTEM_PROMPT

        assert "Default" in SYSTEM_PROMPT

    def test_system_prompt_offers_install(self) -> None:
        from sky_claw.app_context import SYSTEM_PROMPT

        assert "instalarla" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Setup wizard auto-fills detected values
# ---------------------------------------------------------------------------


class TestSetupAutoFill:
    @pytest.mark.asyncio
    async def test_get_setup_includes_tool_paths(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        cfg = LocalConfig(
            mo2_root="C:/MO2",
            loot_exe="C:/LOOT/LOOT.exe",
            xedit_exe="C:/SSEEdit/SSEEdit.exe",
            first_run=True,
        )
        save(cfg, config_path)

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/setup")
        assert resp.status == 200
        data = await resp.json()
        assert data["loot_exe"] == "C:/LOOT/LOOT.exe"
        assert data["xedit_exe"] == "C:/SSEEdit/SSEEdit.exe"
