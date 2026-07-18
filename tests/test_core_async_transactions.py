"""Pruebas del límite transaccional SQLite compartido."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)


@pytest.fixture
async def base_transaccional(
    tmp_path: Path,
) -> AsyncGenerator[
    tuple[DatabaseLifecycleManager, Path, aiosqlite.Connection],
    None,
]:
    """Crea una base gestionada con una tabla mínima de escrituras."""
    db_path = tmp_path / "transactions.db"
    lifecycle = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    await lifecycle.init_all()
    conn = await lifecycle.get_connection(db_path)
    await conn.execute("CREATE TABLE writes (value TEXT NOT NULL)")
    await conn.commit()

    try:
        yield lifecycle, db_path, conn
    finally:
        await lifecycle.shutdown_all()


async def _esperar(evento: asyncio.Event) -> None:
    """Espera un punto de sincronización sin dejar un test colgado."""
    await asyncio.wait_for(evento.wait(), timeout=1)


async def test_transaction_serializa_escritores_y_aísla_rollback(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
) -> None:
    lifecycle, db_path, conn = base_transaccional
    primera_dentro = asyncio.Event()
    liberar_primera = asyncio.Event()
    segunda_dentro = asyncio.Event()

    async def primera() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("primera",),
            )
            primera_dentro.set()
            await liberar_primera.wait()
            raise RuntimeError("forzar rollback")

    async def segunda() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            segunda_dentro.set()
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("segunda",),
            )

    tarea_primera = asyncio.create_task(primera())
    await _esperar(primera_dentro)
    tarea_segunda = asyncio.create_task(segunda())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(segunda_dentro.wait(), timeout=0.05)

    liberar_primera.set()
    with pytest.raises(RuntimeError, match="forzar rollback"):
        await tarea_primera
    await tarea_segunda

    async with conn.execute("SELECT value FROM writes ORDER BY rowid") as cursor:
        rows = await cursor.fetchall()
    assert [row[0] for row in rows] == ["segunda"]


async def test_cancelación_espera_rollback_y_no_persiste_fila(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, db_path, conn = base_transaccional
    fila_insertada = asyncio.Event()
    rollback_iniciado = asyncio.Event()
    liberar_rollback = asyncio.Event()
    rollback_original = conn.rollback

    async def rollback_bloqueado() -> None:
        rollback_iniciado.set()
        await liberar_rollback.wait()
        await rollback_original()

    monkeypatch.setattr(conn, "rollback", rollback_bloqueado)

    async def escribir() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("cancelada",),
            )
            fila_insertada.set()
            await asyncio.Event().wait()

    tarea = asyncio.create_task(escribir())
    await _esperar(fila_insertada)
    tarea.cancel()
    await _esperar(rollback_iniciado)
    assert not tarea.done()

    liberar_rollback.set()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    async with conn.execute("SELECT value FROM writes") as cursor:
        rows = await cursor.fetchall()
    assert rows == []


async def test_cancelación_espera_commit_y_conserva_commit_point(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, db_path, conn = base_transaccional
    commit_iniciado = asyncio.Event()
    liberar_commit = asyncio.Event()
    commit_original = conn.commit

    async def commit_bloqueado() -> None:
        commit_iniciado.set()
        await liberar_commit.wait()
        await commit_original()

    monkeypatch.setattr(conn, "commit", commit_bloqueado)

    async def escribir() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("confirmada",),
            )

    tarea = asyncio.create_task(escribir())
    await _esperar(commit_iniciado)
    tarea.cancel()
    await asyncio.sleep(0)
    assert not tarea.done()

    liberar_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    async with conn.execute("SELECT value FROM writes") as cursor:
        rows = await cursor.fetchall()
    assert [row[0] for row in rows] == ["confirmada"]


async def test_cancelación_interna_de_commit_hace_rollback_bajo_lock(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, db_path, conn = base_transaccional
    rollback_iniciado = asyncio.Event()
    liberar_rollback = asyncio.Event()
    lock_adquirido = asyncio.Event()
    rollback_original = conn.rollback

    async def commit_cancelado() -> None:
        raise asyncio.CancelledError("commit cancelado")

    async def rollback_bloqueado() -> None:
        rollback_iniciado.set()
        await liberar_rollback.wait()
        await rollback_original()

    monkeypatch.setattr(conn, "commit", commit_cancelado)
    monkeypatch.setattr(conn, "rollback", rollback_bloqueado)

    async def escribir() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("commit_cancelado",),
            )

    async def esperar_lock() -> None:
        async with lifecycle.get_write_lock(db_path):
            lock_adquirido.set()

    tarea = asyncio.create_task(escribir())
    try:
        await _esperar(rollback_iniciado)
    except TimeoutError:
        with pytest.raises(asyncio.CancelledError, match="commit cancelado"):
            await tarea
        pytest.fail("la cancelación interna del commit no inició rollback")

    assert conn.in_transaction
    tarea_lock = asyncio.create_task(esperar_lock())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(lock_adquirido.wait(), timeout=0.05)

    liberar_rollback.set()
    with pytest.raises(asyncio.CancelledError, match="commit cancelado"):
        await tarea
    await tarea_lock

    assert not conn.in_transaction
    async with conn.execute("SELECT value FROM writes") as cursor:
        rows = await cursor.fetchall()
    assert rows == []
