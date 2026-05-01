"""Tests for sky_claw.loot (cli, parser, masterlist)."""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.loot.cli import (
    LOOTConfig,
    LOOTNotFoundError,
    LOOTRunner,
    LOOTTimeoutError,
)
from sky_claw.loot.masterlist import MasterlistDownloader
from sky_claw.loot.parser import LOOTOutputParser, LOOTResult

if TYPE_CHECKING:
    import pathlib

# ------------------------------------------------------------------
# LOOTOutputParser
# ------------------------------------------------------------------


class TestLOOTOutputParser:
    def test_parse_sorted_plugins(self) -> None:
        stdout = "Sorting plugins...\n  1. Skyrim.esm\n  2. Update.esm\n  3. Dawnguard.esm\n  4. Requiem.esp\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert result.sorted_plugins == [
            "Skyrim.esm",
            "Update.esm",
            "Dawnguard.esm",
            "Requiem.esp",
        ]
        assert result.success is True

    def test_parse_warnings(self) -> None:
        stdout = "Warning: Requiem.esp has unresolved masters\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert len(result.warnings) == 1
        assert "Requiem.esp" in result.warnings[0]

    def test_parse_errors_in_stderr(self) -> None:
        result = LOOTOutputParser.parse(stdout="", stderr="Error: Game path not found\n", return_code=1)
        assert len(result.errors) == 1
        assert result.success is False

    def test_parse_empty_output(self) -> None:
        result = LOOTOutputParser.parse(stdout="", stderr="", return_code=0)
        assert result.sorted_plugins == []
        assert result.warnings == []
        assert result.errors == []
        # Golden Master: success requires plugins > 0
        assert result.success is False

    def test_parse_mixed_output(self) -> None:
        stdout = (
            "  1. Skyrim.esm\n"
            "Warning: Missing master for SomePlugin.esp\n"
            "  2. SomePlugin.esp\n"
            "Error: Critical conflict detected\n"
        )
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=1)
        assert result.sorted_plugins == ["Skyrim.esm", "SomePlugin.esp"]
        assert len(result.warnings) == 1
        assert len(result.errors) == 1
        assert result.success is False

    def test_parse_esl_and_esm(self) -> None:
        stdout = "  1. ccBGSSSE001-Fish.esl\n  2. Unofficial Skyrim Special Edition Patch.esp\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert len(result.sorted_plugins) == 2

    def test_parse_ansi_escapes(self) -> None:
        """Golden Master: ANSI escape sequences are stripped before parsing."""
        stdout = "\x1b[32m  1. Skyrim.esm\x1b[0m\n\x1b[33m  2. Requiem.esp\x1b[0m\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert result.sorted_plugins == ["Skyrim.esm", "Requiem.esp"]
        assert result.success is True

    def test_parse_native_crash(self) -> None:
        """Golden Master: native crash signature injects CRITICAL error."""
        stdout = "  1. Skyrim.esm\nFATAL ERROR: access violation at 0xDEADBEEF\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=1)
        assert result.success is False
        assert len(result.errors) >= 1
        assert "CRITICAL" in result.errors[0]
        assert "crashed natively" in result.errors[0]


# ------------------------------------------------------------------
# LOOTRunner
# ------------------------------------------------------------------


class TestLOOTRunner:
    def _make_config(self, tmp_path: pathlib.Path) -> LOOTConfig:
        loot_exe = tmp_path / "loot.exe"
        loot_exe.touch()
        game_path = tmp_path / "Skyrim"
        game_path.mkdir()
        return LOOTConfig(loot_exe=loot_exe, game_path=game_path, timeout=5)

    @pytest.mark.asyncio
    async def test_loot_not_found_raises(self, tmp_path: pathlib.Path) -> None:
        config = LOOTConfig(
            loot_exe=tmp_path / "nonexistent.exe",
            game_path=tmp_path,
        )
        runner = LOOTRunner(config)
        with pytest.raises(LOOTNotFoundError, match="not found"):
            await runner.sort()

    @pytest.mark.asyncio
    async def test_sort_success(self, tmp_path: pathlib.Path) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(
                b"  1. Skyrim.esm\n  2. Update.esm\n",
                b"",
            )
        )
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            result = await runner.sort()

        assert result.success is True
        assert result.sorted_plugins == ["Skyrim.esm", "Update.esm"]

    @pytest.mark.asyncio
    async def test_sort_timeout(self, tmp_path: pathlib.Path) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        # First call (inside wait_for) never happens because wait_for is patched.
        # Second call (cleanup after kill) should succeed.
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.kill = MagicMock()

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.asyncio.wait_for", side_effect=asyncio.TimeoutError),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            pytest.raises(LOOTTimeoutError, match="timed out"),
        ):
            await runner.sort()

    @pytest.mark.asyncio
    async def test_loot_timeout_wsl_taskkill(self, tmp_path: pathlib.Path) -> None:
        """Golden Master: WSL2 taskkill annihilator fires on timeout."""
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.kill = MagicMock()

        mock_taskkill = MagicMock()

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.asyncio.wait_for", side_effect=asyncio.TimeoutError),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            patch("sky_claw.loot.cli.subprocess.run", mock_taskkill),
            patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}),
            pytest.raises(LOOTTimeoutError, match="timed out"),
        ):
            await runner.sort()

        mock_taskkill.assert_called_once_with(
            ["taskkill.exe", "/F", "/IM", "loot.exe", "/T"],
            capture_output=True,
            check=False,
        )

    @pytest.mark.asyncio
    async def test_sort_with_errors(self, tmp_path: pathlib.Path) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Error: Game path invalid\n"))
        mock_proc.returncode = 1
        mock_proc.kill = MagicMock()

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            result = await runner.sort()

        assert result.success is False
        assert len(result.errors) == 1


