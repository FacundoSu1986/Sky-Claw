"""P0.7 R-02 — I/O bloqueante en _watch_loop debe correr en asyncio.to_thread.

os.path.exists y os.stat son llamadas síncronas de I/O que bloquean el
event loop. En producción, un modlist.txt sobre una red lenta o un disco
ocupado puede bloquear toda la corrutina durante cientos de ms.

Contrato: ambas llamadas deben enrutarse a asyncio.to_thread, verificado
interceptando asyncio.to_thread y confirmando que os.path.exists y os.stat
son los callables que se pasan. La implementación real se ejecuta a través
del spy para preservar el comportamiento funcional.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.orchestrator.watcher_daemon import WatcherDaemon


def _make_daemon(modlist_path: Path, interval: float = 0.05) -> WatcherDaemon:
    db = MagicMock()
    db.get_memory = AsyncMock(return_value=None)
    db.set_memory = AsyncMock()
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    return WatcherDaemon(
        modlist_path=str(modlist_path),
        profile_name="Default",
        db=db,
        event_bus=event_bus,
        interval=interval,
    )


async def _correr_watch_loop_hasta(daemon: WatcherDaemon, dispatched: list[object], objetivo: object) -> None:
    """Corre ``_watch_loop`` hasta que *objetivo* aparezca en *dispatched*.

    Un sleep fijo es flaky en runners lentos (Windows CI): el loop puede no
    llegar al segundo ``to_thread`` antes del cancel. Esperar la condición con
    timeout generoso es determinista y rápido cuando el host es rápido.
    """
    task = asyncio.create_task(daemon._watch_loop())
    try:
        async with asyncio.timeout(5.0):
            while objetivo not in dispatched:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestWatcherDaemonIO:
    @pytest.mark.asyncio
    async def test_path_exists_dispatched_via_to_thread(self, tmp_path: Path) -> None:
        """os.path.exists must be dispatched via asyncio.to_thread, not called directly.

        Without asyncio.to_thread, os.path.exists would block the event loop
        thread during slow FS operations (network paths, busy disks).
        """
        modlist = tmp_path / "modlist.txt"
        modlist.write_text("", encoding="utf-8")

        dispatched: list[object] = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(func, *args, **kwargs):  # type: ignore[misc]
            dispatched.append(func)
            return await real_to_thread(func, *args, **kwargs)

        daemon = _make_daemon(modlist)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            await _correr_watch_loop_hasta(daemon, dispatched, os.path.exists)

        assert dispatched, "asyncio.to_thread was never called"
        assert os.path.exists in dispatched, (
            "os.path.exists must be dispatched via asyncio.to_thread; "
            f"got {[getattr(f, '__name__', repr(f)) for f in dispatched]!r}"
        )

    @pytest.mark.asyncio
    async def test_stat_dispatched_via_to_thread(self, tmp_path: Path) -> None:
        """os.stat must be dispatched via asyncio.to_thread, not called directly."""
        modlist = tmp_path / "modlist.txt"
        modlist.write_text("", encoding="utf-8")

        dispatched: list[object] = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(func, *args, **kwargs):  # type: ignore[misc]
            dispatched.append(func)
            return await real_to_thread(func, *args, **kwargs)

        daemon = _make_daemon(modlist)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            await _correr_watch_loop_hasta(daemon, dispatched, os.stat)

        assert dispatched, "asyncio.to_thread was never called"
        assert os.stat in dispatched, (
            "os.stat must be dispatched via asyncio.to_thread; "
            f"got {[getattr(f, '__name__', repr(f)) for f in dispatched]!r}"
        )
