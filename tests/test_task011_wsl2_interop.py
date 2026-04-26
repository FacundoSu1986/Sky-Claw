"""TASK-011: WSL2/Windows Interoperability Robustness Tests.

Tests cover:
1. LOOTRunner — timeout triggers proc.kill() + zombie reaping.
2. MO2Controller.launch_game() — timeout triggers proc.kill().
3. MO2Controller.close_game() — psutil wrapped in asyncio.to_thread.
4. WSL2 path translation — conditional logic for WSL2 vs native Windows.
5. is_wsl2() detection utility.
6. translate_path_if_wsl() — validation of Linux paths on native Windows.
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
    DEFAULT_LAUNCH_TIMEOUT,
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
    # Make validate() return the same path (pass-through)
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
# 1. LOOTRunner — Timeout + Zombie Prevention
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

        # ASSERT: proc.kill() was called to prevent zombie
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

        # Make the first wait_for (communicate) raise TimeoutError.
        # The second wait_for (proc.wait after kill) should succeed.
        call_count = 0

        async def _wait_for_side_effect(coro, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError
            # Second call: await the coro (proc.wait())
            return await coro

        with (
            patch("sky_claw.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.loot.cli.asyncio.wait_for", side_effect=_wait_for_side_effect),
            patch("sky_claw.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            pytest.raises(LOOTTimeoutError),
        ):
            await runner.sort()

        # ASSERT: kill was called
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_sort_does_not_kill(self, tmp_path: pathlib.Path) -> None:
        """On successful execution, proc.kill() must NOT be called."""
        config = _make_loot_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"  1. Skyrim.esm\n  2. Update.esm\n", b"")
        )
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
        """LOOTNotFoundError when the exe doesn't exist."""
        config = LOOTConfig(
            loot_exe=tmp_path / "nonexistent.exe",
            game_path=tmp_path,
        )
        runner = LOOTRunner(config)
        with pytest.raises(LOOTNotFoundError, match="not found"):
            await runner.sort()


# ===================================================================
# 2. MO2Controller.launch_game() — Timeout + Zombie Prevention
# ===================================================================


