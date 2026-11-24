"""WSL2/Windows interoperability layer.

Provides:
- WSL2 environment detection via :func:`is_wsl2`.
- Path translation (Linux -> Windows) via :func:`translate_path_if_wsl`.
- LOOT.exe execution via :class:`ModdingToolsAgent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING

from sky_claw.app.core.models import WSLInteropError
from sky_claw.sin_consola import creationflags_sin_consola

#: Windows ``CREATE_NO_WINDOW`` — evita popups de consola al invocar taskkill.
_CREATE_NO_WINDOW = 0x08000000

#: Timeout corto para ``taskkill`` — best-effort, no debe bloquear el cleanup.
_TASKKILL_TIMEOUT = 5.0

if TYPE_CHECKING:
    import pathlib as _pathlib

logger = logging.getLogger("SkyClaw.Interop")


# ---------------------------------------------------------------------------
# WSL2 detection
# ---------------------------------------------------------------------------


def is_wsl2() -> bool:
    """Return ``True`` when running under Windows Subsystem for Linux v2.

    Detection strategy (ordered by reliability):
      1. ``/proc/version`` contains ``microsoft`` and ``WSL2`` -- definitive.
      2. ``sys.platform`` is Linux **and** ``/mnt/c`` exists -- heuristic.

    The result is cached at module level to avoid repeated I/O on every call.
    """
    if sys.platform == "win32":
        return False

    # Fast path: read /proc/version directly (no shell invocation)
    try:
        version = pathlib.Path("/proc/version").read_text(encoding="utf-8", errors="replace")
        if "microsoft" in version.lower() and "wsl" in version.lower():
            return True
    except OSError:
        pass

    # Heuristic fallback: /mnt/c mount point exists
    return os.path.isdir("/mnt/c")


# Module-level cached flag -- evaluated once on import.
_WSL2_ACTIVE: bool | None = None
_WSL2_LOCK = asyncio.Lock()


async def is_wsl2_cached() -> bool:
    """Cached version of :func:`is_wsl2`.

    The detection I/O is performed only once and the result is memoised
    for the lifetime of the process.  Safe for concurrent async callers.
    """
    global _WSL2_ACTIVE  # noqa: PLW0603
    if _WSL2_ACTIVE is not None:
        return _WSL2_ACTIVE
    async with _WSL2_LOCK:
        if _WSL2_ACTIVE is None:
            _WSL2_ACTIVE = is_wsl2()
        return _WSL2_ACTIVE


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------


async def translate_path_if_wsl(path: str | _pathlib.Path, timeout: float = 10.0) -> str:
    """Translate *path* to Windows format when running under WSL2.

    If the current environment is **not** WSL2, the path is returned as-is
    after validating it does not contain Linux-style absolute prefixes
    (``/mnt/``) which would be invalid on native Windows.

    Args:
        path: Filesystem path to translate.
        timeout: Maximum seconds to wait for ``wslpath``.

    Returns:
        Windows-compatible path string (e.g. ``C:\\Modding\\MO2``).

    Raises:
        WSLInteropError: When ``wslpath`` fails or times out.
        ValueError: When running on native Windows but the path looks like
            a Linux absolute path (``/mnt/...``).
    """
    path_str = str(path)

    if await is_wsl2_cached():
        return await _translate_wsl_to_win(path_str, timeout=timeout)

    # Native Windows -- reject Linux-style paths
    if path_str.startswith("/mnt/") or (path_str.startswith("/") and not path_str.startswith("//")):
        msg = (
            f"Linux-style path '{path_str}' detected on native Windows. Provide a Windows path (e.g. C:\\Modding\\MO2)."
        )
        raise ValueError(msg)

    return path_str


async def _translate_wsl_to_win(wsl_path: str, timeout: float = 10.0) -> str:
    """Invoke ``wslpath -w`` to convert a Linux path to Windows format."""
    proc = await asyncio.create_subprocess_exec(
        "wslpath",
        "-w",
        wsl_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags_sin_consola(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        await _kill_and_reap(proc, timeout=3.0)
        raise WSLInteropError(f"wslpath timed out after {timeout}s for: {wsl_path}") from None

    if proc.returncode != 0:
        err_str = stderr.decode("utf-8", errors="replace").strip()
        raise WSLInteropError(f"wslpath failed: {err_str}")

    return stdout.decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def _kill_tree_windows(pid: int) -> None:
    """Best-effort: mata el ÁRBOL de procesos en Windows vía ``taskkill /T``.

    ``proc.kill()`` solo termina el hijo directo; herramientas externas pueden
    dejar nietos huérfanos. ``taskkill /F /T /PID`` termina el árbol completo.
    Cualquier fallo se ignora — la garantía dura sigue siendo ``proc.kill()`` + reap.
    El ``timeout`` acotado evita que un ``taskkill`` colgado bloquee el cleanup.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=_TASKKILL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # subprocess.TimeoutExpired es subclase de SubprocessError → cubierto.
        logger.debug("taskkill best-effort falló/timeout para PID %s: %s", pid, exc)


async def _kill_and_reap(proc: asyncio.subprocess.Process, timeout: float = 3.0) -> None:
    """Kill *proc* (y su árbol en Windows) and wait for it to exit.

    Logs a critical message if the process could not be reaped.
    """
    if sys.platform == "win32" and isinstance(getattr(proc, "pid", None), int):
        await asyncio.to_thread(_kill_tree_windows, proc.pid)
    try:
        proc.kill()
    except ProcessLookupError:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    if proc.returncode is None:
        logger.critical("Process PID %d could not be reaped after SIGKILL", proc.pid)


# ---------------------------------------------------------------------------
# ModdingToolsAgent (legacy compatibility)
# ---------------------------------------------------------------------------


class ModdingToolsAgent:
    """High-level agent for invoking Windows modding tools from WSL2."""

    @staticmethod
    async def translate_path_wsl_to_win(wsl_path: str, timeout: float = 10.0) -> str:
        r"""Use ``wslpath`` to convert a Linux path to Windows format (``C:\...``).

        Args:
            wsl_path: WSL path to translate (e.g. ``/mnt/c/...``).
            timeout: Timeout in seconds for ``wslpath`` invocation (default: 10s).

        Raises:
            WSLInteropError: If ``wslpath`` fails or times out.
        """
        return await _translate_wsl_to_win(wsl_path, timeout=timeout)
