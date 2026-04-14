"""MO2 Virtual File System control.

This module manages the MO2 portable instance: modlist.txt
manipulation, tool execution (LOOT CLI, SSEEdit) via the
``ModOrganizer.exe`` proxy, and profile management.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiofiles
import psutil

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from sky_claw.security.path_validator import PathValidator

logger = logging.getLogger(__name__)


class ModlistParseError(Exception):
    """Raised when a modlist.txt line cannot be parsed."""


class MO2Controller:
    """Controller for a portable Mod Organizer 2 instance."""

    def __init__(
        self,
        mo2_root: pathlib.Path,
        path_validator: PathValidator,
    ) -> None:
        self._root = mo2_root.resolve()
        self._validator = path_validator
        self._modlist_lock = asyncio.Lock()  # NUEVO: Lock de exclusión mutua

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
            *ModName   (unmanaged / separator – skipped)

        Yields one tuple per valid mod entry, consuming O(1) memory.
        Corrupt or unparseable lines are logged and skipped.
        """
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
        modlist_path = self._root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with self._modlist_lock:  # CORRECCIÓN: Escritura atómica
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
                logger.info(
                    "Mod %r already in modlist for profile %r", mod_name, profile
                )
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
        modlist_path = self._root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with self._modlist_lock:  # CORRECCIÓN: Escritura atómica
            lines: list[str] = []
            found = False
            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        line = raw_line.strip()
                        if (
                            line
                            and line[1:].strip() == mod_name
                            and line[0] in ("+", "-")
                        ):
                            found = True
                            continue  # Skip this line
                        lines.append(raw_line)
            except FileNotFoundError:
                return

            if not found:
                return

            async with aiofiles.open(validated, mode="w", encoding="utf-8") as fh:
                await fh.writelines(lines)
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
        modlist_path = self._root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with self._modlist_lock:  # CORRECCIÓN: Escritura atómica
            lines: list[str] = []
            changed = False
            target_prefix = "+" if enable else "-"

            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        line = raw_line.strip()
                        if (
                            line
                            and line[1:].strip() == mod_name
                            and line[0] in ("+", "-")
                        ):
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
                async with aiofiles.open(validated, mode="w", encoding="utf-8") as fh:
                    await fh.writelines(lines)
                state = "Enabled" if enable else "Disabled"
                logger.info("%s %s in modlist for profile %r", state, mod_name, profile)

    async def delete_mod_files(self, mod_name: str) -> None:
        """Delete the mod directory from MO2's mods folder entirely.

        Args:
            mod_name: The mod directory name.
        """
        mod_dir = self._root / "mods" / mod_name
        validated = self._validator.validate(mod_dir)

        if validated.exists() and validated.is_dir():
            import shutil

            await asyncio.to_thread(shutil.rmtree, validated, ignore_errors=True)
            logger.info("Deleted mod directory: %s", validated)

    async def launch_game(self, profile: str = "Default") -> dict[str, Any]:
        """Launch Skyrim via SKSE through MO2 for the given profile.

        Args:
            profile: The MO2 profile to use.

        Returns:
            Dict containing the PID of the spawned ModOrganizer process.
        """
        mo2_exe = self._root / "ModOrganizer.exe"
        validated_exe = self._validator.validate(mo2_exe)

        if not validated_exe.exists():
            raise FileNotFoundError(f"MO2 executable not found: {validated_exe}")

        cmd = [str(validated_exe), "-p", profile, "moshortcut://SKSE"]

        logger.info("Launching game with command: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self._root),
        )
        return {"pid": proc.pid, "status": "launched", "profile": profile}

    async def close_game(self) -> dict[str, Any]:
        """Attempt to forcefully close Skyrim SE and MO2.

        Returns:
            Dict showing which processes were killed.
        """
        killed = []
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

        logger.info("Closed game processes: %s", killed)
        return {"status": "closed", "killed_processes": killed}
