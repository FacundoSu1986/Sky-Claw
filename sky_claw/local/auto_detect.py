"""Zero-config auto-detection of MO2, Skyrim SE, LOOT and SSEEdit.

Searches common Windows paths, the Windows registry, and Steam
library folders to locate the tools Sky-Claw needs.  Every search
has a hard timeout so the startup experience stays fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import pathlib
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from sky_claw.config import (
    LOOT_SEARCH_PATHS,
    MO2_COMMON_PATHS,
    SEARCH_TIMEOUT_SECONDS,
    SKYRIM_COMMON_PATHS,
    STEAM_DEFAULT_PATHS,
    XEDIT_SEARCH_PATHS,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def run_off_loop(fn: Callable[[], _T]) -> _T:
    """Ejecuta ``fn()`` en un hilo *daemon* puenteado al event loop.

    A diferencia de ``asyncio.to_thread`` —que despacha al default executor,
    cuyos hilos son NON-daemon y ``asyncio.run`` joinea sin timeout al cerrar—
    un hilo daemon no retiene el shutdown del loop ni del proceso: si una
    syscall de filesystem o de registro se cuelga, el worker se abandona al
    salir el intérprete en vez de colgarlo. El ``wait_for`` de cada detector
    puede así vencer y liberar el arranque aunque el probe siga bloqueado.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()
    ctx = contextvars.copy_context()

    def _resolver(valor: Any, es_error: bool) -> None:
        # Corre en el hilo del loop: si el wait_for ya venció, el future está
        # cancelado y no hay nada que resolver (set_* lanzaría InvalidStateError).
        if future.cancelled():
            return
        if es_error:
            future.set_exception(valor)
        else:
            future.set_result(valor)

    def _worker() -> None:
        try:
            resultado = ctx.run(fn)
        except BaseException as exc:  # noqa: BLE001 — se re-propaga vía el future
            # Si el loop ya cerró (p. ej. tras el timeout), call_soon_threadsafe
            # lanza RuntimeError; el worker daemon muere en silencio.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_resolver, exc, True)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_resolver, resultado, False)

    threading.Thread(target=_worker, name="autodetect", daemon=True).start()
    return await future


# Module-level aliases — used directly by AutoDetector methods so that
# tests can patch them via patch("sky_claw.local.auto_detect._MO2_COMMON", ...).
_MO2_COMMON: tuple[str, ...] = MO2_COMMON_PATHS
_STEAM_DEFAULT_PATHS: tuple[str, ...] = STEAM_DEFAULT_PATHS
_SKYRIM_COMMON: tuple[str, ...] = SKYRIM_COMMON_PATHS
_LOOT_COMMON: tuple[str, ...] = LOOT_SEARCH_PATHS
_XEDIT_COMMON: tuple[str, ...] = XEDIT_SEARCH_PATHS
_SEARCH_TIMEOUT: float = SEARCH_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Local AppData helper (patchable by tests)
# ---------------------------------------------------------------------------


def _local_appdata() -> pathlib.Path | None:
    r"""Return the user's local application data directory.

    On Windows this is typically ``C:\Users\<user>\AppData\Local``.
    Returns *None* when the home directory cannot be determined.
    """
    try:
        return pathlib.Path.home() / "AppData" / "Local"
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# Windows registry helper (stdlib winreg, Windows-only)
# ---------------------------------------------------------------------------

try:
    import winreg  # type: ignore[import-not-found]

    _HAS_WINREG = True
except ImportError:
    _HAS_WINREG = False


def _read_registry_value(hive: int, subkey: str, value_name: str) -> str | None:
    """Read a string value from the Windows registry, or *None*."""
    if not _HAS_WINREG:
        return None
    try:
        with winreg.OpenKey(hive, subkey) as key:
            data, _ = winreg.QueryValueEx(key, value_name)
            return str(data)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Steam library folders parser
# ---------------------------------------------------------------------------


def _parse_steam_library_folders(vdf_path: pathlib.Path) -> list[pathlib.Path]:
    """Return library root paths from ``libraryfolders.vdf``."""
    folders: list[pathlib.Path] = []
    if not vdf_path.exists():
        return folders
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip().strip('"')
            if line.startswith("path"):
                _, _, val = line.partition('"')
                val = val.strip().strip('"').replace("\\\\", "\\")
                if val:
                    folders.append(pathlib.Path(val))
    except OSError:
        pass
    return folders


def _find_skyrim_in_steam_libraries(
    libraries: list[pathlib.Path],
) -> pathlib.Path | None:
    """Check each Steam library for the Skyrim SE install."""
    for lib in libraries:
        candidate = lib / "steamapps" / "common" / "Skyrim Special Edition"
        if (candidate / "SkyrimSE.exe").exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Common search paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AutoDetector
# ---------------------------------------------------------------------------