# ------------------------------------------------------------------
# MasterlistDownloader
# ------------------------------------------------------------------


class TestMasterlistDownloader:
    @pytest.mark.asyncio
    async def test_uses_cache_when_valid(self, tmp_path: pathlib.Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = cache_dir / "masterlist.yaml"
        cached.write_text("cached content")

        mock_gw = MagicMock()
        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=cache_dir, ttl=3600)
        session = MagicMock()
        path = await downloader.get(session)

        assert path == cached
        mock_gw.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_downloads_when_cache_expired(self, tmp_path: pathlib.Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = cache_dir / "masterlist.yaml"
        cached.write_text("old content")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(cached, (old_time, old_time))

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"new content")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_gw = MagicMock()
        mock_gw.request = AsyncMock(return_value=mock_resp)

        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=cache_dir, ttl=3600)
        session = MagicMock()
        path = await downloader.get(session)

        assert path == cached
        assert cached.read_text() == "new content"
        mock_gw.request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_downloads_when_no_cache(self, tmp_path: pathlib.Path) -> None:
        cache_dir = tmp_path / "cache"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"masterlist yaml")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_gw = MagicMock()
        mock_gw.request = AsyncMock(return_value=mock_resp)

        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=cache_dir, ttl=3600)
        session = MagicMock()
        path = await downloader.get(session)

        assert path.exists()
        assert path.read_text() == "masterlist yaml"

    @pytest.mark.asyncio
    async def test_raises_on_download_failure(self, tmp_path: pathlib.Path) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.text = AsyncMock(return_value="Not Found")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_gw = MagicMock()
        mock_gw.request = AsyncMock(return_value=mock_resp)

        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=tmp_path, ttl=3600)
        session = MagicMock()
        with pytest.raises(RuntimeError, match="404"):
            await downloader.get(session)


# ------------------------------------------------------------------
# Tool integration (run_loot_sort uses LOOTRunner)
# ------------------------------------------------------------------


class TestLootSortTool:
    @pytest.mark.asyncio
    async def test_loot_sort_no_runner_configured(self, tmp_path: pathlib.Path) -> None:
        """When no LOOTRunner is provided, tool returns error JSON."""
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.db.async_registry import AsyncModRegistry
        from sky_claw.mo2.vfs import MO2Controller
        from sky_claw.orchestrator.sync_engine import SyncEngine
        from sky_claw.security.path_validator import PathValidator

        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("+TestMod-100\n")
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, validator)

        registry = AsyncModRegistry(db_path=tmp_path / "test.db")
        await registry.open()
        try:
            sync = SyncEngine(mo2, MagicMock(), registry)
            tool_reg = AsyncToolRegistry(
                registry=registry,
                mo2=mo2,
                sync_engine=sync,
                loot_runner=None,
            )
            import json

            result = json.loads(await tool_reg.execute("run_loot_sort", {"profile": "Default"}))
            assert "error" in result
            assert "not configured" in result["error"] or "not found" in result["error"]
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_loot_sort_with_runner(self, tmp_path: pathlib.Path) -> None:
        """When LOOTRunner is provided, tool delegates to it."""
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.db.async_registry import AsyncModRegistry
        from sky_claw.mo2.vfs import MO2Controller
        from sky_claw.orchestrator.sync_engine import SyncEngine
        from sky_claw.security.path_validator import PathValidator

        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("+TestMod-100\n")
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, validator)

        registry = AsyncModRegistry(db_path=tmp_path / "test.db")
        await registry.open()
        try:
            sync = SyncEngine(mo2, MagicMock(), registry)

            mock_runner = MagicMock()
            mock_runner.sort = AsyncMock(
                return_value=LOOTResult(
                    return_code=0,
                    sorted_plugins=["Skyrim.esm", "Requiem.esp"],
                    warnings=["Some warning"],
                    errors=[],
                )
            )

            tool_reg = AsyncToolRegistry(
                registry=registry,
                mo2=mo2,
                sync_engine=sync,
                loot_runner=mock_runner,
            )
            import json

            result = json.loads(await tool_reg.execute("run_loot_sort", {"profile": "Default"}))
            assert result["success"] is True
            assert result["sorted_plugins"] == ["Skyrim.esm", "Requiem.esp"]
            assert result["warnings"] == ["Some warning"]
            mock_runner.sort.assert_awaited_once()
        finally:
            await registry.close()
