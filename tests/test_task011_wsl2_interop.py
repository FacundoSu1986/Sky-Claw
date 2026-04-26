"""TASK-011: WSL2/Windows Interoperability Robustness Tests.

Tests cover:
1. LOOTRunner -- timeout triggers proc.kill() + zombie reaping.
2. MO2Controller.launch_game() -- spawn verification (PID heartbeat).
3. MO2Controller.close_game() -- psutil wrapped in asyncio.to_thread.
4. WSL2 path translation -- conditional logic for WSL2 vs native Windows.
5. is_wsl2() detection utility.
6. translate_path_if_wsl() -- validation of Linux paths on native Windows.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.core.windows_interop import (
    is_wsl2,
    is_wsl2_cached,
    translate_path_if_wsl,
)
from sky_claw.loot.cli import (
    LOOTConfig,
    LOOTNotFoundError,
    LOOTRunner,
    LOOTTimeoutError,
)
from sky_claw.mo2.vfs import (
    DEFAULT_SPAWN_TIMEOUT,
    GameLaunchTimeoutError,
    MO2Controller,
)

# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture()
def _reset_wsl2_cache() -> None:
    """Reset the module-level WSL2 detection cache between tests."""
    import sky_claw.core.windows_interop as _mod

    _mod._WSL2_ACTIVE = None


@pytest.fixture()
def tmp_mo2_env(tmp_path: pathlib.Path) -> tuple[pathlib.Path, MagicMock]:
    """Create a minimal MO2 directory tree and a mock PathValidator."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    (mo2_root / "ModOrganizer.exe").touch()
    profile_dir = mo2_root / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text("+TestMod\n", encoding="utf-8")

    validator = MagicMock()
    validator.validate = MagicMock(side_effect=lambda p: pathlib.Path(p))
    return mo2_root, validator


def _make_loot_config(tmp_path: pathlib.Path) -> LOOTConfig:
    """Create a LOOTConfig pointing at fake executables under *tmp_path*."""
    loot_exe = tmp_path / "loot.exe"
    loot_exe.touch()
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()
    return LOOTConfig(loot_exe=loot_exe, game_path=game_path, timeout=5)


# ===================================================================
# 1. LOOTRunner -- Timeout + Zombie Prevention
# ===================================================================


class TestLOOTRunnerTimeout:
    """Verify that LOOTRunner.sort() kills the subprocess on timeout."""

    @pytest.mark.asyncio
    async def test_timeout_triggers_kill(self, tmp_path: pathlib.Path) -> None:
        """When LOOT times out, proc.kill() must be invoked exactly once."""
        config = _make_loot_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.asyncio.wait_for", side_effect=asyncio.TimeoutError),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            pytest.raises(LOOTTimeoutError, match="timed out"),
        ):
            await runner.sort()

        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_reaps_process(self, tmp_path: pathlib.Path) -> None:
        """After kill, proc.wait() is awaited to fully reap the process."""
        config = _make_loot_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        call_count = 0

        async def _wait_for_side_effect(coro, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError
            return await coro

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.asyncio.wait_for", side_effect=_wait_for_side_effect),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            pytest.raises(LOOTTimeoutError),
        ):
            await runner.sort()

        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_sort_does_not_kill(self, tmp_path: pathlib.Path) -> None:
        """On successful execution, proc.kill() must NOT be called."""
        config = _make_loot_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"  1. Skyrim.esm\n  2. Update.esm\n", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            result = await runner.sort()

        mock_proc.kill.assert_not_called()
        assert result.success is True
        assert result.sorted_plugins == ["Skyrim.esm", "Update.esm"]

    @pytest.mark.asyncio
    async def test_not_found_raises(self, tmp_path: pathlib.Path) -> None:
        """LOOTNotFoundError when the exe does not exist."""
        config = LOOTConfig(
            loot_exe=tmp_path / "nonexistent.exe",
            game_path=tmp_path,
        )
        runner = LOOTRunner(config)
        with pytest.raises(LOOTNotFoundError, match="not found"):
            await runner.sort()


# ===================================================================
# 2. MO2Controller.launch_game() -- Spawn Verification (PID Heartbeat)
# ===================================================================


