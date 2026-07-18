"""Pruebas del límite transaccional SQLite compartido."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pytest

from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.antigravity.core.dlq_manager import DLQManager
from sky_claw.antigravity.core.event_bus import Event


class LifecycleEspia(DatabaseLifecycleManager):
    """Cuenta los limites transaccionales y su concurrencia real."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(
            db_paths=[db_path],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        self.entradas = 0
        self.activas = 0
        self.maximo_activas = 0

    @asynccontextmanager
    async def transaction(
        self,
        db_path: Path | str,
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        async with super().transaction(db_path) as conn:
            self.entradas += 1
            self.activas += 1
            self.maximo_activas = max(self.maximo_activas, self.activas)
            try:
                yield conn
            finally:
                self.activas -= 1


async def test_database_y_dlq_comparten_dueno_transaccional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DatabaseAgent y DLQ serializan todas sus escrituras por lifecycle."""
    db_path = tmp_path / "core-shared.db"
    lifecycle = LifecycleEspia(db_path)
    monkeypatch.setattr(
        "sky_claw.antigravity.core.database.DatabaseLifecycleManager",
        lambda **_kwargs: lifecycle,
    )

    database = DatabaseAgent(str(db_path))
    await database.init_db()

    async def handler(_event: Event) -> None:
        return None

    dlq = DLQManager(db_path, lambda _name: handler, lifecycle=lifecycle)
    event = Event(
        topic="core.test",
        payload={"valor": 1},
        timestamp_ms=1,
        source="test",
    )

    try:
        await asyncio.gather(
            database.set_memory("clave", "valor", 1.0),
            dlq.enqueue(event, handler, RuntimeError("fallo esperado")),
        )

        assert lifecycle.entradas >= 3
        assert lifecycle.maximo_activas == 1
        assert await database.get_memory("clave") == "valor"
        assert len(await dlq.list_pending()) == 1
    finally:
        await database.close()


async def test_schema_dlq_revierte_tabla_si_falla_creacion_de_indice(
    tmp_path: Path,
) -> None:
    """El schema DLQ completo es una sola unidad DDL rollbackable."""
    db_path = tmp_path / "schema-rollback.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE idx_dlq_status_retry (id INTEGER)")
        await conn.commit()

    dlq = DLQManager(db_path, lambda _name: None)
    with pytest.raises(sqlite3.OperationalError):
        await dlq._ensure_schema()

    async with (
        aiosqlite.connect(db_path) as conn,
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dead_letter_events'") as cursor,
    ):
        tabla_dlq = await cursor.fetchone()

    assert tabla_dlq is None
    assert not dlq._schema_ensured


async def test_fallback_revierte_si_commit_falla(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El fallback ejecuta rollback explicito si falla su commit."""
    db_path = tmp_path / "fallback-commit.db"
    conn = await aiosqlite.connect(db_path)
    await conn.execute("CREATE TABLE writes (value TEXT NOT NULL)")
    await conn.commit()
    commit_original = conn.commit
    rollback_original = conn.rollback
    rollbacks = 0

    @asynccontextmanager
    async def conexion_persistente() -> AsyncGenerator[aiosqlite.Connection, None]:
        yield conn

    async def commit_fallido() -> None:
        raise sqlite3.OperationalError("commit fallido")

    async def rollback_espiado() -> None:
        nonlocal rollbacks
        rollbacks += 1
        await rollback_original()

    dlq = DLQManager(db_path, lambda _name: None)
    monkeypatch.setattr(dlq, "_connect", conexion_persistente)
    monkeypatch.setattr(conn, "commit", commit_fallido)
    monkeypatch.setattr(conn, "rollback", rollback_espiado)

    try:
        with pytest.raises(sqlite3.OperationalError, match="commit fallido"):
            async with dlq._write_transaction() as transaction_conn:
                await transaction_conn.execute(
                    "INSERT INTO writes (value) VALUES (?)",
                    ("no confirmada",),
                )

        assert rollbacks == 1
        assert not conn.in_transaction
        async with conn.execute("SELECT value FROM writes") as cursor:
            assert await cursor.fetchall() == []
    finally:
        monkeypatch.setattr(conn, "commit", commit_original)
        await conn.close()


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


async def test_commit_fallido_hace_rollback_bajo_lock(
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

    async def commit_fallido() -> None:
        raise sqlite3.OperationalError("commit fallido")

    async def rollback_bloqueado() -> None:
        rollback_iniciado.set()
        await liberar_rollback.wait()
        await rollback_original()

    monkeypatch.setattr(conn, "commit", commit_fallido)
    monkeypatch.setattr(conn, "rollback", rollback_bloqueado)

    async def escribir() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("commit_fallido",),
            )

    async def esperar_lock() -> None:
        async with lifecycle.get_write_lock(db_path):
            lock_adquirido.set()

    tarea = asyncio.create_task(escribir())
    await _esperar(rollback_iniciado)
    assert conn.in_transaction
    assert not tarea.done()

    tarea_lock = asyncio.create_task(esperar_lock())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(lock_adquirido.wait(), timeout=0.05)

    liberar_rollback.set()
    with pytest.raises(sqlite3.OperationalError, match="commit fallido"):
        await tarea
    await tarea_lock

    assert not conn.in_transaction
    async with conn.execute("SELECT value FROM writes") as cursor:
        rows = await cursor.fetchall()
    assert rows == []


async def test_cancelación_externa_gana_a_fallo_de_commit_tras_rollback(
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
    rollback_iniciado = asyncio.Event()
    liberar_rollback = asyncio.Event()
    lock_adquirido = asyncio.Event()
    rollback_original = conn.rollback

    async def commit_bloqueado_y_fallido() -> None:
        commit_iniciado.set()
        await liberar_commit.wait()
        raise sqlite3.OperationalError("commit fallido tras cancelación")

    async def rollback_bloqueado() -> None:
        rollback_iniciado.set()
        await liberar_rollback.wait()
        await rollback_original()

    monkeypatch.setattr(conn, "commit", commit_bloqueado_y_fallido)
    monkeypatch.setattr(conn, "rollback", rollback_bloqueado)

    async def escribir() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("cancelada_con_fallo",),
            )

    async def esperar_lock() -> None:
        async with lifecycle.get_write_lock(db_path):
            lock_adquirido.set()

    tarea = asyncio.create_task(escribir())
    await _esperar(commit_iniciado)
    tarea.cancel()
    await asyncio.sleep(0)
    assert not tarea.done()

    liberar_commit.set()
    await _esperar(rollback_iniciado)
    assert conn.in_transaction
    assert not tarea.done()

    tarea_lock = asyncio.create_task(esperar_lock())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(lock_adquirido.wait(), timeout=0.05)

    liberar_rollback.set()
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await tarea
    await tarea_lock

    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    assert str(exc_info.value.__cause__) == "commit fallido tras cancelación"
    assert not conn.in_transaction
    async with conn.execute("SELECT value FROM writes") as cursor:
        rows = await cursor.fetchall()
    assert rows == []


@pytest.mark.parametrize(
    "segunda_cancelación",
    [False, True],
    ids=["sin_segunda_cancelación", "con_segunda_cancelación"],
)
async def test_cancelación_original_gana_si_commit_y_rollback_fallan(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
    monkeypatch: pytest.MonkeyPatch,
    segunda_cancelación: bool,
) -> None:
    lifecycle, db_path, conn = base_transaccional
    commit_iniciado = asyncio.Event()
    liberar_commit = asyncio.Event()
    rollback_iniciado = asyncio.Event()
    liberar_rollback = asyncio.Event()
    rollback_terminado = asyncio.Event()
    lock_adquirido = asyncio.Event()

    async def commit_bloqueado_y_fallido() -> None:
        commit_iniciado.set()
        await liberar_commit.wait()
        raise sqlite3.OperationalError("fallo de commit")

    async def rollback_bloqueado_y_fallido() -> None:
        rollback_iniciado.set()
        await liberar_rollback.wait()
        rollback_terminado.set()
        raise sqlite3.OperationalError("fallo de rollback")

    monkeypatch.setattr(conn, "commit", commit_bloqueado_y_fallido)
    monkeypatch.setattr(conn, "rollback", rollback_bloqueado_y_fallido)

    async def escribir() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("fallos_encadenados",),
            )

    async def esperar_lock() -> None:
        async with lifecycle.get_write_lock(db_path):
            lock_adquirido.set()

    tarea = asyncio.create_task(escribir())
    await _esperar(commit_iniciado)
    tarea.cancel("cancelación original")
    await asyncio.sleep(0)
    assert not tarea.done()

    liberar_commit.set()
    await _esperar(rollback_iniciado)
    tarea_lock = asyncio.create_task(esperar_lock())

    if segunda_cancelación:
        tarea.cancel("segunda cancelación")
        await asyncio.sleep(0)

    assert not tarea.done()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(lock_adquirido.wait(), timeout=0.05)

    liberar_rollback.set()
    with pytest.raises(asyncio.CancelledError, match="cancelación original") as exc_info:
        await tarea
    await tarea_lock

    assert rollback_terminado.is_set()
    rollback_error = exc_info.value.__cause__
    assert isinstance(rollback_error, sqlite3.OperationalError)
    assert str(rollback_error) == "fallo de rollback"
    commit_error = rollback_error.__cause__
    assert isinstance(commit_error, sqlite3.OperationalError)
    assert str(commit_error) == "fallo de commit"

    conn_segura = await lifecycle.get_connection(db_path)
    assert conn_segura is not conn


