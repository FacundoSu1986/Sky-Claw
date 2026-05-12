"""P0.7 R-02 — I/O bloqueante en _watch_loop debe correr en asyncio.to_thread.

os.path.exists y os.stat son llamadas síncronas de I/O que bloquean el
event loop. En producción, un modlist.txt sobre una red lenta o un disco
ocupado puede bloquear toda la corrutina durante cientos de ms.

Contrato: ambas llamadas deben ejecutarse desde un thread distinto al
main thread (event loop), verificado con threading.current_thread().
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
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


class TestWatcherDaemonIO:
    @pytest.mark.asyncio
    async def test_path_exists_runs_off_event_loop_thread(self, tmp_path: Path) -> None:
        """os.path.exists must be called from a worker thread, not the event loop.

        Without asyncio.to_thread, os.path.exists would run on the event loop
        thread, blocking all other coroutines during slow FS operations.
        """
        modlist = tmp_path / "modlist.txt"
        modlist.write_text("", encoding="utf-8")

        called_from_threads: list[str] = []
        main_thread_name = threading.current_thread().name

        original_exists = Path.exists

        def recording_exists(self_path: Path) -> bool:
            called_from_threads.append(threading.current_thread().name)
            return original_exists(self_path)

        daemon = _make_daemon(modlist)

        with patch("os.path.exists", side_effect=lambda p: recording_exists(Path(p))):
            # Run one iteration of the watch loop then cancel.
            task = asyncio.create_task(daemon._watch_loop())
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert called_from_threads, "os.path.exists was never called"
        for thread_name in called_from_threads:
            assert thread_name != main_thread_name, (
                f"os.path.exists ran on the event loop thread ({thread_name!r}); "
                "it must be offloaded via asyncio.to_thread"
            )

    @pytest.mark.asyncio
    async def test_stat_runs_off_event_loop_thread(self, tmp_path: Path) -> None:
        """os.stat must be called from a worker thread, not the event loop."""
        modlist = tmp_path / "modlist.txt"
        modlist.write_text("", encoding="utf-8")

        called_from_threads: list[str] = []
        main_thread_name = threading.current_thread().name

        import os as _os

        os_stat_real = _os.stat

        def recording_stat(path: str) -> object:
            called_from_threads.append(threading.current_thread().name)
            return os_stat_real(path)

        daemon = _make_daemon(modlist)

        with patch("os.stat", side_effect=recording_stat):
            task = asyncio.create_task(daemon._watch_loop())
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert called_from_threads, "os.stat was never called"
        for thread_name in called_from_threads:
            assert thread_name != main_thread_name, (
                f"os.stat ran on the event loop thread ({thread_name!r}); it must be offloaded via asyncio.to_thread"
            )
