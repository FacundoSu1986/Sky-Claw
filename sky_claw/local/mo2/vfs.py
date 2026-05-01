"""MO2 Virtual File System control.

This module manages the MO2 portable instance: modlist.txt
manipulation, tool execution (LOOT CLI, SSEEdit) via the
``ModOrganizer.exe`` proxy, and profile management.

TASK-011 enhancements:
- All subprocess invocations are fully async with ``asyncio.wait_for`` timeout.
- Zombie process prevention: ``proc.kill()`` + ``proc.wait()`` on timeout.
- Blocking ``psutil`` calls wrapped in ``asyncio.to_thread``.
- WSL2 conditional path translation via :func:`translate_path_if_wsl`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import uuid
from typing import TYPE_CHECKING, Any

import aiofiles
import psutil

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sky_claw.antigravity.security.path_validator import PathValidator

from sky_claw.antigravity.security.path_validator import assert_safe_component

logger = logging.getLogger(__name__)

# TASK-011: Default timeout for game-launch *spawn* verification (seconds).
# ModOrganizer.exe is a long-running GUI process; we only verify that the
# OS registered the PID, we do NOT wait for the process to finish.
DEFAULT_SPAWN_TIMEOUT = 5


async def _write_modlist_atomic(path: pathlib.Path, lines: list[str]) -> None:
    """Write *lines* to *path* with UTF-8 BOM, using an atomic tmp->rename swap.

    Each line is normalised to end with ``\\n`` so MO2 is happy on all
    platforms.  The file is first written to a uniquely-named temporary
    file with ``encoding="utf-8-sig"`` (which prepends the BOM
    automatically), then renamed over the original so the swap is atomic.
    """
    tmp: pathlib.Path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    normalised = [(line if line.endswith("\n") else line.rstrip("\r\n") + "\n") for line in lines]
    try:
        async with aiofiles.open(tmp, mode="w", encoding="utf-8-sig") as fh:
            await fh.writelines(normalised)
        await asyncio.to_thread(os.replace, tmp, path)
    except Exception:
        # Clean up orphaned tmp if write or rename fails
        with contextlib.suppress(OSError):
            await asyncio.to_thread(tmp.unlink, missing_ok=True)
        raise


class ModlistParseError(Exception):
    """Raised when a modlist.txt line cannot be parsed."""


class GameLaunchTimeoutError(RuntimeError):
    """Raised when the game launch subprocess fails to appear in the process table."""

    def __init__(self, timeout: int) -> None:
        super().__init__(f"Game launch timed out after {timeout}s")
        self.timeout = timeout


class MO2Controller:
    """Controller for a portable Mod Organizer 2 instance.

    TASK-011: All external process invocations are fully async with
    timeouts and zombie prevention.  Blocking I/O is wrapped in
    ``asyncio.to_thread``.
    """

    def __init__(
        self,
        mo2_root: pathlib.Path,
        path_validator: PathValidator,
        launch_timeout: int = DEFAULT_SPAWN_TIMEOUT,
    ) -> None:
        self._root = mo2_root.resolve()
        self._validator = path_validator
        self._modlist_lock = asyncio.Lock()
        self._spawn_timeout = launch_timeout

    @property
    def root(self) -> pathlib.Path:
        return self._root

    async def read_modlist(
        self,
        profile: str = "Default",
    ) -> AsyncGenerator[tuple[str, bool], None]:
        """Parse ``modlist.txt`` for *profile*, yielding ``(mod_name, enabled)``.

        Each line in MO2's ``modlist.txt`` follows the format::

            +ModName   (enabled)
            -ModName   (disabled)
            *ModName   (unmanaged / separator -- skipped)

        Yields one tuple per valid mod entry, consuming O(1) memory.
        Corrupt or unparseable lines are logged and skipped.
        """
        assert_safe_component(profile, field="profile")
        modlist_path = self._root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
            async for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                prefix = line[0]
                if prefix == "*":
                    continue

                if prefix not in ("+", "-"):
                    logger.warning("Skipping unparseable modlist line: %r", line)
                    continue

                mod_name = line[1:].strip()
                if not mod_name:
                    logger.warning("Skipping empty mod name in line: %r", line)
                    continue

                yield mod_name, (prefix == "+")

    async def add_mod_to_modlist(
        self,
        mod_name: str,
        profile: str = "Default",
    ) -> None:
        """Append *mod_name* as enabled (``+``) to the profile modlist.

        Skips if the mod is already present (enabled or disabled).

        Args:
            mod_name: The mod directory name (e.g. ``"Requiem"``).
            profile: MO2 profile name.
        """
        assert_safe_component(mod_name, field="mod_name")
        assert_safe_component(profile, field="profile")
        modlist_path = self._root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with self._modlist_lock:
            # Check if already present.
            existing_names: set[str] = set()
            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        line = raw_line.strip()
                        if line and line[0] in ("+", "-"):
                            existing_names.add(line[1:].strip())
            except FileNotFoundError:
                pass

            if mod_name in existing_names:
                logger.info("Mod %r already in modlist for profile %r", mod_name, profile)
                return

            async with aiofiles.open(validated, mode="a", encoding="utf-8") as fh:
                await fh.write(f"+{mod_name}\n")

            logger.info("Added +%s to modlist for profile %r", mod_name, profile)

    async def remove_mod_from_modlist(
        self,
        mod_name: str,
        profile: str = "Default",
    ) -> None:
        """Remove *mod_name* entirely from the profile modlist.

        Args:
            mod_name: The mod directory name.
            profile: MO2 profile name.
        """
        assert_safe_component(mod_name, field="mod_name")
        assert_safe_component(profile, field="profile")
        modlist_path = self._root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with self._modlist_lock:
            lines: list[str] = []
            found = False
            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        line = raw_line.strip()
                        if line and line[1:].strip() == mod_name and line[0] in ("+", "-"):
                            found = True
                            continue  # Skip this line
                        lines.append(raw_line)
            except FileNotFoundError:
                return

            if not found:
                return

            await _write_modlist_atomic(validated, lines)
            logger.info("Removed %s from modlist for profile %r", mod_name, profile)

    async def toggle_mod_in_modlist(
        self,
        mod_name: str,
        profile: str = "Default",
        enable: bool = True,
    ) -> None:
        """Toggle the enabled state of *mod_name* in the modlist.

        Args:
            mod_name: The mod directory name.
            profile: MO2 profile name.
            enable: True to enable (+), False to disable (-).
        """
        assert_safe_component(mod_name, field="mod_name")
        assert_safe_component(profile, field="profile")
        modlist_path = self._root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with self._modlist_lock:
            lines: list[str] = []
            changed = False
            target_prefix = "+" if enable else "-"

            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        line = raw_line.strip()
                        if line and line[1:].strip() == mod_name and line[0] in ("+", "-"):
                            if line[0] != target_prefix:
                                lines.append(f"{target_prefix}{mod_name}\n")
                                changed = True
                            else:
                                lines.append(raw_line)
                        else:
                            lines.append(raw_line)
            except FileNotFoundError:
                return

            if changed:
                await _write_modlist_atomic(validated, lines)
                state = "Enabled" if enable else "Disabled"
                logger.info("%s %s in modlist for profile %r", state, mod_name, profile)

    async def delete_mod_files(self, mod_name: str) -> None:
        """Delete the mod directory from MO2's mods folder entirely.

        Args:
            mod_name: The mod directory name.
        """
        assert_safe_component(mod_name, field="mod_name")
        mod_dir = self._root / "mods" / mod_name
        validated = self._validator.validate(mod_dir)

        if validated.exists() and validated.is_dir():
            import shutil

            await asyncio.to_thread(shutil.rmtree, validated, ignore_errors=True)
            logger.info("Deleted mod directory: %s", validated)

    async def launch_game(self, profile: str = "Default") -> dict[str, Any]:
        """Launch Skyrim via SKSE through MO2 for the given profile.

        TASK-011: Fully async spawn verification with zombie prevention.
        ModOrganizer.exe is a long-running GUI process; we only verify
        that the OS registered the PID within ``_spawn_timeout`` seconds.
        We do **not** block waiting for the process to finish.

        Args:
            profile: The MO2 profile to use.

        Returns:
            Dict containing the PID of the spawned ModOrganizer process.

        Raises:
            FileNotFoundError: If the MO2 executable does not exist.
            GameLaunchTimeoutError: If the launch process fails to appear
                in the process table within the configured timeout.
        """
        assert_safe_component(profile, field="profile")
        mo2_exe = self._root / "ModOrganizer.exe"
        validated_exe = self._validator.validate(mo2_exe)

        if not validated_exe.exists():
            raise FileNotFoundError(f"MO2 executable not found: {validated_exe}")

        # TASK-011: cwd must be the native filesystem path.
        # Under WSL2 this is the Linux path (/mnt/c/...); on native Windows
        # it is the Windows path (C:\...).  We do NOT translate it here.
        cwd_native = str(self._root)

        cmd = [str(validated_exe), "-p", profile, "moshortcut://SKSE"]

        logger.info("Launching game with command: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd_native,
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"MO2 executable not found: {validated_exe}") from None

        # TASK-011: Verify the process actually spawned (short grace period).
        try:
            await asyncio.wait_for(
                _verify_pid_alive(proc.pid),
                timeout=self._spawn_timeout,
            )
        except TimeoutError:
            # Spawn failed -- clean up the defunct process.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            logger.error(
                "Game launch process did not appear in process table for profile %r",
                profile,
            )
            raise GameLaunchTimeoutError(self._spawn_timeout) from None

        return {"pid": proc.pid, "status": "launched", "profile": profile}

    async def close_game(self) -> dict[str, Any]:
        """Attempt to forcefully close Skyrim SE and MO2.

        TASK-011: The ``psutil`` iteration is wrapped in
        ``asyncio.to_thread`` to avoid blocking the event loop.

        Returns:
            Dict showing which processes were killed.
        """
        killed = await asyncio.to_thread(self._kill_game_processes)
        logger.info("Closed game processes: %s", killed)
        return {"status": "closed", "killed_processes": killed}

    @staticmethod
    def _kill_game_processes() -> list[str]:
        """Synchronous helper to find and kill game-related processes.

        Separated for ``asyncio.to_thread`` wrapping.
        """
        killed: list[str] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = proc.info["name"]
                if name and name.lower() in (
                    "skyrimse.exe",
                    "skse64_loader.exe",
                    "modorganizer.exe",
                ):
                    proc.kill()
                    killed.append(name)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return killed


async def _verify_pid_alive(pid: int) -> None:
    """Poll until *pid* is visible in the process table.

    Uses ``asyncio.to_thread`` so the synchronous ``psutil.pid_exists``
    call does not block the event loop.
    """
    for _ in range(50):  # 5 seconds total @ 0.1 s
        exists = await asyncio.to_thread(psutil.pid_exists, pid)
        if exists:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError
