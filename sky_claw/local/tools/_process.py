"""Unified external-process execution helpers (M-1).

Consolidates the subprocess pattern that was duplicated across the local tool
runners (BodySlide, Pandora, Wrye Bash, xEdit, Synthesis): spawn via
``asyncio.create_subprocess_exec``, capture stdout/stderr under a bounded
timeout, and guarantee that no orphaned OS process survives a timeout,
cancellation, or error.

Design notes
------------
- ``kill_and_reap`` suppresses ONLY ``TimeoutError`` during the reap; a shutdown
  ``CancelledError`` raised while reaping must propagate (the process is already
  killed), matching the canonical ``antigravity.core.windows_interop._kill_and_reap``.
- This module lives in the ``local`` layer so the local runners share one copy
  without importing it from ``antigravity`` (layering: ``local`` may depend on
  ``antigravity``, not the reverse — so the antigravity layer keeps its own
  equivalent).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger("SkyClaw.Process")

#: Windows ``CREATE_NO_WINDOW`` — suppress console popups for GUI tools.
_CREATE_NO_WINDOW = 0x08000000

#: Default grace period (seconds) to reap a killed process before giving up.
_REAP_TIMEOUT = 3.0

#: Timeout corto para ``taskkill`` — best-effort, no debe bloquear el cleanup.
_TASKKILL_TIMEOUT = 5.0


def _kill_tree_windows(pid: int) -> None:
    """Best-effort: mata el ÁRBOL de procesos en Windows vía ``taskkill /T``.

    ``proc.kill()`` solo termina el hijo directo, dejando huérfanos a los nietos
    (DynDOLOD lanza TexGen; xEdit puede lanzar procesos auxiliares). ``taskkill
    /F /T /PID`` termina el árbol completo a partir del PID raíz.

    Best-effort por diseño: cualquier fallo (proceso ya muerto, ``taskkill``
    ausente) se ignora — la garantía dura sigue siendo ``proc.kill()`` + reap.
    El ``timeout`` acotado evita que un ``taskkill`` colgado (AV, PID en estado
    raro) bloquee indefinidamente el flujo de cleanup, aun corriendo en thread.
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
        # subprocess.TimeoutExpired es subclase de SubprocessError → cubierto:
        # si taskkill se cuelga, se aborta y seguimos con proc.kill() + reap.
        logger.debug("taskkill best-effort falló/timeout para PID %s: %s", pid, exc)


async def kill_and_reap(
    proc: asyncio.subprocess.Process | None,
    timeout: float = _REAP_TIMEOUT,
) -> None:
    """Kill *proc* (y su árbol en Windows) y reap para no dejar procesos huérfanos.

    Safe with ``None`` (process never spawned) and tolerant of an already-exited
    process. Suppresses only the reap ``TimeoutError`` — a ``CancelledError``
    raised while reaping propagates (the process is already killed), so shutdown
    cancellation is never swallowed.

    En Windows mata el árbol completo (``taskkill /T``) ANTES del ``proc.kill()``,
    mientras la relación padre-hijo sigue intacta, para no orfanar nietos.
    """
    if proc is None:
        return
    if sys.platform == "win32" and isinstance(getattr(proc, "pid", None), int):
        await asyncio.to_thread(_kill_tree_windows, proc.pid)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=timeout)


async def run_capture(
    args: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
) -> tuple[bytes, bytes, int]:
    """Run *args* to completion, capturing stdout/stderr under *timeout*.

    On Windows, ``CREATE_NO_WINDOW`` is applied so GUI tools do not flash a
    console. The child is always killed + reaped on timeout, cancellation, or
    error — never orphaned.

    Parameters
    ----------
    args:
        Full argv vector; ``args[0]`` is the executable.
    timeout:
        Seconds before the run is aborted (the process is killed first).
    cwd:
        Working directory for the child, or ``None``.

    Returns
    -------
    tuple[bytes, bytes, int]
        ``(stdout, stderr, returncode)`` on success.

    Raises
    ------
    FileNotFoundError
        If the executable does not exist (caller maps to a domain error).
    TimeoutError
        If execution exceeds *timeout* (the process is killed first).
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    if cwd is not None:
        kwargs["cwd"] = cwd

    proc: asyncio.subprocess.Process | None = None
    completed = False
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        completed = True
        return stdout, stderr, proc.returncode if proc.returncode is not None else 0
    finally:
        # Any non-normal exit — timeout, cancellation, or an I/O/pipe error after
        # spawn — must not leave the child running. The original exception (if
        # any) propagates unchanged; the caller maps it to a domain error.
        if not completed:
            await kill_and_reap(proc)


async def spawn_detached(
    args: list[str],
    *,
    cwd: str | None = None,
) -> asyncio.subprocess.Process:
    """Lanza *args* como proceso interactivo *detached* — fire-and-forget.

    Contrapartida de :func:`run_capture` para las GUIs que el usuario opera y
    cierra a mano (p. ej. abrir xEdit posicionado en un conflicto para forwardeo
    manual — T-29). Las dos diferencias clave:

    - **Sin PIPE**: no captura ``stdout``/``stderr``. En una sesión larga los
      pipes se llenarían y bloquearían al proceso; además no hay salida que
      parsear.
    - **Sin kill/reap**: el proceso debe SOBREVIVIR a esta llamada (es el editor
      abierto), así que ni se trackea ni se mata al retornar.

    En Windows aplica ``CREATE_NO_WINDOW`` para no parpadear una consola (la GUI
    del editor aparece igual; solo se suprime la consola de la que colgaría).

    Parameters
    ----------
    args:
        Full argv vector; ``args[0]`` is the executable.
    cwd:
        Working directory for the child, or ``None``.

    Returns
    -------
    asyncio.subprocess.Process
        El proceso lanzado (el caller puede leer su ``pid``).

    Raises
    ------
    FileNotFoundError
        If the executable does not exist (caller maps to a domain error).
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    if cwd is not None:
        kwargs["cwd"] = cwd

    return await asyncio.create_subprocess_exec(*args, **kwargs)