async def test_rollback_fallido_cuarentena_conexión_antes_de_reutilizarla(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, db_path, conn_afectada = base_transaccional
    commit_original = conn_afectada.commit
    rollback_original = conn_afectada.rollback
    commits = 0
    rollbacks = 0

    async def commit_falla_una_vez() -> None:
        nonlocal commits
        commits += 1
        if commits == 1:
            raise sqlite3.OperationalError("fallo de commit")
        await commit_original()

    async def rollback_falla_una_vez() -> None:
        nonlocal rollbacks
        rollbacks += 1
        if rollbacks == 1:
            raise sqlite3.OperationalError("fallo de rollback")
        await rollback_original()

    monkeypatch.setattr(conn_afectada, "commit", commit_falla_una_vez)
    monkeypatch.setattr(conn_afectada, "rollback", rollback_falla_una_vez)

    with pytest.raises(sqlite3.OperationalError, match="fallo de rollback"):
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("sucia",),
            )

    async with lifecycle.transaction(db_path) as conn_segura:
        assert conn_segura is not conn_afectada
        await conn_segura.execute(
            "INSERT INTO writes (value) VALUES (?)",
            ("segunda",),
        )

    async with conn_segura.execute("SELECT value FROM writes") as cursor:
        rows = await cursor.fetchall()
    assert [row[0] for row in rows] == ["segunda"]