class AutoDetector:
    """Searches the local system for MO2, Skyrim SE, LOOT and SSEEdit."""

    @staticmethod
    async def find_mo2() -> pathlib.Path | None:
        """Locate Mod Organizer 2 (portable install with profiles)."""
        return await asyncio.wait_for(
            run_off_loop(AutoDetector._find_mo2_inner),
            timeout=_SEARCH_TIMEOUT,
        )

    @staticmethod
    def _find_mo2_inner() -> pathlib.Path | None:
        # 1. Common portable paths.
        for raw in _MO2_COMMON:
            p = pathlib.Path(raw)
            if (p / "ModOrganizer.exe").exists():
                logger.info("Auto-detected MO2 at %s", p)
                return p

        # 2. AppData: %LOCALAPPDATA%\ModOrganizer\*
        local_appdata = _local_appdata()
        if local_appdata is not None:
            mo_dir = local_appdata / "ModOrganizer"
            if mo_dir.is_dir():
                for child in mo_dir.iterdir():
                    if child.is_dir() and (child / "ModOrganizer.exe").exists():
                        logger.info("Auto-detected MO2 at %s", child)
                        return child

        # 3. Program Files
        for pf in (r"C:\Program Files", r"C:\Program Files (x86)"):
            candidate = pathlib.Path(pf) / "Mod Organizer 2"
            if (candidate / "ModOrganizer.exe").exists():
                logger.info("Auto-detected MO2 at %s", candidate)
                return candidate

        return None

    @staticmethod
    async def find_skyrim() -> pathlib.Path | None:
        """Locate Skyrim Special Edition."""
        return await asyncio.wait_for(
            run_off_loop(AutoDetector._find_skyrim_inner),
            timeout=_SEARCH_TIMEOUT,
        )

    @staticmethod
    def _find_skyrim_inner() -> pathlib.Path | None:
        # 1. Windows registry
        reg_path = _read_registry_value(
            winreg.HKEY_LOCAL_MACHINE if _HAS_WINREG else 0,
            r"SOFTWARE\Bethesda Softworks\Skyrim Special Edition",
            "Installed Path",
        )
        if reg_path:
            p = pathlib.Path(reg_path)
            if (p / "SkyrimSE.exe").exists():
                logger.info("Auto-detected Skyrim (registry) at %s", p)
                return p

        # 2. Steam libraryfolders.vdf
        for steam_root in _STEAM_DEFAULT_PATHS:
            vdf = pathlib.Path(steam_root) / "steamapps" / "libraryfolders.vdf"
            libs = _parse_steam_library_folders(vdf)
            if libs:
                found = _find_skyrim_in_steam_libraries(libs)
                if found:
                    logger.info("Auto-detected Skyrim (Steam) at %s", found)
                    return found

        # 3. Common direct paths
        for raw in _SKYRIM_COMMON:
            p = pathlib.Path(raw)
            if (p / "SkyrimSE.exe").exists():
                logger.info("Auto-detected Skyrim at %s", p)
                return p

        return None

    @staticmethod
    async def find_loot() -> pathlib.Path | None:
        """Locate the LOOT executable."""
        return await asyncio.wait_for(
            run_off_loop(AutoDetector._find_loot_inner),
            timeout=_SEARCH_TIMEOUT,
        )

    @staticmethod
    def _find_loot_inner() -> pathlib.Path | None:
        for raw in _LOOT_COMMON:
            p = pathlib.Path(raw)
            exe = p / "LOOT.exe"
            if exe.exists():
                logger.info("Auto-detected LOOT at %s", exe)
                return exe

        # Check PATH
        import shutil

        loot_in_path = shutil.which("LOOT")
        if loot_in_path:
            logger.info("Auto-detected LOOT in PATH: %s", loot_in_path)
            return pathlib.Path(loot_in_path)

        return None

    @staticmethod
    async def find_xedit() -> pathlib.Path | None:
        """Locate the SSEEdit executable."""
        return await asyncio.wait_for(
            run_off_loop(AutoDetector._find_xedit_inner),
            timeout=_SEARCH_TIMEOUT,
        )

    @staticmethod
    def _find_xedit_inner() -> pathlib.Path | None:
        for raw in _XEDIT_COMMON:
            p = pathlib.Path(raw)
            exe = p / "SSEEdit.exe"
            if exe.exists():
                logger.info("Auto-detected SSEEdit at %s", exe)
                return exe

        import shutil

        xedit_in_path = shutil.which("SSEEdit")
        if xedit_in_path:
            logger.info("Auto-detected SSEEdit in PATH: %s", xedit_in_path)
            return pathlib.Path(xedit_in_path)

        return None

    @staticmethod
    async def detect_all() -> dict[str, str | None]:
        """Run all detectors concurrently and return a summary dict."""
        mo2, skyrim, loot, xedit = await asyncio.gather(
            AutoDetector.find_mo2(),
            AutoDetector.find_skyrim(),
            AutoDetector.find_loot(),
            AutoDetector.find_xedit(),
            return_exceptions=True,
        )

        def _to_str(val: Any) -> str | None:
            if isinstance(val, pathlib.Path):
                return str(val)
            if isinstance(val, BaseException):
                logger.warning("Auto-detect error: %s", val)
                return None
            return None

        return {
            "mo2_root": _to_str(mo2),
            "skyrim_path": _to_str(skyrim),
            "loot_exe": _to_str(loot),
            "xedit_exe": _to_str(xedit),
        }
