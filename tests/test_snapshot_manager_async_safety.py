"""QA-2 — snapshot_manager no bloquea el event loop bajo carga (T1-02).

Verifica que las operaciones de FS pesadas (iterdir, stat, rglob) en
``create_snapshot`` y ``cleanup_old_snapshots`` esten delegadas a
``asyncio.to_thread`` para no congelar el loop.

La metrica usada es ``loop.slow_callback_duration``: si una sola callback
tarda mas de ese threshold, asyncio logea un warning. Verificamos que NO
emita warnings bajo carga concurrente esperable.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager


@pytest.mark.asyncio
async def test_create_50_snapshots_concurrently_no_blocking(tmp_path: Path, caplog) -> None:
    """50 snapshots concurrentes — ningun callback debe tardar > 200ms."""
    mgr = FileSnapshotManager(tmp_path / "snapshots")

    # Crear 50 archivos source.
    sources = []
    for i in range(50):
        p = tmp_path / f"src_{i}.bin"
        p.write_bytes(b"x" * 1024)
        sources.append(p)

    # Threshold bajo para detectar bloqueos largos.
    loop = asyncio.get_running_loop()
    original_slow = loop.slow_callback_duration
    loop.slow_callback_duration = 0.2

    try:
        with caplog.at_level(logging.WARNING, logger="asyncio"):
            results = await asyncio.gather(
                *(mgr.create_snapshot(s) for s in sources)
            )
    finally:
        loop.slow_callback_duration = original_slow

    assert len(results) == 50
    # Cero warnings de slow callbacks (asyncio.base_events logger "asyncio").
    slow_warns = [r for r in caplog.records if "took" in r.getMessage() and "slow" in r.getMessage().lower()]
    assert not slow_warns, f"slow callbacks detected: {slow_warns}"


@pytest.mark.asyncio
async def test_cleanup_old_snapshots_does_not_block(tmp_path: Path) -> None:
    """cleanup_old_snapshots con muchos archivos no debe bloquear el loop.

    Verifica que la operacion completa de cleanup permita a otras coroutinas
    avanzar entre awaits."""
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()

    # Crear un date_dir antiguo (2020-01-01) con varios archivos.
    old_dir = snap_dir / "2020-01-01"
    old_dir.mkdir()
    for i in range(30):
        (old_dir / f"file_{i}.bin").write_bytes(b"x" * 256)

    mgr = FileSnapshotManager(snap_dir)

    # Lanzar cleanup y una corutina "ticker" que cuenta cuantas veces le toca
    # avanzar. Si el cleanup bloqueara el loop, ticker no avanzaria.
    tick_count = 0
    cleanup_done = False

    async def ticker() -> None:
        nonlocal tick_count
        while not cleanup_done:
            await asyncio.sleep(0)
            tick_count += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await mgr.cleanup_old_snapshots(days_old=30, dry_run=False)
    finally:
        cleanup_done = True
        await ticker_task

    assert result.deleted_count == 30
    # El ticker debio avanzar al menos varias veces durante el cleanup,
    # demostrando que el loop no se bloqueo durante el escaneo.
    assert tick_count > 5, f"loop apareceria bloqueado: solo {tick_count} ticks"
    assert not old_dir.exists()


@pytest.mark.asyncio
async def test_create_snapshot_returns_correct_size(tmp_path: Path) -> None:
    """Verifica que el cambio de stat() sync a to_thread() preserva el tamano."""
    mgr = FileSnapshotManager(tmp_path / "snapshots")
    src = tmp_path / "src.bin"
    src.write_bytes(b"y" * 4096)

    info = await mgr.create_snapshot(src)
    assert info.size_bytes == 4096