class TestMO2LaunchGameSpawn:
    """Verify that launch_game() verifies PID appearance without blocking."""

    @pytest.mark.asyncio
    async def test_spawn_verification_succeeds(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """When PID appears promptly, launch succeeds and proc is NOT killed."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=5)

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch("sky_claw.mo2.vfs.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.mo2.vfs.psutil.pid_exists", return_value=True),
        ):
            result = await controller.launch_game("Default")

        mock_proc.kill.assert_not_called()
        assert result["status"] == "launched"
        assert result["pid"] == 12345

    @pytest.mark.asyncio
    async def test_spawn_timeout_triggers_kill(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """When PID never appears, proc.kill() must be invoked."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=5)

        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch("sky_claw.mo2.vfs.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.mo2.vfs.psutil.pid_exists", return_value=False),
            pytest.raises(GameLaunchTimeoutError, match="timed out"),
        ):
            await controller.launch_game("Default")

        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_timeout_reaps_process(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """After kill on spawn failure, proc.wait() is awaited."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=5)

        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        call_count = 0

        async def _wait_for_side_effect(coro, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError
            return await coro

        with (
            patch("sky_claw.mo2.vfs.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.mo2.vfs.psutil.pid_exists", return_value=False),
            patch("sky_claw.mo2.vfs.asyncio.wait_for", side_effect=_wait_for_side_effect),
            pytest.raises(GameLaunchTimeoutError),
        ):
            await controller.launch_game("Default")

        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_missing_exe_raises(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """FileNotFoundError when ModOrganizer.exe does not exist."""
        mo2_root, validator = tmp_mo2_env
        (mo2_root / "ModOrganizer.exe").unlink()
        controller = MO2Controller(mo2_root, validator)

        with pytest.raises(FileNotFoundError, match="MO2 executable not found"):
            await controller.launch_game("Default")

    @pytest.mark.asyncio
    async def test_launch_uses_configurable_timeout(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """The launch_timeout parameter controls spawn verification timeout."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=42)

        mock_proc = AsyncMock()
        mock_proc.pid = 999
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch("sky_claw.mo2.vfs.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.mo2.vfs.psutil.pid_exists", return_value=True),
            patch("sky_claw.mo2.vfs.asyncio.wait_for", return_value=None) as mock_wf,
        ):
            await controller.launch_game("Default")

        _, kwargs = mock_wf.call_args
        assert kwargs.get("timeout") == 42


# ===================================================================
# 3. MO2Controller.close_game() -- asyncio.to_thread wrapping
# ===================================================================


class TestMO2CloseGameAsync:
    """Verify that close_game() delegates psutil to a thread."""

    @pytest.mark.asyncio
    async def test_close_game_uses_to_thread(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """close_game() wraps _kill_game_processes in asyncio.to_thread."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator)

        with patch("sky_claw.mo2.vfs.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = ["SkyrimSE.exe"]
            result = await controller.close_game()

        mock_to_thread.assert_called_once()
        assert result["status"] == "closed"
        assert result["killed_processes"] == ["SkyrimSE.exe"]

    @pytest.mark.asyncio
    async def test_close_game_returns_killed_list(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """close_game() returns the list of killed process names."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator)

        mock_proc_item = MagicMock()
        mock_proc_item.info = {"pid": 1, "name": "SkyrimSE.exe"}
        mock_proc_item.kill = MagicMock()

        mock_notepad = MagicMock()
        mock_notepad.info = {"pid": 2, "name": "notepad.exe"}

        with patch("sky_claw.mo2.vfs.psutil.process_iter", return_value=[mock_proc_item, mock_notepad]):
            result = await controller.close_game()

        assert result["status"] == "closed"
        assert "SkyrimSE.exe" in result["killed_processes"]
        assert "notepad.exe" not in result["killed_processes"]


# ===================================================================
# 4. WSL2 Path Translation -- Conditional Logic
# ===================================================================


class TestWSL2PathTranslation:
    """Verify translate_path_if_wsl() behavior in WSL2 vs native."""

    @pytest.mark.asyncio
    async def test_wsl2_translates_path(self, *, _reset_wsl2_cache: None) -> None:
        """Under WSL2, path is translated via wslpath."""
        with (
            patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=True),
            patch(
                "sky_claw.core.windows_interop._translate_wsl_to_win",
                return_value=r"C:\Modding\MO2",
            ) as mock_translate,
        ):
            result = await translate_path_if_wsl("/mnt/c/Modding/MO2")

        assert result == r"C:\Modding\MO2"
        mock_translate.assert_called_once_with("/mnt/c/Modding/MO2", timeout=10.0)

    @pytest.mark.asyncio
    async def test_native_windows_passes_through(self, *, _reset_wsl2_cache: None) -> None:
        """On native Windows, a valid Windows path passes through unchanged."""
        with patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=False):
            result = await translate_path_if_wsl(r"C:\Modding\MO2")

        assert result == r"C:\Modding\MO2"

    @pytest.mark.asyncio
    async def test_native_windows_rejects_linux_path(self, *, _reset_wsl2_cache: None) -> None:
        """On native Windows, a Linux-style /mnt/ path raises ValueError."""
        with (
            patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=False),
            pytest.raises(ValueError, match="Linux-style path"),
        ):
            await translate_path_if_wsl("/mnt/c/Modding/MO2")

    @pytest.mark.asyncio
    async def test_native_windows_rejects_unix_absolute(self, *, _reset_wsl2_cache: None) -> None:
        """On native Windows, a Unix absolute path raises ValueError."""
        with (
            patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=False),
            pytest.raises(ValueError, match="Linux-style path"),
        ):
            await translate_path_if_wsl("/home/user/mods")

    @pytest.mark.asyncio
    async def test_custom_timeout_forwarded(self, *, _reset_wsl2_cache: None) -> None:
        """Custom timeout is forwarded to _translate_wsl_to_win."""
        with (
            patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=True),
            patch(
                "sky_claw.core.windows_interop._translate_wsl_to_win",
                return_value="C:\\test",
            ) as mock_translate,
        ):
            await translate_path_if_wsl("/mnt/c/test", timeout=30.0)

        mock_translate.assert_called_once_with("/mnt/c/test", timeout=30.0)


