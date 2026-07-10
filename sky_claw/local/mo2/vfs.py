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
import shutil
import stat
import uuid
from typing import TYPE_CHECKING, Any

import aiofiles
import psutil

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sky_claw.antigravity.security.path_validator import PathValidator

from sky_claw.antigravity.security.path_validator import assert_safe_component

logger = logging.getLogger(__name__)


def _rmtree_force(path: pathlib.Path) -> None:
    """Recursively delete *path*, clearing Windows read-only attributes that make
    ``shutil.rmtree`` fail (read-only files are common in extracted mods / ``.git``).

    Unlike ``rmtree(ignore_errors=True)`` this never leaves a partially-deleted
    directory silently: it retries once after clearing read-only bits and lets a
    still-failing delete raise.
    """

    def _clear_readonly() -> None:
        for root, dirs, files in os.walk(path):
            for name in (*dirs, *files):
                p = os.path.join(root, name)
                with contextlib.suppress(OSError):
                    # Add the write bit, preserving existing mode (don't clobber
                    # read/execute — clobbering a dir's mode breaks rmtree on POSIX).
                    os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)

    try:
        shutil.rmtree(path)
    except OSError:
        _clear_readonly()
        shutil.rmtree(path)


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
        # M-8: PID del ModOrganizer.exe lanzado por ESTA instancia. close_game
        # mata sólo su árbol, no todos los procesos homónimos del host.
        self._launched_pid: int | None = None

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
            # Read the existing entries (and their order) so the atomic rewrite
            # keeps them; build existing_names in the same pass. The rewrite
            # normalizes line endings and always writes a UTF-8 BOM (not a
            # byte-for-byte copy of the original).
            lines: list[str] = []
            existing_names: set[str] = set()
            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        lines.append(raw_line)
                        stripped = raw_line.strip()
                        if stripped and stripped[0] in ("+", "-"):
                            existing_names.add(stripped[1:].strip())
            except FileNotFoundError:
                pass

            if mod_name in existing_names:
                logger.info("Mod %r already in modlist for profile %r", mod_name, profile)
                return

            # Atomic tmp->rename rewrite (with BOM), consistent with
            # remove/toggle — a non-atomic append could expose a partial line to
            # an external reader (watcher / MO2.exe) and omit the BOM (obs #192).
            lines.append(f"+{mod_name}\n")
            await _write_modlist_atomic(validated, lines)

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
            await asyncio.to_thread(_rmtree_force, validated)
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

        # M-8: recordar el PID lanzado para que close_game acote la terminación.
        self._launched_pid = proc.pid
        return {"pid": proc.pid, "status": "launched", "profile": profile}

    async def close_game(self) -> dict[str, Any]:
        """Attempt to forcefully close the game/MO2 tree launched by this controller.

        M-8: sólo se mata el árbol del ModOrganizer.exe que ESTA instancia lanzó
        (el PID guardado en :attr:`_launched_pid` + sus descendientes), no todos
        los procesos del host que se llamen ``skyrimse.exe``/``modorganizer.exe``.
        Así una segunda instancia de MO2/Skyrim del usuario no se ve afectada.

        TASK-011: The ``psutil`` iteration is wrapped in ``asyncio.to_thread`` to
        avoid blocking the event loop.

        Returns:
            Dict showing which processes were killed.
        """
        pid = self._launched_pid
        if pid is None:
            logger.info("close_game: no hay un juego lanzado por esta instancia; no-op.")
            return {"status": "closed", "killed_processes": []}

        killed = await asyncio.to_thread(self._kill_process_tree, pid)
        self._launched_pid = None
        logger.info("Closed game process tree (pid=%s): %s", pid, killed)
        return {"status": "closed", "killed_processes": killed}

    @staticmethod
    def _kill_process_tree(pid: int) -> list[str]:
        """Mata SÓLO el proceso ``pid`` y sus descendientes (no por nombre).

        Separado para envolver en ``asyncio.to_thread``. Best-effort: procesos ya
        muertos o sin permiso se ignoran.
        """
        killed: list[str] = []
        try:
            root = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return killed

        # Recolectar el árbol (hijos primero) antes de matar, para no perder la
        # relación padre-hijo cuando el padre muere.
        try:
            procs = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            procs = []
        procs.append(root)

        for proc in procs:
            try:
                name = proc.name()
                proc.kill()
                killed.append(f"{name}({proc.pid})")
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