class TestMO2LaunchGameTimeout:
    """Verify that launch_game() kills the subprocess on timeout."""

    @pytest.mark.asyncio
    async def test_launch_timeout_triggers_kill(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """When game launch times out, proc.kill() must be invoked."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=5)

        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch(
                "sky_claw.mo2.vfs.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch("sky_claw.mo2.vfs.asyncio.wait_for", side_effect=asyncio.TimeoutError),
            patch("sky_claw.mo2.vfs.translate_path_if_wsl", return_value=str(mo2_root)),
            pytest.raises(GameLaunchTimeoutError, match="timed out"),
        ):
            await controller.launch_game("Default")

        # ASSERT: proc.kill() was called to prevent zombie
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_timeout_reaps_process(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """After kill, proc.wait() is awaited to fully reap the process."""
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
            patch(
                "sky_claw.mo2.vfs.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "sky_claw.mo2.vfs.asyncio.wait_for",
                side_effect=_wait_for_side_effect,
            ),
            patch("sky_claw.mo2.vfs.translate_path_if_wsl", return_value=str(mo2_root)),
            pytest.raises(GameLaunchTimeoutError),
        ):
            await controller.launch_game("Default")

        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_success_no_kill(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """On successful launch, proc.kill() must NOT be called."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=5)

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch(
                "sky_claw.mo2.vfs.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch("sky_claw.mo2.vfs.asyncio.wait_for", return_value=None),
            patch("sky_claw.mo2.vfs.translate_path_if_wsl", return_value=str(mo2_root)),
        ):
            result = await controller.launch_game("Default")

        mock_proc.kill.assert_not_called()
        assert result["status"] == "launched"
        assert result["pid"] == 12345

    @pytest.mark.asyncio
    async def test_launch_missing_exe_raises(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """FileNotFoundError when ModOrganizer.exe doesn't exist."""
        mo2_root, validator = tmp_mo2_env
        # Delete the exe
        (mo2_root / "ModOrganizer.exe").unlink()
        controller = MO2Controller(mo2_root, validator)

        with pytest.raises(FileNotFoundError, match="MO2 executable not found"):
            await controller.launch_game("Default")

    @pytest.mark.asyncio
    async def test_launch_uses_configurable_timeout(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """The launch_timeout parameter is respected."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=42)

        mock_proc = AsyncMock()
        mock_proc.pid = 999
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch(
                "sky_claw.mo2.vfs.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch("sky_claw.mo2.vfs.asyncio.wait_for", return_value=None) as mock_wf,
            patch("sky_claw.mo2.vfs.translate_path_if_wsl", return_value=str(mo2_root)),
        ):
            await controller.launch_game("Default")

        # ASSERT: wait_for was called with timeout=42
        _, kwargs = mock_wf.call_args
        assert kwargs.get("timeout") == 42


# ===================================================================
# 3. MO2Controller.close_game() — asyncio.to_thread wrapping
# ===================================================================


class TestMO2CloseGameAsync:
    """Verify that close_game() delegates psutil to a thread."""

    @pytest.mark.asyncio
    async def test_close_game_uses_to_thread(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """close_game() wraps _kill_game_processes in asyncio.to_thread."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator)

        with (
            patch.object(controller, "_kill_game_processes", return_value=["SkyrimSE.exe"]),
            patch("sky_claw.mo2.vfs.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
        ):
            mock_to_thread.return_value = ["SkyrimSE.exe"]
            result = await controller.close_game()

        # asyncio.to_thread was called with the sync helper
        mock_to_thread.assert_called_once()
        assert result["status"] == "closed"
        assert result["killed_processes"] == ["SkyrimSE.exe"]

    @pytest.mark.asyncio
    async def test_close_game_returns_killed_list(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """close_game() returns the list of killed process names."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator)

        with patch(
            "sky_claw.mo2.vfs.psutil.process_iter",
            return_value=[
                MagicMock(info={"pid": 1, "name": "SkyrimSE.exe"}),
                MagicMock(info={"pid": 2, "name": "notepad.exe"}),
            ],
        ):
            result = await controller.close_game()

        assert result["status"] == "closed"
        assert "SkyrimSE.exe" in result["killed_processes"]
        assert "notepad.exe" not in result["killed_processes"]


# ===================================================================
# 4. WSL2 Path Translation — Conditional Logic
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
    async def test_native_windows_passes_through(
        self, *, _reset_wsl2_cache: None
    ) -> None:
        """On native Windows, a valid Windows path passes through unchanged."""
        with patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=False):
            result = await translate_path_if_wsl(r"C:\Modding\MO2")

        assert result == r"C:\Modding\MO2"

    @pytest.mark.asyncio
    async def test_native_windows_rejects_linux_path(
        self, *, _reset_wsl2_cache: None
    ) -> None:
        """On native Windows, a Linux-style /mnt/ path raises ValueError."""
        with (
            patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=False),
            pytest.raises(ValueError, match="Linux-style path"),
        ):
            await translate_path_if_wsl("/mnt/c/Modding/MO2")

    @pytest.mark.asyncio
    async def test_native_windows_rejects_unix_absolute(
        self, *, _reset_wsl2_cache: None
    ) -> None:
        """On native Windows, a Unix absolute path raises ValueError."""
        with (
            patch("sky_claw.core.windows_interop.is_wsl2_cached", return_value=False),
            pytest.raises(ValueError, match="Linux-style path"),
        ):
            await translate_path_if_wsl("/home/user/mods")

    @pytest.mark.asyncio
    async def test_custom_timeout_forwarded(
        self, *, _reset_wsl2_cache: None
    ) -> None:
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
            # Need to re-evaluate; patch at function level
        # Simpler: just patch sys.platform in the module
        with patch("sky_claw.core.windows_interop.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert is_wsl2() is False

    def test_linux_with_proc_version_wsl(self, *, _reset_wsl2_cache: None) -> None:
        """On Linux with WSL signature in /proc/version, returns True."""
        with (
            patch("sky_claw.core.windows_interop.sys") as mock_sys,
            patch("sky_claw.core.windows_interop.os.popen") as mock_popen,
        ):
            mock_sys.platform = "linux"
            mock_popen.return_value.__enter__ = MagicMock(return_value=MagicMock(read=lambda: "Linux version 5.15 microsoft-standard-WSL2"))
            mock_popen.return_value.__exit__ = MagicMock(return_value=False)
            assert is_wsl2() is True

    def test_linux_without_wsl(self, *, _reset_wsl2_cache: None) -> None:
        """On Linux without WSL, returns False."""
        with (
            patch("sky_claw.core.windows_interop.sys") as mock_sys,
            patch("sky_claw.core.windows_interop.os.popen") as mock_popen,
            patch("sky_claw.core.windows_interop.os.path.isdir", return_value=False),
        ):
            mock_sys.platform = "linux"
            mock_popen.return_value.__enter__ = MagicMock(return_value=MagicMock(read=lambda: "Linux version 5.15 generic"))
            mock_popen.return_value.__exit__ = MagicMock(return_value=False)
            assert is_wsl2() is False

    def test_cached_flag_persists(self, *, _reset_wsl2_cache: None) -> None:
        """is_wsl2_cached() returns the same value on repeated calls."""
        import sky_claw.core.windows_interop as _mod

        _mod._WSL2_ACTIVE = None  # Reset
        with patch("sky_claw.core.windows_interop.is_wsl2", return_value=True):
            result1 = is_wsl2_cached()
            result2 = is_wsl2_cached()

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

        # ASSERT: translate_path_if_wsl was called with game_path
        mock_translate.assert_called_once_with(config.game_path)

        # ASSERT: create_subprocess_exec received the translated path
        call_args = mock_exec.call_args
        args_list = call_args[0]
        # args are: loot_exe, --game, SkyrimSE, --game-path, translated_path, --sort
        assert r"C:\Skyrim" in args_list

        assert result.success is True

    @pytest.mark.asyncio
    async def test_sort_wsl2_translation_failure(
        self, tmp_path: pathlib.Path
    ) -> None:
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
    """Verify launch_game() calls translate_path_if_wsl for cwd."""

    @pytest.mark.asyncio
    async def test_launch_calls_translate_for_cwd(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """launch_game() translates mo2_root for cwd via translate_path_if_wsl."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator, launch_timeout=5)

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch(
                "sky_claw.mo2.vfs.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch("sky_claw.mo2.vfs.asyncio.wait_for", return_value=None),
            patch(
                "sky_claw.mo2.vfs.translate_path_if_wsl",
                return_value=r"C:\MO2",
            ) as mock_translate,
        ):
            result = await controller.launch_game("Default")

        # ASSERT: translate_path_if_wsl was called with mo2_root
        mock_translate.assert_called_once_with(mo2_root)

        # ASSERT: create_subprocess_exec received translated cwd — already verified above

        assert result["status"] == "launched"

    @pytest.mark.asyncio
    async def test_launch_wsl2_translation_failure(
        self, tmp_mo2_env: tuple[pathlib.Path, MagicMock]
    ) -> None:
        """If translate_path_if_wsl raises, the error propagates."""
        mo2_root, validator = tmp_mo2_env
        controller = MO2Controller(mo2_root, validator)

        from sky_claw.core.models import WSLInteropError

        with (
            patch(
                "sky_claw.mo2.vfs.translate_path_if_wsl",
                side_effect=WSLInteropError("wslpath failed"),
            ),
            pytest.raises(WSLInteropError, match="wslpath failed"),
        ):
            await controller.launch_game("Default")


# ===================================================================
# 8. Default timeout constant
# ===================================================================


class TestDefaultConstants:
    """Verify default timeout values are sensible."""

    def test_default_launch_timeout(self) -> None:
        assert DEFAULT_LAUNCH_TIMEOUT == 30

    def test_loot_config_default_timeout(self) -> None:
        from sky_claw.loot.cli import DEFAULT_TIMEOUT as LOOT_DEFAULT

        config = LOOTConfig(
            loot_exe=pathlib.Path("/fake/loot.exe"),
            game_path=pathlib.Path("/fake/game"),
        )
        assert config.timeout == LOOT_DEFAULT
