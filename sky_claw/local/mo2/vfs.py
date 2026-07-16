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
        # M-8: ModOrganizer.exe lanzados por ESTA instancia, indexados por PID →
        # ``create_time`` (identidad estable contra reuso de PID del SO; ``None``
        # si no se pudo leer al lanzar). close_game mata SÓLO estos árboles (+
        # descendientes), no los procesos homónimos del host. Es un dict (no un
        # solo PID) para no perder un MO2 previo si se relanzó sin cerrar, y cada
        # PID se registra ANTES de verificar el spawn para no dejar huérfanos si
        # MO2 muere o la operación se cancela durante la verificación (§1.1/§1.2).
        self._launched_procs: dict[int, float | None] = {}
        # Serializa la región snapshot→matar→pop de close_game: dos close_game
        # concurrentes tomarían el mismo snapshot y matarían el árbol dos veces
        # (el segundo kill es no-op hoy, pero el lock lo blinda ante un futuro
        # sin GIL — verificación auditoría PRs #300-#304, follow-up H1).
        # launch_game NO toma este lock: registra el PID con una escritura de
        # dict plana (atómica) para no reabrir la ventana de huérfano de #302 si
        # la task se cancela esperando el lock (review Codex #305 C1).
        self._procs_lock = asyncio.Lock()

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

        # §1.1: registrar el PID ANTES de verificar el spawn. Si MO2 muere (o la
        # operación se cancela) entre la verificación y el registro, un PID sin
        # trackear quedaría FUERA del alcance de close_game → MO2/Skyrim huérfano
        # corriendo el precache contra la instalación real (análisis hostil §1.1).
        # Se guarda el create_time (identidad estable) para que close_game no
        # mate un proceso ajeno si el SO reusó el PID (review Codex #302).
        create_time: float | None = None
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            create_time = psutil.Process(proc.pid).create_time()
        # Escritura de dict PLANA (atómica bajo el GIL): NO se toma _procs_lock
        # acá. Un await entre el spawn y el registro reabriría la ventana de
        # huérfano que #302 cerró — si la task se cancela esperando el lock (un
        # close_game concurrente lo sostiene durante su kill loop), el PID nunca
        # se registra y el snapshot ya tomado de close_game no lo ve (review
        # Codex #305 C1). close_game snapshotea+popea solo su snapshot, así que
        # este registro concurrente sobrevive sin necesidad del lock.
        self._launched_procs[proc.pid] = create_time

        # TASK-011: Verify the process actually spawned (short grace period).
        try:
            await asyncio.wait_for(
                _verify_pid_alive(proc.pid),
                timeout=self._spawn_timeout,
            )
        except TimeoutError:
            # Spawn failed -- el proceso nunca apareció: dejar de trackearlo y
            # limpiar el defunto. Pop plano (atómico, idempotente).
            self._launched_procs.pop(proc.pid, None)
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
        """Attempt to forcefully close the game/MO2 tree launched by this controller.

        M-8: sólo se matan los árboles de los ModOrganizer.exe que ESTA instancia
        lanzó (los PIDs de :attr:`_launched_pids` + sus descendientes), no todos
        los procesos del host que se llamen ``skyrimse.exe``/``modorganizer.exe``.
        Así una segunda instancia de MO2/Skyrim del usuario no se ve afectada.

        §1.2: se matan TODOS los PIDs trackeados, no solo el último — si el
        crash-loop relanzó MO2 sin cerrarlo (o un cierre previo no alcanzó a
        limpiar), un MO2 viejo vivo no debe quedar huérfano (análisis hostil §1.2).

        TASK-011: The ``psutil`` iteration is wrapped in ``asyncio.to_thread`` to
        avoid blocking the event loop.

        Returns:
            Dict showing which processes were killed.
        """
        # Snapshot: se procesan SOLO estos PIDs. No se usa clear() porque un
        # launch_game concurrente podría registrar un PID nuevo mientras matamos
        # (await), y clear() lo descartaría dejándolo huérfano (review Codex
        # #302). Toda la región snapshot→matar→pop va bajo _procs_lock: dos
        # close_game concurrentes no toman el mismo snapshot ni matan el árbol
        # dos veces (el segundo ve el dict ya vaciado), y un launch_game
        # concurrente espera al lock y registra su PID DESPUÉS del pop, así que
        # no se pierde (se matará en la próxima corrida). Verificación auditoría
        # PRs #300-#304, follow-up H1.
        async with self._procs_lock:
            snapshot = sorted(self._launched_procs.items())
            if not snapshot:
                logger.info("close_game: no hay un juego lanzado por esta instancia; no-op.")
                return {"status": "closed", "killed_processes": []}

            killed: list[str] = []
            for pid, create_time in snapshot:
                killed.extend(await asyncio.to_thread(self._kill_process_tree, pid, create_time))
            for pid, _ in snapshot:
                self._launched_procs.pop(pid, None)
        pids = [pid for pid, _ in snapshot]
        logger.info("Closed game process tree(s) (pids=%s): %s", pids, killed)
        return {"status": "closed", "killed_processes": killed}

    @staticmethod
    def _kill_process_tree(pid: int, expected_create_time: float | None = None) -> list[str]:
        """Mata SÓLO el proceso ``pid`` y sus descendientes (no por nombre).

        Separado para envolver en ``asyncio.to_thread``. Best-effort: procesos ya
        muertos o sin permiso se ignoran.

        Si se pasa *expected_create_time*, se verifica la identidad del proceso
        antes de matar: un PID reusado por el SO (tras morir el MO2 original)
        tendría otro ``create_time`` → no se mata un proceso ajeno del usuario
        (review Codex #302). ``None`` (create_time no capturado al lanzar) omite
        el chequeo: el proceso casi siempre ya murió (``NoSuchProcess``).
        """
        killed: list[str] = []
        try:
            root = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return killed

        if expected_create_time is not None:
            try:
                if root.create_time() != expected_create_time:
                    return killed  # PID reusado: NO es el proceso que lanzamos
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