async def test_cuarentena_cierra_afectada_sin_cerrar_reemplazo(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, db_path, conn_afectada = base_transaccional
    path_str = str(db_path.resolve())
    conn_reemplazo = await aiosqlite.connect(path_str)

    async def commit_fallido() -> None:
        raise sqlite3.OperationalError("fallo de commit")

    async def rollback_reemplaza_y_falla() -> None:
        lifecycle._connections[path_str] = conn_reemplazo
        raise sqlite3.OperationalError("fallo de rollback")

    monkeypatch.setattr(conn_afectada, "commit", commit_fallido)
    monkeypatch.setattr(conn_afectada, "rollback", rollback_reemplaza_y_falla)

    try:
        with pytest.raises(sqlite3.OperationalError, match="fallo de rollback"):
            async with lifecycle.transaction(db_path) as transaction_conn:
                await transaction_conn.execute(
                    "INSERT INTO writes (value) VALUES (?)",
                    ("no_confirmada",),
                )

        assert await lifecycle.get_connection(db_path) is conn_reemplazo
        async with conn_reemplazo.execute("SELECT 1") as cursor:
            assert await cursor.fetchone() == (1,)

        with pytest.raises(ValueError, match="no active connection"):
            await conn_afectada.execute("SELECT 1")
    finally:
        await conn_afectada.close()


async def test_cancelación_del_cuerpo_gana_si_rollback_falla(
    base_transaccional: tuple[
        DatabaseLifecycleManager,
        Path,
        aiosqlite.Connection,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, db_path, conn_afectada = base_transaccional
    fila_insertada = asyncio.Event()
    close_iniciado = asyncio.Event()
    liberar_close = asyncio.Event()
    lock_adquirido = asyncio.Event()
    close_original = conn_afectada.close

    async def rollback_fallido() -> None:
        raise sqlite3.OperationalError("fallo de rollback del cuerpo")

    async def close_bloqueado() -> None:
        close_iniciado.set()
        await liberar_close.wait()
        await close_original()

    monkeypatch.setattr(conn_afectada, "rollback", rollback_fallido)
    monkeypatch.setattr(conn_afectada, "close", close_bloqueado)

    async def escribir() -> None:
        async with lifecycle.transaction(db_path) as transaction_conn:
            await transaction_conn.execute(
                "INSERT INTO writes (value) VALUES (?)",
                ("cancelada",),
            )
            fila_insertada.set()
            await asyncio.Event().wait()

    async def esperar_lock() -> None:
        async with lifecycle.get_write_lock(db_path):
            lock_adquirido.set()

    tarea = asyncio.create_task(escribir())
    await _esperar(fila_insertada)
    tarea.cancel("cancelación del cuerpo")
    await _esperar(close_iniciado)
    assert not tarea.done()

    tarea_lock = asyncio.create_task(esperar_lock())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(lock_adquirido.wait(), timeout=0.05)

    liberar_close.set()
    with pytest.raises(asyncio.CancelledError, match="cancelación del cuerpo") as exc_info:
        await tarea
    await tarea_lock

    assert tarea.cancelled()
    rollback_error = exc_info.value.__cause__
    assert isinstance(rollback_error, sqlite3.OperationalError)
    assert str(rollback_error) == "fallo de rollback del cuerpo"

    async with lifecycle.transaction(db_path) as conn_segura:
        assert conn_segura is not conn_afectada
        await conn_segura.execute(
            "INSERT INTO writes (value) VALUES (?)",
            ("segunda",),
        )

    async with conn_segura.execute("SELECT value FROM writes") as cursor:
        rows = await cursor.fetchall()
    assert [row[0] for row in rows] == ["segunda"]