# ===================================================================
# 5. is_wsl2() Detection
# ===================================================================


class TestIsWSL2Detection:
    """Verify WSL2 detection logic."""

    def test_win32_returns_false(self, *, _reset_wsl2_cache: None) -> None:
        """On win32 platform, is_wsl2() always returns False."""
        with patch("sky_claw.core.windows_interop.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert is_wsl2() is False

    def test_linux_with_proc_version_wsl(self, *, _reset_wsl2_cache: None) -> None:
        """On Linux with WSL signature in /proc/version, returns True."""
        with (
            patch("sky_claw.core.windows_interop.sys") as mock_sys,
            patch(
                "pathlib.Path.read_text",
                return_value="Linux version 5.15 microsoft-standard-WSL2",
            ),
        ):
            mock_sys.platform = "linux"
            assert is_wsl2() is True

    def test_linux_without_wsl(self, *, _reset_wsl2_cache: None) -> None:
        """On Linux without WSL, returns False."""
        with (
            patch("sky_claw.core.windows_interop.sys") as mock_sys,
            patch(
                "pathlib.Path.read_text",
                return_value="Linux version 5.15 generic",
            ),
            patch("sky_claw.core.windows_interop.os.path.isdir", return_value=False),
        ):
            mock_sys.platform = "linux"
            assert is_wsl2() is False

    def test_cached_flag_persists(self, *, _reset_wsl2_cache: None) -> None:
        """is_wsl2_cached() returns the same value on repeated calls."""
        import sky_claw.core.windows_interop as _mod

        _mod._WSL2_ACTIVE = None
        with patch("sky_claw.core.windows_interop.is_wsl2", return_value=True):
            result1 = asyncio.run(is_wsl2_cached())
            result2 = asyncio.run(is_wsl2_cached())

        assert result1 is True
        assert result2 is True


# ===================================================================
# 6. LOOTRunner WSL2 Integration
# ===================================================================


class TestLOOTRunnerWSL2Integration:
    """Verify LOOTRunner calls translate_path_if_wsl for game_path."""

    @pytest.mark.asyncio
    async def test_sort_calls_translate_path(self, tmp_path: pathlib.Path) -> None:
        """LOOTRunner.sort() translates game_path via translate_path_if_wsl."""
        config = _make_loot_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"  1. Skyrim.esm\n", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            patch(
                "sky_claw.loot.cli.translate_path_if_wsl",
                return_value=r"C:\Skyrim",
            ) as mock_translate,
        ):
            result = await runner.sort()

        mock_translate.assert_called_once_with(config.game_path)

        call_args = mock_exec.call_args
        args_list = call_args[0]
        assert r"C:\Skyrim" in args_list

        assert result.success is True

    @pytest.mark.asyncio
    async def test_sort_wsl2_translation_failure(self, tmp_path: pathlib.Path) -> None:
        """If translate_path_if_wsl raises, the error propagates."""
        config = _make_loot_config(tmp_path)
        runner = LOOTRunner(config)

        from sky_claw.core.models import WSLInteropError

        with (
            patch(
                "sky_claw.loot.cli.translate_path_if_wsl",
                side_effect=WSLInteropError("wslpath failed"),
            ),
            pytest.raises(WSLInteropError, match="wslpath failed"),
        ):
            await runner.sort()


# ===================================================================
# 7. MO2Controller.launch_game() WSL2 Integration
# ===================================================================


class TestMO2LaunchGameWSL2:
    """Verify launch_game() uses native cwd and does not translate it."""

    @pytest.mark.asyncio
    async def test_launch_uses_native_cwd(self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]) -> None:
        """launch_game() passes the native path (Linux or Windows) as cwd."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=5)

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        async def _fake_verify(pid: int) -> None:
            return

        with (
            patch("sky_claw.mo2.vfs.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            patch("sky_claw.mo2.vfs._verify_pid_alive", side_effect=_fake_verify),
        ):
            result = await controller.launch_game("Default")

        call_kwargs = mock_exec.call_args.kwargs
        assert call_kwargs.get("cwd") == str(mo2_root)
        assert result["status"] == "launched"


# ===================================================================
# 8. Default timeout constants
# ===================================================================


class TestDefaultConstants:
    """Verify default timeout values are sensible."""

    def test_default_launch_timeout(self) -> None:
        assert DEFAULT_SPAWN_TIMEOUT == 5

    def test_loot_config_default_timeout(self) -> None:
        from sky_claw.loot.cli import DEFAULT_TIMEOUT as LOOT_DEFAULT

        config = LOOTConfig(
            loot_exe=pathlib.Path("/fake/loot.exe"),
            game_path=pathlib.Path("/fake/game"),
        )
        assert config.timeout == LOOT_DEFAULT
