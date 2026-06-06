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
import sys
from typing import Any

#: Windows ``CREATE_NO_WINDOW`` — suppress console popups for GUI tools.
_CREATE_NO_WINDOW = 0x08000000

#: Default grace period (seconds) to reap a killed process before giving up.
_REAP_TIMEOUT = 3.0


async def kill_and_reap(
    proc: asyncio.subprocess.Process | None,
    timeout: float = _REAP_TIMEOUT,
) -> None:
    """Kill *proc* and reap it so no orphaned OS process survives.

    Safe with ``None`` (process never spawned) and tolerant of an already-exited
    process. Suppresses only the reap ``TimeoutError`` — a ``CancelledError``
    raised while reaping propagates (the process is already killed), so shutdown
    cancellation is never swallowed.
    """
    if proc is None:
        return
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
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await kill_and_reap(proc)
        raise
    except asyncio.CancelledError:
        await kill_and_reap(proc)
        raise
    return stdout, stderr, proc.returncode if proc.returncode is not None else 0
