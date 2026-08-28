"""PR-1b2b — carreras conductuales del boundary para Registry y Journal.

Cuando AsyncModRegistry / OperationJournal reciben un DatabaseLifecycleManager,
todo su SQL lifecycle-backed debe correr dentro de ``operation()`` (lecturas/DDL)
o ``transaction()`` (mutaciones). Este módulo demuestra las propiedades por
CONDUCTA observable (barreras ``asyncio.Event`` + ``ceder()``), no por ancla
sintáctica:

- shutdown no puede cerrar la conexión debajo de SQL en vuelo;
- cancelación pre-commit deja ``in_transaction=False`` y nada durable;
- el estado Python del journal (``_current_transaction``) solo avanza tras el
  commit durable;
- no hay doble adquisición del lock de path (un doble lock se cuelga y lo
  atrapa el watchdog ``DEADLINE``);
- llegada tardía propaga ``DatabaseLifecycleShuttingDownError``.

Archivo nuevo deliberado: sumar estas carreras a
``test_lifecycle_consumers_boundary.py`` (DatabaseAgent/DLQ/Governance + anclas
AST) lo habría dejado ilegible. Sin sleeps de sincronización (solo ``sleep(0)``
como yield del scheduler); los timeouts son watchdog.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from sky_claw.app.core import db_lifecycle as db_lifecycle_mod
from sky_claw.app.core.db_lifecycle import (
    DatabaseConnectionQuarantinedError,
    DatabaseConnectionReplacedError,
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
    DatabaseLifecycleShuttingDownError,
    DatabaseShutdownIncompleteError,
)
from sky_claw.app.db import journal as journal_mod
from sky_claw.app.db.async_registry import AsyncModRegistry, DatabaseError
from sky_claw.app.db.journal import (
    _FALLOS_DE_CLEANUP_ABSORBIBLES,
    JournalTransactionError,
    OperationJournal,
    OperationStatus,
    OperationType,
    TransactionStatus,
)

DEADLINE = 5.0


async def ceder(veces: int = 40) -> None:
    """Vacía la cola de listos del loop (no es sleep de sincronización)."""
    for _ in range(veces):
        await asyncio.sleep(0)


def _mgr() -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )


@pytest.fixture
async def lifecycle() -> AsyncIterator[DatabaseLifecycleManager]:
    """Lifecycle fresco por test; shutdown_all() garantizado en el teardown.

    Idempotente: si el test ya cerró (late-admission/ownership), el segundo
    shutdown completa sin error.
    """
    manager = _mgr()
    try:
        yield manager
    finally:
        await manager.shutdown_all()


class _ResultInterpuesto:
    """Proxy de ``aiosqlite.Result`` (awaitable + async context manager) que
    interpone hooks asíncronos antes y/o después del SQL real.

    ``Connection.execute`` devuelve un ``Result``: devolver una coroutine cruda
    rompería el ``async with conn.execute(...)`` de los consumers.
    """

    def __init__(
        self,
        resultado: Any,
        *,
        antes: Any = None,
        despues: Any = None,
    ) -> None:
        self._resultado = resultado
        self._antes = antes
        self._despues = despues
        self._objeto: Any = None

    def __await__(self) -> Any:
        return self._esperar().__await__()

    async def _esperar(self) -> Any:
        if self._antes is not None:
            await self._antes()
        objeto = await self._resultado
        if self._despues is not None:
            await self._despues()
        return objeto

    async def __aenter__(self) -> Any:
        self._objeto = await self._esperar()
        return self._objeto

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._objeto is not None and hasattr(self._objeto, "close"):
            await self._objeto.close()


def _ejecucion_con_barrera(
    conn: aiosqlite.Connection,
    fragmento: str,
    entro: asyncio.Event,
    puerta: asyncio.Event,
    *,
    tras_ejecutar: bool = False,
) -> None:
    """Bloquea ``conn.execute`` cuando el SQL contiene ``fragmento``.

    ``tras_ejecutar=True`` ejecuta el SQL REAL primero y bloquea después: deja
    el DML realizado con la transacción implícita abierta, la ventana exacta
    para cancelar entre DML y commit.
    """
    ejecucion_real = conn.execute

    async def antes() -> None:
        if not tras_ejecutar:
            entro.set()
            await puerta.wait()

    async def despues() -> None:
        if tras_ejecutar:
            entro.set()
            await puerta.wait()

    def espia(sql: str, *args: Any, **kwargs: Any) -> Any:
        if isinstance(sql, str) and fragmento in sql:
            return _ResultInterpuesto(
                ejecucion_real(sql, *args, **kwargs),
                antes=antes,
                despues=despues,
            )
        return ejecucion_real(sql, *args, **kwargs)

    conn.execute = espia  # type: ignore[method-assign]


# ===========================================================================
# REGISTRY — shutdown vs SQL en vuelo
# ===========================================================================


async def test_registry_read_suspended_blocks_shutdown(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Una lectura de ``get_mod`` suspendida DENTRO de operation() mantiene la
    admisión: shutdown_all() no completa hasta que la lectura sale.

    Reversión (REV-REG-READ): ``get_connection()`` + SELECT suelto suelta la
    admisión antes del SQL → shutdown cierra debajo → el SELECT falla → ROJO.
    """
    reg = AsyncModRegistry(tmp_path / "reg_read.db", lifecycle=lifecycle)
    await reg.open()
    await reg.upsert_mod(nexus_id=1, name="ReadMe")
    conn = reg._conn
    assert conn is not None

    entro, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(conn, "SELECT * FROM mods", entro, puerta)
    lectura: dict[str, object] = {"nombre": None, "error": None}

    async def lector() -> None:
        try:
            fila = await reg.get_mod(1)
            lectura["nombre"] = fila["name"] if fila is not None else None
        except Exception as error:  # noqa: BLE001 — el cierre prematuro es justo lo que observamos
            lectura["error"] = f"{type(error).__name__}: {error}"

    t_lec = asyncio.create_task(lector())
    await entro.wait()
    try:
        assert lifecycle._admitted >= 1, "la lectura soltó la admisión antes de terminar el SQL"
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not t_sd.done(), "shutdown completó con una lectura admitida en vuelo"
    finally:
        puerta.set()

    await asyncio.wait_for(t_lec, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert lectura["error"] is None, f"cierre debajo del SELECT: {lectura['error']}"
    assert lectura["nombre"] == "ReadMe"


async def test_registry_write_suspended_blocks_shutdown(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Una mutación suspendida DENTRO de transaction() no deja que shutdown
    cierre la conexión debajo del SQL; la fila termina durable.

    Reversión (REV-REG-WRITE): ``_write_lock`` + commit manual suelta la
    admisión antes del SQL → shutdown cierra debajo → ROJO conductual.
    """
    reg = AsyncModRegistry(tmp_path / "reg_write.db", lifecycle=lifecycle)
    await reg.open()
    conn = reg._conn
    assert conn is not None

    entro, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(conn, "INSERT INTO mods", entro, puerta)

    t_op = asyncio.create_task(reg.upsert_mod(nexus_id=7, name="WriteMe"))
    await entro.wait()
    try:
        assert lifecycle._admitted >= 1, "la mutación soltó la admisión antes del SQL"
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not t_sd.done(), "shutdown completó con una mutación admitida en vuelo"
    finally:
        puerta.set()

    mod_id = await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert mod_id >= 1
    # La fila sobrevivió al shutdown: reabrir explícitamente y releer.
    await lifecycle.init_all()
    fila = await reg.get_mod(7)
    assert fila is not None and fila["name"] == "WriteMe"


async def test_registry_cancel_before_commit_rolls_back(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Cancelación tras el DML y antes del commit: rollback, in_transaction=False
    y la mutación no es durable (sin dirty bleed posterior).

    Reversión (REV-REG-TRANSACTION): commit manual bajo ``_write_lock`` deja la
    transacción colgante en la conexión compartida → ``in_transaction`` queda
    True → ROJO.
    """
    reg = AsyncModRegistry(tmp_path / "reg_cancel.db", lifecycle=lifecycle)
    await reg.open()
    conn = reg._conn
    assert conn is not None

    dml_ejecutado, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(conn, "INSERT INTO mods", dml_ejecutado, puerta, tras_ejecutar=True)

    tarea = asyncio.create_task(reg.upsert_mod(nexus_id=99, name="Cancelada"))
    await dml_ejecutado.wait()
    assert conn.in_transaction, "premisa: el DML abrió la transacción implícita"
    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert conn.in_transaction is False, "quedó una transacción colgante en la conexión compartida"
    assert await reg.get_mod(99) is None, "la fila cancelada quedó durable (dirty bleed)"


async def test_registry_batch_partial_rollback(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """executemany parcial + excepción: ninguna fila parcial queda durable."""
    reg = AsyncModRegistry(tmp_path / "reg_batch.db", lifecycle=lifecycle)
    await reg.open()

    filas: list[tuple[int | None, str, str, str, str, str, bool, bool]] = [
        (1, "ModA", "1.0", "", "", "", False, False),
        (None, "Rota", "1.0", "", "", "", False, False),  # nexus_id NOT NULL
        (3, "ModC", "1.0", "", "", "", False, False),
    ]
    with pytest.raises(DatabaseError):
        await reg.upsert_mods_batch(filas)  # type: ignore[arg-type]

    assert await reg.get_mod(1) is None, "fila anterior al fallo quedó durable"
    assert await reg.get_mod(3) is None, "fila posterior al fallo quedó durable"


async def test_registry_late_admission_propagates(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Con el shutdown ya iniciado, una lectura nueva propaga
    DatabaseLifecycleShuttingDownError (fail-closed, sin fallback silencioso)."""
    reg = AsyncModRegistry(tmp_path / "reg_late.db", lifecycle=lifecycle)
    await reg.open()
    await lifecycle.shutdown_all()

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await reg.get_mod(1)


async def test_registry_two_wrappers_no_interleave(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Dos wrappers sobre el mismo lifecycle y path no intercalan transacciones.

    El watchdog DEADLINE también atrapa un doble lock (self._write_lock +
    transaction() = self-deadlock) sin dejar la suite colgada.
    """
    db_path = tmp_path / "reg_par.db"
    r1 = AsyncModRegistry(db_path, lifecycle=lifecycle)
    r2 = AsyncModRegistry(db_path, lifecycle=lifecycle)
    await r1.open()
    await r2.open()
    conn = r1._conn
    assert conn is not None
    assert r1._conn is r2._conn, "premisa: la conexión del lifecycle es singleton por path"
    assert r1._write_lock is r2._write_lock, "premisa: el lock acompaña a la conexión"

    en_vuelo = 0
    maximo = 0
    ejecucion_real = conn.execute

    async def ventana_antes() -> None:
        nonlocal en_vuelo, maximo
        en_vuelo += 1
        maximo = max(maximo, en_vuelo)

    async def ventana_despues() -> None:
        nonlocal en_vuelo
        await asyncio.sleep(0)
        en_vuelo -= 1

    def espia(sql: str, *args: Any, **kwargs: Any) -> Any:
        if isinstance(sql, str) and "INSERT INTO mods" in sql:
            return _ResultInterpuesto(
                ejecucion_real(sql, *args, **kwargs),
                antes=ventana_antes,
                despues=ventana_despues,
            )
        return ejecucion_real(sql, *args, **kwargs)

    conn.execute = espia  # type: ignore[method-assign]

    wrappers = [r1, r2] * 6
    tareas = [asyncio.create_task(wrapper.upsert_mod(nexus_id=i, name=f"mod{i}")) for i, wrapper in enumerate(wrappers)]
    await asyncio.wait_for(asyncio.gather(*tareas), timeout=DEADLINE)

    assert maximo == 1, f"dos wrappers intercalaron transacciones (max={maximo})"
    assert await r1.get_all_nexus_ids() == set(range(12))


async def test_registry_schema_under_boundary(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Un open() tardío no puede commitear el trabajo a medias de otro wrapper
    sobre la conexión compartida: el executescript del schema corre bajo el
    boundary del path."""
    db_path = tmp_path / "reg_schema.db"
    r1 = AsyncModRegistry(db_path, lifecycle=lifecycle)
    await r1.open()
    conexion = r1._conn
    assert conexion is not None

    schema_ejecutado = asyncio.Event()
    ejecucion_real = conexion.executescript

    async def executescript_observado(sql: str) -> None:
        schema_ejecutado.set()
        await ejecucion_real(sql)

    conexion.executescript = executescript_observado  # type: ignore[method-assign]

    r2 = AsyncModRegistry(db_path, lifecycle=lifecycle)
    apertura: asyncio.Task[None]
    async with lifecycle.get_write_lock(db_path):
        await conexion.execute("BEGIN")
        await conexion.execute("INSERT INTO mods (nexus_id, name) VALUES (1, 'parcial')")
        assert conexion._conn.in_transaction, "premisa: hay una transacción abierta"

        apertura = asyncio.create_task(r2.open())
        await ceder()

        assert not schema_ejecutado.is_set(), "r2 ejecutó el schema sin el boundary del path"
        assert not apertura.done(), "r2.open() no esperó el boundary"
        assert conexion._conn.in_transaction, "la transacción de r1 dejó de estar pendiente"
        await conexion.rollback()

    await asyncio.wait_for(apertura, timeout=DEADLINE)
    conexion.executescript = ejecucion_real  # type: ignore[method-assign]

    assert await r1.get_all_nexus_ids() == set(), "el trabajo parcial de r1 sobrevivió"


async def test_registry_close_does_not_close_lifecycle_connection(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """close() del wrapper borra su referencia pero NO cierra la conexión
    lifecycle-owned; shutdown_all() sí la cierra físicamente."""
    reg = AsyncModRegistry(tmp_path / "reg_own.db", lifecycle=lifecycle)
    await reg.open()
    conn = reg._conn
    assert conn is not None
    await reg.close()

    async with conn.execute("SELECT 1") as cur:
        assert await cur.fetchone() is not None

    await lifecycle.shutdown_all()
    with pytest.raises(ValueError, match="no active connection"):
        async with conn.execute("SELECT 1") as cur:
            await cur.fetchone()


# ===========================================================================
# JOURNAL — shutdown vs SQL en vuelo
# ===========================================================================


async def test_journal_read_suspended_blocks_shutdown(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Una lectura suspendida dentro de operation() bloquea el shutdown.

    Reversión (REV-JRN-READ): SELECT suelto tras get_connection suelta la
    admisión antes del SQL → shutdown cierra debajo → ROJO.
    """
    journal = OperationJournal(tmp_path / "jrn_read.db", lifecycle=lifecycle)
    await journal.open()
    tx_id = await journal.begin_transaction("para leer")
    db = journal._db
    assert db is not None

    entro, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(db, "FROM transactions", entro, puerta)
    leido: dict[str, object] = {"error": None, "status": None}

    async def lector() -> None:
        try:
            tx = await journal.get_transaction(tx_id)
            leido["status"] = tx.status.value if tx is not None else None
        except Exception as error:  # noqa: BLE001 — el cierre prematuro es lo que observamos
            leido["error"] = f"{type(error).__name__}: {error}"

    t_lec = asyncio.create_task(lector())
    await entro.wait()
    try:
        assert lifecycle._admitted >= 1
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not t_sd.done(), "shutdown completó con una lectura admitida en vuelo"
    finally:
        puerta.set()

    await asyncio.wait_for(t_lec, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert leido["error"] is None, f"cierre debajo del SELECT: {leido['error']}"
    assert leido["status"] == "pending"


async def test_journal_write_suspended_blocks_shutdown(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """begin_transaction suspendida dentro de transaction() bloquea el shutdown;
    la transacción termina durable.

    Reversión (REV-JRN-WRITE): ``_lock`` + commit manual → ROJO conductual.
    """
    journal = OperationJournal(tmp_path / "jrn_write.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None

    entro, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(db, "INSERT INTO transactions", entro, puerta)

    t_op = asyncio.create_task(journal.begin_transaction("suspendida"))
    await entro.wait()
    try:
        assert lifecycle._admitted >= 1
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not t_sd.done(), "shutdown completó con una mutación admitida en vuelo"
    finally:
        puerta.set()

    tx_id = await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    # La transacción sobrevivió al shutdown: reabrir explícitamente y releer.
    await lifecycle.init_all()
    tx = await journal.get_transaction(tx_id)
    assert tx is not None and tx.status is TransactionStatus.PENDING


async def test_journal_cancel_before_commit_rolls_back(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Cancelación tras el INSERT y antes del commit: rollback, sin fila durable
    y sin transacción colgante en la conexión compartida."""
    journal = OperationJournal(tmp_path / "jrn_cancel.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None

    dml_ejecutado, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(db, "INSERT INTO transactions", dml_ejecutado, puerta, tras_ejecutar=True)

    tarea = asyncio.create_task(journal.begin_transaction("cancelada"))
    await dml_ejecutado.wait()
    assert db.in_transaction, "premisa: el INSERT abrió la transacción implícita"
    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert db.in_transaction is False, "quedó una transacción colgante en la conexión compartida"
    assert journal._current_transaction is None, "el estado avanzó sin commit durable"
    assert await journal.list_recent_transactions(limit=50) == [], "la fila cancelada quedó durable"


async def test_journal_begin_sets_state_after_commit(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JRN-BEGIN: _current_transaction solo avanza después del commit durable.

    Reversión (REV-JRN-STATE): asignar dentro del body cambia el estado aunque
    el commit falle → ROJO.
    """
    journal = OperationJournal(tmp_path / "jrn_state.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None

    async def commit_fallido() -> None:
        raise sqlite3.OperationalError("commit deliberadamente fallido")

    monkeypatch.setattr(db, "commit", commit_fallido)

    with pytest.raises(JournalTransactionError):
        await journal.begin_transaction("falla en commit")

    assert journal._current_transaction is None, "el estado cambió pese al commit fallido"
    assert db.in_transaction is False


async def test_journal_commit_clears_state_after_commit(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JRN-COMMIT: _current_transaction se limpia SOLO después del commit durable."""
    journal = OperationJournal(tmp_path / "jrn_cstate.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None

    tx_id = await journal.begin_transaction("para commit")
    assert journal._current_transaction == tx_id

    async def commit_fallido() -> None:
        raise sqlite3.OperationalError("commit deliberadamente fallido")

    monkeypatch.setattr(db, "commit", commit_fallido)
    with pytest.raises(JournalTransactionError):
        await journal.commit_transaction(tx_id)
    assert journal._current_transaction == tx_id, "el estado se limpió sin commit durable"

    monkeypatch.undo()
    await journal.commit_transaction(tx_id)
    assert journal._current_transaction is None
    tx = await journal.get_transaction(tx_id)
    assert tx is not None and tx.status is TransactionStatus.COMMITTED


async def test_journal_rollback_status_clears_state_after_commit(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JRN-ROLLBACK-STATUS: el UPDATE a rolled_back commitea ANTES de limpiar el
    estado Python; un commit fallido conserva el estado."""
    journal = OperationJournal(tmp_path / "jrn_rstate.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None

    tx_id = await journal.begin_transaction("para rollback")
    assert journal._current_transaction == tx_id

    async def commit_fallido() -> None:
        raise sqlite3.OperationalError("commit deliberadamente fallido")

    monkeypatch.setattr(db, "commit", commit_fallido)
    with pytest.raises(JournalTransactionError):
        await journal.rollback_transaction(tx_id)
    assert journal._current_transaction == tx_id, "el estado se limpió sin commit durable"

    monkeypatch.undo()
    await journal.rollback_transaction(tx_id)
    assert journal._current_transaction is None
    tx = await journal.get_transaction(tx_id)
    assert tx is not None and tx.status is TransactionStatus.ROLLED_BACK


async def test_journal_sweep_uses_single_boundary(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """sweep_stale_pending usa UN solo boundary lifecycle.

    Si además tomara self._lock (el MISMO lock de path), el sweep se
    self-deadlockearía de inmediato y el watchdog DEADLINE lo delata.
    """
    journal = OperationJournal(tmp_path / "jrn_sweep.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None

    tx_id = await journal.begin_transaction("huerfana")
    await db.execute(
        "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
        (tx_id,),
    )
    await db.commit()

    barridas = await asyncio.wait_for(journal.sweep_stale_pending(max_age_hours=24.0), timeout=DEADLINE)
    assert barridas == 1
    tx = await journal.get_transaction(tx_id)
    assert tx is not None and tx.status is TransactionStatus.ROLLED_BACK


async def test_journal_open_no_self_deadlock(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """JRN-OPEN: open() (schema bajo boundary + sweep con SU PROPIO boundary)
    completa sin self-deadlock.

    Reversión (REV-JRN-OPEN): sostener el boundary del schema a través del
    sweep → doble adquisición del lock de path → deadlock → TimeoutError.
    """
    journal = OperationJournal(tmp_path / "jrn_open.db", lifecycle=lifecycle)
    await asyncio.wait_for(journal.open(), timeout=DEADLINE)
    assert journal._db is not None


async def test_journal_late_admission_propagates(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> None:
    """Con el lifecycle cerrado, una lectura nueva propaga
    DatabaseLifecycleShuttingDownError (fail-closed)."""
    journal = OperationJournal(tmp_path / "jrn_late.db", lifecycle=lifecycle)
    await journal.open()
    await lifecycle.shutdown_all()

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await journal.get_transaction(1)


async def test_journal_close_does_not_close_lifecycle_connection(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """close() del journal NO cierra la conexión lifecycle-owned; shutdown_all()
    sí la cierra físicamente."""
    journal = OperationJournal(tmp_path / "jrn_own.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None
    await journal.close()

    async with db.execute("SELECT 1") as cur:
        assert await cur.fetchone() is not None

    await lifecycle.shutdown_all()
    with pytest.raises(ValueError, match="no active connection"):
        async with db.execute("SELECT 1") as cur:
            await cur.fetchone()


# ===========================================================================
# JOURNAL — C2: el cleanup del context manager NO reemplaza la excepción original
# ===========================================================================


async def _c2_escenario_shutdown_real(
    lifecycle: DatabaseLifecycleManager,
    journal: OperationJournal,
    original: BaseException,
) -> BaseException | None:
    """Shutdown REAL drenando una operación admitida + cuerpo que lanza.

    Sin sleeps de timing: barreras asyncio.Event y yields del scheduler.
    La operación retenida vive en OTRO path del mismo lifecycle para mantener
    `_admitted > 0` (shutdown drenando, conexiones aún abiertas) sin bloquear
    el lock del path del journal. Devuelve la excepción propagada por la tarea
    del cuerpo (o None si terminara limpia, lo que no debería pasar).
    """
    hold_path = journal._db_path.parent / "hold_c2.db"
    op_entro, liberar_op = asyncio.Event(), asyncio.Event()

    async def operacion_retenida() -> None:
        async with lifecycle.operation(hold_path):
            op_entro.set()
            await liberar_op.wait()

    cuerpo_entro, senal = asyncio.Event(), asyncio.Event()

    async def cuerpo() -> None:
        async with journal.transaction("c2-shutdown-real"):
            cuerpo_entro.set()
            await senal.wait()
            raise original

    t_hold = asyncio.create_task(operacion_retenida())
    await op_entro.wait()
    t_cuerpo = asyncio.create_task(cuerpo())
    await cuerpo_entro.wait()

    t_sd = asyncio.create_task(lifecycle.shutdown_all())
    for _ in range(10_000):
        if lifecycle._shutting_down:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("shutdown_all nunca arrancó el cierre")
    assert not t_sd.done(), "shutdown no está drenando la operación retenida"

    senal.set()  # el cuerpo lanza → rollback → admisión rechazada
    propagada: BaseException | None = None
    try:
        await asyncio.wait_for(t_cuerpo, timeout=DEADLINE)
    except BaseException as error:
        propagada = error

    liberar_op.set()
    await asyncio.wait_for(t_hold, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    return propagada


async def test_journal_c2_sentinel_preservado_con_shutdown_real(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """C2-A: con el shutdown drenando, el rollback rechazado por admisión NO
    reemplaza la excepción original del cuerpo del context manager.

    Reversión: quitar DatabaseLifecycleShuttingDownError del except del cleanup
    → `DatabaseLifecycleShuttingDownError` reemplaza el sentinel → ROJO.
    """
    journal = OperationJournal(tmp_path / "c2_sentinel.db", lifecycle=lifecycle)
    await journal.open()
    sentinel = ValueError("sentinel-cuerpo-c2")

    propagada = await _c2_escenario_shutdown_real(lifecycle, journal, sentinel)

    assert propagada is sentinel, f"la original fue reemplazada por {type(propagada).__name__}"
    assert lifecycle._admitted == 0, "quedó una admisión colgada"
    assert lifecycle._drained.is_set()
    db = journal._db
    assert db is not None
    with pytest.raises(ValueError, match="no active connection"):
        async with db.execute("SELECT 1") as cur:
            await cur.fetchone()


async def test_journal_c2_cancelled_error_preservado_con_shutdown_real(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """C2-B: una CancelledError del cuerpo NO se traduce a
    DatabaseLifecycleShuttingDownError durante el cleanup con shutdown drenando.

    Reversión: quitar DatabaseLifecycleShuttingDownError del except del cleanup
    → la cancelación se traduce a ShuttingDownError → ROJO.
    """
    journal = OperationJournal(tmp_path / "c2_cancel.db", lifecycle=lifecycle)
    await journal.open()
    cancellation = asyncio.CancelledError("cancelacion-cuerpo-c2")

    propagada = await _c2_escenario_shutdown_real(lifecycle, journal, cancellation)

    assert propagada is cancellation, f"la cancelación fue traducida a {type(propagada).__name__}"
    assert lifecycle._admitted == 0, "quedó una admisión colgada"
    assert lifecycle._drained.is_set()
    db = journal._db
    assert db is not None
    with pytest.raises(ValueError, match="no active connection"):
        async with db.execute("SELECT 1") as cur:
            await cur.fetchone()


# ===========================================================================
# JOURNAL — F-02: la cuarentena del lifecycle es el SEGUNDO motivo de rechazo
#
# #475 estableció que un rollback rechazado por el lifecycle no reemplaza la
# excepción del cuerpo, y enumeró el único rechazo que existía entonces
# (shutdown). #483 agregó un segundo —cuarentena por path— sin tocar esa
# enumeración, así que el cleanup volvía a enmascarar. Los tests de acá abajo
# son el gemelo exacto de los C2 de shutdown, con la cuarentena como causa.
# ===========================================================================


async def _instalar_cuarentena_real(lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
    """Deja *db_path* en cuarentena usando SOLO la API pública del manager.

    ``recover_connection()`` documenta que una ventana que termina sin haber
    reabierto deja el path fail-closed y que esa marca no la levanta ninguna
    transición del manager. Es la vía que separa limpiamente esta causa de la de
    shutdown: acá ``_shutting_down`` sigue en False, así que lo único que cambia
    respecto del escenario sano es la cuarentena.
    """
    conn = await lifecycle.get_connection(db_path)
    with pytest.raises(DatabaseConnectionQuarantinedError):
        async with lifecycle.recover_connection(db_path, conn):
            pass  # ventana que NO reconstruye
    assert lifecycle._resolve_key(db_path) in lifecycle._quarantined_paths
    assert not lifecycle._shutting_down, "esta causa debe ser cuarentena, no shutdown"


def _estado_de_fila(db_path: Path, transaction_id: int) -> str | None:
    """Lee el status de la fila con una conexión INDEPENDIENTE del lifecycle.

    El path está en cuarentena: pedirle la conexión al manager sería justamente
    lo que el fail-closed rechaza.
    """
    con = sqlite3.connect(str(db_path))
    try:
        fila = con.execute("SELECT status FROM transactions WHERE transaction_id = ?", (transaction_id,)).fetchone()
    finally:
        con.close()
    return None if fila is None else str(fila[0])


async def test_journal_c2_sentinel_preservado_con_quarantine_real(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager, caplog: pytest.LogCaptureFixture
) -> None:
    """C2-C: con el path en cuarentena, el rollback rechazado NO reemplaza la
    excepción original del cuerpo del context manager.

    Gemelo de ``test_journal_c2_sentinel_preservado_con_shutdown_real``: misma
    forma, mismo boundary, mismo punto de fallo — sólo cambia el motivo por el
    que el lifecycle rechaza el rollback. Que uno preservara la original y el
    otro no era el hueco de integración.

    Reversión: quitar DatabaseConnectionQuarantinedError de
    ``_FALLOS_DE_CLEANUP_ABSORBIBLES`` → la QuarantinedError reemplaza el
    sentinel → ROJO.
    """
    journal = OperationJournal(tmp_path / "c2_quarantine.db", lifecycle=lifecycle)
    await journal.open()
    sentinel = ValueError("ORIGINAL_SENTINEL")
    tx_id: int | None = None

    propagada: BaseException | None = None
    with caplog.at_level(logging.ERROR, logger="sky_claw.app.db.journal"):
        try:
            async with journal.transaction("c2-quarantine-real") as abierta:
                tx_id = abierta
                # La cuarentena SOBREVIENE con la fila PENDING ya creada.
                # Instalarla antes haría fallar begin_transaction() y el cuerpo
                # no correría: ese camino es fail-closed correcto, no este bug.
                await _instalar_cuarentena_real(lifecycle, journal._db_path)
                raise sentinel
        except BaseException as error:
            propagada = error

    assert propagada is sentinel, f"la original fue reemplazada por {type(propagada).__name__}"

    # El rollback REALMENTE intentó alcanzar el path en cuarentena: quedó
    # logueado y la fila sigue PENDING porque el UPDATE nunca llegó a correr.
    assert any("Rollback of transaction" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records), (
        "el fallo secundario del cleanup no quedó logueado"
    )
    assert tx_id is not None
    assert _estado_de_fila(journal._db_path, tx_id) == TransactionStatus.PENDING.value

    assert lifecycle._resolve_key(journal._db_path) in lifecycle._quarantined_paths
    assert lifecycle._admitted == 0, "quedó una admisión colgada"
    assert lifecycle._drained.is_set()


async def test_journal_salida_limpia_propaga_fallo_de_commit_por_quarantine(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """Guard anti-regresión del fix: sin excepción PRIMARIA que preservar, un
    commit rechazado por cuarentena DEBE seguir propagando.

    El contrato absorbe fallos de cleanup, no fallos de la operación principal.
    Un fix que ensanchara el ``except`` hasta tragarse esto convertiría una
    transacción no confirmada en un éxito silencioso.
    """
    journal = OperationJournal(tmp_path / "commit_limpio.db", lifecycle=lifecycle)
    await journal.open()
    tx_id: int | None = None

    with pytest.raises(DatabaseConnectionQuarantinedError):
        async with journal.transaction("commit-limpio") as abierta:
            tx_id = abierta
            await _instalar_cuarentena_real(lifecycle, journal._db_path)
            # salida LIMPIA del cuerpo → commit_transaction() choca la cuarentena

    assert tx_id is not None
    assert _estado_de_fila(journal._db_path, tx_id) == TransactionStatus.PENDING.value
    assert lifecycle._admitted == 0
    assert lifecycle._drained.is_set()


async def test_journal_log_operation_preserva_error_primario_con_quarantine(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager, caplog: pytest.LogCaptureFixture
) -> None:
    """El hermano de ``transaction()`` en el mismo archivo: ``log_operation()``
    marcaba la transacción como rolled-back en un cleanup SIN guard alguno, así
    que cualquier fallo de ese marcado —no sólo el de cuarentena— se llevaba
    puesta la excepción primaria.

    El fallo SECUNDARIO es integración real: ``mark_transaction_rolled_back()``
    pide su boundary al lifecycle y choca con la cuarentena de verdad. El
    sentinel PRIMARIO se inyecta por seam, igual que los C2 existentes lo
    inyectan en el cuerpo del context manager (en ``log_operation`` no hay
    cuerpo donde lanzarlo).

    Reversión: quitar el try/except del cleanup de ``log_operation`` → la
    QuarantinedError secundaria reemplaza el sentinel → ROJO.
    """
    journal = OperationJournal(tmp_path / "log_op_quarantine.db", lifecycle=lifecycle)
    await journal.open()
    sentinel = ValueError("ORIGINAL_SENTINEL")

    async def _complete_operation_falla(entry_id: int) -> None:
        # La cuarentena sobreviene con la transacción legacy ya abierta y su
        # entrada ya insertada: el cleanup es quien va a chocar con ella.
        await _instalar_cuarentena_real(lifecycle, journal._db_path)
        raise sentinel

    journal.complete_operation = _complete_operation_falla  # type: ignore[method-assign]

    propagada: BaseException | None = None
    with caplog.at_level(logging.ERROR, logger="sky_claw.app.db.journal"):
        try:
            await journal.log_operation(
                agent_id="c2",
                operation_type="file_modify",
                file_path=str(tmp_path / "objetivo.esp"),
            )
        except BaseException as error:
            propagada = error

    assert propagada is sentinel, f"la primaria fue reemplazada por {type(propagada).__name__}"
    assert any("Marking transaction" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records), (
        "el fallo secundario del cleanup no quedó logueado"
    )
    assert lifecycle._resolve_key(journal._db_path) in lifecycle._quarantined_paths
    assert lifecycle._admitted == 0, "quedó una admisión colgada"
    assert lifecycle._drained.is_set()


async def test_journal_cancelacion_nueva_durante_rollback_no_es_absorbida(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """El fix ensancha el ``except`` del cleanup, pero NO hasta BaseException:
    una cancelación NUEVA llegada durante el rollback conserva su semántica y
    sigue propagando.

    Integración real, sin sleeps: un tercero toma el lock del path, el cleanup
    queda esperándolo —ese es el punto de suspensión— y ahí se cancela la tarea
    del cuerpo. Si el fix la absorbiera, la tarea terminaría con el sentinel en
    vez de quedar cancelada.
    """
    journal = OperationJournal(tmp_path / "cancel_nueva.db", lifecycle=lifecycle)
    await journal.open()
    sentinel = ValueError("ORIGINAL_SENTINEL")

    cuerpo_entro, senal = asyncio.Event(), asyncio.Event()

    async def cuerpo() -> None:
        async with journal.transaction("cancel-nueva"):
            cuerpo_entro.set()
            await senal.wait()
            raise sentinel

    t_cuerpo = asyncio.create_task(cuerpo())
    await asyncio.wait_for(cuerpo_entro.wait(), timeout=DEADLINE)

    hold_entro, liberar = asyncio.Event(), asyncio.Event()

    async def retener_lock() -> None:
        async with lifecycle.operation(journal._db_path):
            hold_entro.set()
            await liberar.wait()

    t_hold = asyncio.create_task(retener_lock())
    await asyncio.wait_for(hold_entro.wait(), timeout=DEADLINE)

    senal.set()  # el cuerpo lanza → el cleanup pide el lock del path y bloquea
    await ceder()
    t_cuerpo.cancel()  # cancelación NUEVA, ya dentro del cleanup

    hechas, pendientes = await asyncio.wait({t_cuerpo}, timeout=DEADLINE)
    assert not pendientes, "el cuerpo quedó colgado"
    assert t_cuerpo.cancelled(), "la cancelación nueva del cleanup fue absorbida por el fix"

    liberar.set()
    await asyncio.wait_for(t_hold, timeout=DEADLINE)
    assert lifecycle._admitted == 0, "quedó una admisión colgada"
    assert lifecycle._drained.is_set()


# ===========================================================================
# ANCLA ENUMERANTE — la familia de rechazos de admisión del lifecycle
#
# Los tests conductuales de arriba prueban los DOS rechazos que existen hoy,
# pero un tercero que naciera en db_lifecycle no rompería ninguno: quedaría
# fuera de `_FALLOS_DE_CLEANUP_ABSORBIBLES` y los dos cleanups del journal
# volverían a pisar la excepción primaria — exactamente cómo nació este bug
# (#475 enumeró shutdown, #483 agregó cuarentena sin tocar la enumeración).
#
# El ancla congela la familia COMPLETA de excepciones públicas del lifecycle y
# obliga a clasificar cada una: o puede salir de `transaction().__aenter__` —y
# entonces necesita su receta y queda cubierta por el contrato— o no puede, y
# se dice por qué. Sin AST y sin congelar nombres privados: introspección del
# módulo + igualdad literal, el patrón de RITUAL_TOOL_MAP.
# ===========================================================================


async def _provocar_shutdown(lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
    """Receta de DatabaseLifecycleShuttingDownError: `_admit()` deja de admitir."""
    await lifecycle.shutdown_all()


async def _provocar_cuarentena(lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
    """Receta de DatabaseConnectionQuarantinedError: `_resolve_connection()` fail-closed."""
    await _instalar_cuarentena_real(lifecycle, db_path)


# Rechazos que SÍ pueden salir de `lifecycle.transaction().__aenter__`, cada uno
# con la receta que lo provoca. Un rechazo nuevo entra acá y hereda por
# construcción el test conductual parametrizado de más abajo.
_RECHAZOS_DE_ADMISION: dict[type[BaseException], Callable[[DatabaseLifecycleManager, Path], Awaitable[None]]] = {
    DatabaseLifecycleShuttingDownError: _provocar_shutdown,
    DatabaseConnectionQuarantinedError: _provocar_cuarentena,
}

# Excepciones públicas del lifecycle que NO pueden aparecer al entrar a
# `transaction()`, con el motivo. Estar acá es una EXENCIÓN declarada, no un
# olvido: el ancla de abajo exige que cada excepción esté en exactamente una
# de las dos listas.
_FUERA_DEL_CAMINO_DE_ADMISION: dict[type[BaseException], str] = {
    DatabaseShutdownIncompleteError: "sólo la levanta shutdown_all() al no poder cerrar todo",
    DatabaseConnectionReplacedError: "sólo la levanta recover_connection() por el guard de identidad",
}


def _excepciones_publicas_del_lifecycle() -> set[type[BaseException]]:
    """Las excepciones públicas DEFINIDAS en db_lifecycle, por introspección."""
    return {
        objeto
        for nombre, objeto in vars(db_lifecycle_mod).items()
        if isinstance(objeto, type)
        and issubclass(objeto, BaseException)
        and objeto.__module__ == db_lifecycle_mod.__name__
        and not nombre.startswith("_")
    }


def test_ancla_familia_de_excepciones_del_lifecycle_congelada() -> None:
    """Toda excepción pública del lifecycle está clasificada, y sólo una vez.

    Igualdad literal: agregar una excepción a db_lifecycle rompe este test hasta
    que alguien decida si es un rechazo de admisión —y le escriba su receta, que
    la mete automáticamente en el test conductual— o una exención con motivo.
    """
    clasificadas = set(_RECHAZOS_DE_ADMISION) | set(_FUERA_DEL_CAMINO_DE_ADMISION)

    assert not (set(_RECHAZOS_DE_ADMISION) & set(_FUERA_DEL_CAMINO_DE_ADMISION)), (
        "una excepción no puede ser rechazo de admisión y exención a la vez"
    )
    assert _excepciones_publicas_del_lifecycle() == clasificadas, (
        "hay excepciones del lifecycle sin clasificar: decidí si pueden salir de "
        "transaction().__aenter__ (receta en _RECHAZOS_DE_ADMISION) o no (motivo "
        "en _FUERA_DEL_CAMINO_DE_ADMISION)"
    )


def test_ancla_todo_rechazo_de_admision_es_absorbible_por_el_cleanup() -> None:
    """El contrato del journal cubre la familia ENTERA, no una muestra.

    Reversión: quitar cualquier miembro de `_FALLOS_DE_CLEANUP_ABSORBIBLES` →
    ROJO nombrando exactamente el rechazo que quedó sin cubrir.
    """
    sin_cubrir = set(_RECHAZOS_DE_ADMISION) - set(_FALLOS_DE_CLEANUP_ABSORBIBLES)
    assert not sin_cubrir, (
        "estos rechazos del lifecycle reemplazarían la excepción primaria en los "
        f"cleanups del journal: {sorted(e.__name__ for e in sin_cubrir)}"
    )


@pytest.mark.parametrize(
    "rechazo",
    list(_RECHAZOS_DE_ADMISION),
    ids=[e.__name__ for e in _RECHAZOS_DE_ADMISION],
)
async def test_ancla_cleanup_preserva_primaria_para_todo_rechazo_de_admision(
    rechazo: type[BaseException],
    tmp_path: Path,
    lifecycle: DatabaseLifecycleManager,
) -> None:
    """Conductual y parametrizado sobre la familia: para CADA rechazo, el cuerpo
    falla, el rollback choca ese rechazo, y la primaria sale igual.

    Un rechazo nuevo con su receta entra acá sin escribir un test a mano — que es
    lo que diferencia enumerar de muestrear.
    """
    journal = OperationJournal(tmp_path / f"ancla_{rechazo.__name__}.db", lifecycle=lifecycle)
    await journal.open()
    sentinel = ValueError("ORIGINAL_SENTINEL")
    provocar = _RECHAZOS_DE_ADMISION[rechazo]

    propagada: BaseException | None = None
    try:
        async with journal.transaction(f"ancla-{rechazo.__name__}"):
            await provocar(lifecycle, journal._db_path)
            raise sentinel
    except BaseException as error:
        propagada = error

    assert propagada is sentinel, f"la primaria fue reemplazada por {type(propagada).__name__}"

    # La receta provocó ESE rechazo y no otro: sin esto, una receta que dejara de
    # funcionar volvería el caso verde por la razón equivocada.
    with pytest.raises(rechazo):
        async with lifecycle.transaction(journal._db_path):
            pass  # pragma: no cover — el __aenter__ ya rechaza


# ---------------------------------------------------------------------------
# Cierre del hueco: congelar los SITIOS de raise, no sólo el conjunto de clases
#
# El ancla de arriba detecta una excepción NUEVA en db_lifecycle, pero no que
# una ya clasificada como exención empiece a levantarse desde el camino de
# admisión (p.ej. `_resolve_connection` levantando DatabaseConnectionReplacedError):
# el conjunto de clases no cambia y el contrato del journal queda corto igual.
#
# Es el mismo argumento con el que `test_db_connection_invariant.py` congela el
# CONTEO por módulo y no los nombres: "agregar un segundo `connect()` dentro de
# un módulo ya listado es el punto de extensión más probable, y con un set de
# nombres pasaría sin romper nada". Acá el par (función, excepción) hace ese
# papel. Escribir el párrafo explicando el hueco no lo verifica — AGENTS.md es
# explícito en que eso no cuenta.
# ---------------------------------------------------------------------------

_ARCHIVO_DB_LIFECYCLE = Path(db_lifecycle_mod.__file__)

# Eslabones de db_lifecycle que se atraviesan al entrar a `transaction()`:
# transaction → operation → _admit / get_write_lock / _resolve_connection → _init_single.
# Congelar estos nombres es deliberado y load-bearing: renombrar un eslabón
# rompe el ancla y obliga a re-mirar qué puede levantar ahora ese camino.
_CAMINO_DE_ADMISION = frozenset(
    {"transaction", "operation", "_admit", "get_write_lock", "_resolve_connection", "_init_single"}
)

# Todo `raise <ClaseDeExcepción>` de db_lifecycle, por (función, excepción).
_SITIOS_DE_RAISE_ESPERADOS = {
    ("_admit", "DatabaseLifecycleShuttingDownError"),
    ("_await_db_operation", "_DatabaseOperationCancelled"),
    ("_await_db_operation", "_DatabaseOperationFailedAfterCancellation"),
    ("_resolve_connection", "DatabaseConnectionQuarantinedError"),
    ("_shutdown_all_under_transition", "DatabaseShutdownIncompleteError"),
    ("init_all", "DatabaseLifecycleShuttingDownError"),
    ("recover_connection", "DatabaseConnectionQuarantinedError"),
    ("recover_connection", "DatabaseConnectionReplacedError"),
}


def _sitios_de_raise_del_lifecycle() -> set[tuple[str, str]]:
    """(función, excepción) por cada ``raise <ClaseDeExcepción>`` de db_lifecycle.

    AST para el sitio; introspección para quedarse SOLO con lo que de verdad es
    una clase de excepción del módulo. Sin ese filtro, los ``raise
    variable_local`` —re-raises de diagnóstico como ``raise diagnostic_error``—
    entran al conjunto y lo vuelven ruido que nadie va a mantener.
    """
    arbol = ast.parse(_ARCHIVO_DB_LIFECYCLE.read_text(encoding="utf-8"))
    pila: list[str] = []
    sitios: set[tuple[str, str]] = set()

    class _Visitante(ast.NodeVisitor):
        def _entrar(self, nodo: Any) -> None:
            pila.append(nodo.name)
            self.generic_visit(nodo)
            pila.pop()

        def visit_FunctionDef(self, nodo: ast.FunctionDef) -> None:  # noqa: N802 — API de ast.NodeVisitor
            self._entrar(nodo)

        def visit_AsyncFunctionDef(self, nodo: ast.AsyncFunctionDef) -> None:  # noqa: N802 — API de ast.NodeVisitor
            self._entrar(nodo)

        def visit_Raise(self, nodo: ast.Raise) -> None:  # noqa: N802 — API de ast.NodeVisitor
            if nodo.exc is not None:
                objetivo = nodo.exc.func if isinstance(nodo.exc, ast.Call) else nodo.exc
                if isinstance(objetivo, ast.Name):
                    resuelto = getattr(db_lifecycle_mod, objetivo.id, None)
                    if isinstance(resuelto, type) and issubclass(resuelto, BaseException):
                        sitios.add((pila[-1] if pila else "<módulo>", objetivo.id))
            self.generic_visit(nodo)

    _Visitante().visit(arbol)
    return sitios


def test_ancla_sitios_de_raise_del_lifecycle_congelados() -> None:
    """Igualdad literal de los pares (función, excepción) de db_lifecycle.

    Un `raise` nuevo —clase nueva O una ya existente desde un sitio nuevo— rompe
    esto hasta que alguien decida si ese camino alcanza `transaction().__aenter__`
    y, si lo alcanza, lo agregue a `_RECHAZOS_DE_ADMISION` y a
    `_FALLOS_DE_CLEANUP_ABSORBIBLES`.
    """
    assert _sitios_de_raise_del_lifecycle() == _SITIOS_DE_RAISE_ESPERADOS, (
        "cambiaron los sitios de raise de db_lifecycle: decidí si el camino "
        "afectado puede alcanzar transaction().__aenter__ y, si sí, que su "
        "excepción quede cubierta por el contrato de cleanup del journal"
    )


def test_ancla_el_camino_de_admision_solo_levanta_rechazos_clasificados() -> None:
    """Lo que el camino de admisión levanta == lo que el journal declara absorber.

    Ata el ancla sintáctica a la semántica: cierra el caso que el conjunto de
    clases no ve —una excepción ya clasificada como exención levantándose desde
    el camino de admisión—, porque ahí el par cambia aunque el conjunto no.
    """
    levantados = {
        excepcion for funcion, excepcion in _sitios_de_raise_del_lifecycle() if funcion in _CAMINO_DE_ADMISION
    }
    clasificados = {rechazo.__name__ for rechazo in _RECHAZOS_DE_ADMISION}
    assert levantados == clasificados, (
        "el camino de admisión del lifecycle levanta rechazos que el journal no "
        f"clasifica (o al revés): camino={sorted(levantados)} vs. clasificados={sorted(clasificados)}"
    )


# ===========================================================================
# fail_operation BAJO EL BOUNDARY DEL LIFECYCLE
#
# `fail_operation` pasó a escribir estado + evidencia en UNA sola
# `lifecycle.transaction()`. Eso le da, por construcción y sin código propio:
# admisión (el shutdown la espera), fail-closed ante cuarentena, rollback que
# TERMINA aunque cancelen al llamador, y preservación de la excepción primaria.
#
# Estos tests son la autoridad CONDUCTUAL de esa herencia. El ancla sintáctica
# de más abajo impide que alguien saque el método del boundary sin que nada
# falle — que era exactamente el estado anterior del repo.
# ===========================================================================


async def _operacion_started(journal: OperationJournal) -> int:
    """Una operación STARTED lista para fallar."""
    tx_id = await journal.begin_transaction("fallo bajo boundary")
    return await journal.begin_operation(
        agent_id="agente_boundary",
        operation_type=OperationType.FILE_MODIFY,
        target_path="/mods/objetivo.esp",
        transaction_id=tx_id,
    )


def _evidencia_independiente(db_path: Path, entry_id: int) -> tuple[str | None, str | None]:
    """(status, metadata) con una conexión INDEPENDIENTE del lifecycle."""
    conexion = sqlite3.connect(str(db_path))
    try:
        fila = conexion.execute("SELECT status, metadata FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    finally:
        conexion.close()
    return (None, None) if fila is None else (fila[0], fila[1])


async def test_journal_fail_operation_en_vuelo_bloquea_el_shutdown(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """El registro de un fallo en vuelo cuenta como admitido: el shutdown lo
    espera en vez de cerrar la conexión debajo.

    Reversión: sacar `fail_operation` de `lifecycle.transaction()` (escribir con
    `self._lock` + commit manual) → el shutdown completa con la escritura en
    vuelo → ROJO.
    """
    journal = OperationJournal(tmp_path / "fail_admision.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None
    entry_id = await _operacion_started(journal)

    entro, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(db, "SET status", entro, puerta)

    t_op = asyncio.create_task(journal.fail_operation(entry_id, error="boom", metadata={"k": "v"}))
    await asyncio.wait_for(entro.wait(), timeout=DEADLINE)
    try:
        assert lifecycle._admitted >= 1, "la escritura del fallo no tomó admisión"
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not t_sd.done(), "el shutdown completó con el registro de un fallo en vuelo"
    finally:
        puerta.set()

    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)

    # La evidencia sobrevivió al shutdown: es durable, no quedó en un buffer.
    status, metadata = _evidencia_independiente(journal._db_path, entry_id)
    assert status == OperationStatus.FAILED.value
    assert metadata is not None and "boom" in metadata


async def test_journal_fail_operation_durante_shutdown_es_fail_closed(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """Con el lifecycle cerrado, registrar un fallo propaga en vez de escribir a
    ciegas sobre una conexión que ya nadie coordina."""
    journal = OperationJournal(tmp_path / "fail_shutdown.db", lifecycle=lifecycle)
    await journal.open()
    entry_id = await _operacion_started(journal)
    await lifecycle.shutdown_all()

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await journal.fail_operation(entry_id, error="boom")


async def test_journal_fail_operation_con_path_en_cuarentena_es_fail_closed(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """Gemelo del anterior por la OTRA causa de rechazo del camino de admisión.

    Que uno fuera fail-closed y el otro no es la forma exacta del hueco que
    `_RECHAZOS_DE_ADMISION` existe para cerrar.
    """
    journal = OperationJournal(tmp_path / "fail_quarantine.db", lifecycle=lifecycle)
    await journal.open()
    entry_id = await _operacion_started(journal)
    await _instalar_cuarentena_real(lifecycle, journal._db_path)

    with pytest.raises(DatabaseConnectionQuarantinedError):
        await journal.fail_operation(entry_id, error="boom")


async def test_journal_fail_operation_cancelada_no_deja_estado_parcial(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> None:
    """Cancelación entre el UPDATE del estado y el commit: la fila no queda
    FAILED sin su evidencia, y no sobrevive una transacción colgante en la
    conexión COMPARTIDA.

    Es la ventana que los dos boundaries dejaban abierta. No se afirma un
    desenlace único —según dónde caiga la cancelación el commit puede haber
    completado o no, y las dos cosas son correctas— sino que el par
    (status, evidencia) es consistente.
    """
    journal = OperationJournal(tmp_path / "fail_cancel.db", lifecycle=lifecycle)
    await journal.open()
    db = journal._db
    assert db is not None
    entry_id = await _operacion_started(journal)

    entro, puerta = asyncio.Event(), asyncio.Event()
    _ejecucion_con_barrera(db, "SET status", entro, puerta, tras_ejecutar=True)

    tarea = asyncio.create_task(journal.fail_operation(entry_id, error="boom", metadata={"k": "v"}))
    await asyncio.wait_for(entro.wait(), timeout=DEADLINE)
    assert db.in_transaction, "premisa: el UPDATE del estado abrió la transacción implícita"
    tarea.cancel()
    puerta.set()

    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert db.in_transaction is False, "quedó una transacción colgante en la conexión compartida"
    status, metadata = _evidencia_independiente(journal._db_path, entry_id)
    aplicado = status == OperationStatus.FAILED.value and metadata is not None and "error" in metadata
    intacto = status == OperationStatus.STARTED.value and metadata is None
    assert aplicado or intacto, f"estado PARCIAL: status={status!r} metadata={metadata!r}"


# ---------------------------------------------------------------------------
# ANCLA ENUMERANTE — ningún mutador del journal sale del boundary del lifecycle
#
# El hueco que esto cierra, medido: hasta este PR NINGÚN test detectaba que un
# método del journal dejara de pasar por `lifecycle.transaction()`. Sacar
# `fail_operation` del boundary —que es exactamente lo que hacía la solución
# histórica de #466, escrita antes de que el boundary existiera— pasaba en
# verde, perdiendo admisión, fail-closed de cuarentena y gating de shutdown sin
# una sola línea roja.
#
# Se congela por IGUALDAD LITERAL, no por muestreo, y en dos partes que se
# necesitan mutuamente:
#
#   1. El inventario `{método: usa_el_boundary}` de todo el que emite SQL. Un
#      método nuevo, o uno existente que deje de tomar el boundary, rompe acá.
#   2. Los helpers que ASUMEN el boundary del llamador (hoy `_escribir_fallo`,
#      que existe para que las dos ramas de `fail_operation` compartan un solo
#      cuerpo de SQL). Sin esta parte, mover el SQL a un helper y quitarle el
#      boundary al llamador pasaría el inventario: el helper no toma boundary
#      "legítimamente" y el llamador ya no emite SQL directo.
#
# La autoridad sobre correctness siguen siendo los tests conductuales de arriba;
# AST es inventario.
# ---------------------------------------------------------------------------

_ARCHIVO_JOURNAL = Path(journal_mod.__file__)

# Métodos de `OperationJournal` que emiten SQL, y si toman el boundary del
# lifecycle por su cuenta.
_EMISORES_DE_SQL_ESPERADOS = {
    # Helper compartido por las dos ramas de fail_operation: NO toma boundary a
    # propósito —lo sostiene el llamador—, y por eso está en la lista de
    # exenciones de abajo, que verifica a sus callers.
    "_escribir_fallo": False,
    "_update_status": True,
    "begin_operation": True,
    "begin_transaction": True,
    "commit_transaction": True,
    "get_last_operation": True,
    "get_operation_by_id": True,
    "get_operations_by_agent": True,
    "get_operations_by_path": True,
    "get_operations_by_transaction": True,
    "get_transaction": True,
    "list_recent_transactions": True,
    "mark_rolled_back": True,
    "mark_transaction_rolled_back": True,
    "open": True,
    "rollback_transaction": True,
    "sweep_stale_pending": True,
    # D2 (PR #493): lecturas del handoff durable — toman operation() con
    # lifecycle y self._lock en standalone, como sus pares de get_*.
    "consultar_handoff_activo": True,
    "cerrar_receipts_como_no_artifact": True,
    # D2: los helpers de pasos SQL ASUMEN el boundary del escritor
    # público (ver _HELPERS_QUE_ASUMEN_BOUNDARY): por eso False acá.
    "_escribir_handoff_de_deployment": False,
    "_escribir_resume_completado": False,
    "_escribir_transicion_de_handoff": False,
    "_escribir_handoff_indeterminado": False,
    "_escribir_receipts_de_sweep": False,
}

# Segunda dimensión, y hace falta: `_EMISORES_DE_SQL_ESPERADOS` solo mira el
# boundary del lifecycle, así que borrar `async with self._lock` de la rama
# STANDALONE de cualquier mutador lo dejaba pasar en verde — y con eso se va la
# serialización de la conexión propia, que es el único mecanismo que hay cuando
# no hay lifecycle. Mismo criterio que el otro inventario: igualdad literal.
_EMISORES_CON_LOCK_LOCAL_ESPERADOS = {
    # `open()` no lo toma: el schema corre bajo `operation()` con lifecycle y sin
    # boundary en standalone (conexión recién creada, nadie más la ve todavía), y
    # sostenerlo hasta el final del método deadlockearía con el sweep de arranque.
    "open": False,
    # Helper compartido: el lock lo sostiene el llamador (ver más abajo).
    "_escribir_fallo": False,
    "_update_status": True,
    "begin_operation": True,
    "begin_transaction": True,
    "commit_transaction": True,
    "get_last_operation": True,
    "get_operation_by_id": True,
    "get_operations_by_agent": True,
    "get_operations_by_path": True,
    "get_operations_by_transaction": True,
    "get_transaction": True,
    "list_recent_transactions": True,
    "mark_rolled_back": True,
    "mark_transaction_rolled_back": True,
    "rollback_transaction": True,
    "sweep_stale_pending": True,
    # D2 (PR #493): lecturas del handoff — self._lock en standalone.
    "consultar_handoff_activo": True,
    "cerrar_receipts_como_no_artifact": True,
    # D2: helpers que asumen la serialización del caller (False acá; el par
    # exacto de cada caller se congela en _SERIALIZACION_DE_CALLERS_ESPERADA).
    "_escribir_handoff_de_deployment": False,
    "_escribir_resume_completado": False,
    "_escribir_transicion_de_handoff": False,
    "_escribir_handoff_indeterminado": False,
    "_escribir_receipts_de_sweep": False,
}

# Helpers que emiten SQL ASUMIENDO el boundary del llamador. Cada uno obliga a
# que TODOS sus callers dentro de la clase lo tomen.
_HELPERS_QUE_ASUMEN_BOUNDARY = frozenset(
    {
        "_escribir_fallo",
        # D2 (PR #493): los pasos SQL del handoff durable. Los escritores
        # públicos sostienen las DOS serializaciones en su cuerpo — boundary
        # del lifecycle para la rama compartida, self._lock para la propia — y
        # el par exacto por caller queda congelado abajo.
        "_escribir_handoff_de_deployment",
        "_escribir_resume_completado",
        "_escribir_transicion_de_handoff",
        "_escribir_handoff_indeterminado",
        # D2 (PR #493 patch 0006): el INSERT de receipts del sweep asume el
        # boundary del sweep (Principio 2: receipt y ROLLED_BACK caen juntos).
        "_escribir_receipts_de_sweep",
    }
)

_METODOS_DE_EJECUCION = frozenset({"execute", "executescript", "executemany"})

# POST493_ACTIVE_INDETERMINATE_EVIDENCE: helpers de MÓDULO (no métodos de la
# clase) que emiten SQL sobre el ``conn`` del caller. Existen porque el oracle
# público y los boundaries de sello comparten una sola definición de candidata.
#
# Cierran el modo de falla que el comentario de arriba (parte 2) ya predecía y
# que este PR ejerció: al mover el SQL de ``transacciones_que_nombran`` a un
# helper, el método dejó de emitir SQL directo y salió de AMBOS inventarios —
# y con él la única verificación de que la lectura del oracle está serializada.
# Los inventarios no alcanzan acá: ``_cuerpo_de_operation_journal()`` solo mira
# métodos de ``OperationJournal``, así que un helper de módulo es invisible
# para ellos por construcción.
_HELPERS_DE_MODULO_QUE_EMITEN_SQL = frozenset(
    {
        "_candidatas_orphan_en_conn",
        "_filtrar_resolubles_historicas_en_conn",
        "_obsoletar_receipts_de_artifact",
        "_registrar_resoluciones_de_artifact_en_conn",
        "_migrar_resoluciones_legacy_en_conn",
    }
)
_BOUNDARIES_DEL_LIFECYCLE = frozenset({"transaction", "operation"})


def _cuerpo_de_operation_journal() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Los métodos definidos en `OperationJournal` (no en sus subclases)."""
    arbol = ast.parse(_ARCHIVO_JOURNAL.read_text(encoding="utf-8"))
    clase = next(nodo for nodo in arbol.body if isinstance(nodo, ast.ClassDef) and nodo.name == "OperationJournal")
    return [nodo for nodo in clase.body if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _emite_sql(metodo: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr in _METODOS_DE_EJECUCION
        for nodo in ast.walk(metodo)
    )


def _toma_boundary_del_lifecycle(metodo: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """``self._lifecycle.transaction(...)`` / ``.operation(...)`` en el cuerpo."""
    for nodo in ast.walk(metodo):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)):
            continue
        if nodo.func.attr not in _BOUNDARIES_DEL_LIFECYCLE:
            continue
        receptor = nodo.func.value
        if isinstance(receptor, ast.Attribute) and receptor.attr == "_lifecycle":
            return True
    return False


def _es_el_lock_de_self(nodo: ast.expr) -> bool:
    """``self._lock`` exactamente — no cualquier atributo llamado ``_lock``.

    El dueño importa: ``async with otro._lock`` serializa el lock de otro objeto y
    deja la conexión de ESTE journal sin proteger. Un predicado único para los dos
    sitios que lo consultan (item suelto y dentro de un ``Tuple``): duplicar la
    condición es exactamente el hermano desalineado que este repo colecciona, y
    acá el gemelo flojo dejaría pasar `otro._lock` agrupado.
    """
    return (
        isinstance(nodo, ast.Attribute)
        and nodo.attr == "_lock"
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "self"
    )


def _toma_el_lock_local(metodo: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """``async with self._lock`` en el cuerpo — la serialización del standalone.

    Con lifecycle, ``open()`` reemplaza ``self._lock`` por el lock compartido por
    path del manager, así que este mismo ``async with`` sirve a las dos ramas; lo
    que el ancla defiende es que el mutador tome el lock DEL JOURNAL, sea cual sea
    el objeto que ese atributo tenga en cada rama.
    """
    for nodo in ast.walk(metodo):
        if not isinstance(nodo, ast.AsyncWith):
            continue
        for item in nodo.items:
            objetivo = item.context_expr
            if _es_el_lock_de_self(objetivo):
                return True
            # `async with self._lock, db.execute(...) as cursor:` — el lock puede
            # venir como uno de varios items, y también dentro de un Tuple.
            if isinstance(objetivo, ast.Tuple) and any(_es_el_lock_de_self(elemento) for elemento in objetivo.elts):
                return True
    return False


def test_ancla_emisores_de_sql_serializan_su_rama_standalone() -> None:
    """Igualdad literal de `{método: toma el lock}` para todo emisor de SQL.

    Cierra el hueco que el inventario de boundaries no ve: sacar
    `async with self._lock` de la rama sin lifecycle deja la conexión propia sin
    serialización y los dos anclas anteriores siguen verdes.
    """
    observado = {
        metodo.name: _toma_el_lock_local(metodo) for metodo in _cuerpo_de_operation_journal() if _emite_sql(metodo)
    }
    assert observado == _EMISORES_CON_LOCK_LOCAL_ESPERADOS, (
        "cambió qué métodos de OperationJournal serializan su SQL con self._lock: "
        "decidí si el método afectado participa de la serialización o es una "
        "exención (y por qué la conexión propia no necesita protección ahí)"
    )


def _llama_a(metodo: ast.FunctionDef | ast.AsyncFunctionDef, nombre: str) -> bool:
    return any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre
        for nodo in ast.walk(metodo)
    )


def test_ancla_emisores_de_sql_del_journal_congelados() -> None:
    """Igualdad literal de `{método: toma el boundary}` para todo emisor de SQL.

    Reversión: quitarle `async with self._lifecycle.transaction(...)` a
    cualquiera de los que hoy dicen True → ROJO. Agregar un método que emita SQL
    sin declararse acá → ROJO, y quien lo agregue tiene que decidir a propósito
    si participa del boundary o es una exención con callers verificados.
    """
    observado = {
        metodo.name: _toma_boundary_del_lifecycle(metodo)
        for metodo in _cuerpo_de_operation_journal()
        if _emite_sql(metodo)
    }
    assert observado == _EMISORES_DE_SQL_ESPERADOS, (
        "cambió qué métodos de OperationJournal emiten SQL bajo el boundary del "
        "lifecycle: decidí si el método afectado participa del boundary o es una "
        "exención que asume el del llamador (y entonces va a "
        "_HELPERS_QUE_ASUMEN_BOUNDARY, con sus callers verificados)"
    )


# Callers de los helpers de `_HELPERS_QUE_ASUMEN_BOUNDARY`, con la serialización
# EXACTA que sostiene cada uno: `(boundary del lifecycle, lock local)`.
#
# Congelar el PAR y no sólo "toma alguno" es lo que cierra el hueco: la rama con
# lifecycle se serializa con `transaction()` y la standalone con `self._lock`, así
# que un caller que perdiera su mecanismo seguiría teniendo el otro en `True` y un
# "toma alguno" pasaría en verde con la mitad de la protección borrada.
_SERIALIZACION_DE_CALLERS_ESPERADA = {
    # Rama con lifecycle: entra por `transaction()`. No toma el lock local porque
    # `open()` ya lo reemplazó por el lock compartido del manager, y ese lo
    # sostiene `operation()` por dentro.
    "fail_operation": (True, False),
    # Rama standalone: conexión propia, sin lifecycle que coordine. El lock local
    # es el ÚNICO mecanismo que serializa acá.
    "_escribir_fallo_standalone": (False, True),
    # D2 (PR #493): los escritores del handoff sostienen las DOS ramas en su
    # propio cuerpo — `transaction()` con lifecycle y `self._lock` con conexión
    # propia — porque el mismo método debe cerrar la TX y el handoff en un
    # único boundary en ambos modos de operación.
    "crear_handoff_de_deployment": (True, True),
    "completar_handoff_de_resume": (True, True),
    "transicionar_handoff": (True, True),
    "registrar_handoff_indeterminado": (True, True),
    # D2 (PR #493 patch 0006): el sweep sostiene las DOS ramas — boundary del
    # lifecycle con conexión compartida, self._lock con conexión propia — y
    # delega el INSERT de receipts en el helper que asume esa serialización.
    "sweep_stale_pending": (True, True),
}


def test_ancla_los_helpers_sin_boundary_solo_tienen_callers_que_lo_toman() -> None:
    """Todo caller de un helper que ASUME serialización debe sostenerla.

    Es la mitad que ata el inventario a la semántica. Sin esto, sacar
    `fail_operation` del boundary dejando el SQL en `_escribir_fallo` pasaría el
    test de arriba sin cambiar una sola entrada del dict: `_escribir_fallo` ya
    figura como exención legítima y `fail_operation` no emite SQL directo.

    Y se congela el PAR `(boundary, lock)` por igualdad, no un "toma alguno":
    `fail_operation` delega la rama standalone en `_escribir_fallo_standalone`, y
    cada uno sostiene un mecanismo distinto. Verificado por mutación: quitarle el
    `async with self._lock` al helper standalone —el único que serializa la
    conexión propia— rompe acá, y antes de este par no rompía nada.
    """
    metodos = _cuerpo_de_operation_journal()
    assert set(_HELPERS_QUE_ASUMEN_BOUNDARY) <= {m.name for m in metodos}, "helper inexistente en el ancla"

    observado: dict[str, tuple[bool, bool]] = {}
    for helper in sorted(_HELPERS_QUE_ASUMEN_BOUNDARY):
        callers = [m for m in metodos if m.name != helper and _llama_a(m, helper)]
        assert callers, f"{helper} quedó sin callers: revisá si sigue haciendo falta"
        for caller in callers:
            observado[caller.name] = (
                _toma_boundary_del_lifecycle(caller),
                _toma_el_lock_local(caller),
            )

    assert observado == _SERIALIZACION_DE_CALLERS_ESPERADA, (
        "cambió la serialización de los callers que emiten SQL a través de un "
        f"helper: {observado}. Cada uno tiene que sostener el mecanismo que le "
        "corresponde —boundary del lifecycle o lock local—; el helper asume una "
        "protección que alguien debe estar sosteniendo."
    )

    sin_proteccion = [nombre for nombre, (bnd, lock) in observado.items() if not (bnd or lock)]
    assert sin_proteccion == [], (
        f"estos callers emiten SQL a través de un helper sin ninguna serialización: {sin_proteccion}"
    )


def _helpers_de_modulo_de_journal() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Funciones definidas a nivel de MÓDULO en journal.py (no métodos)."""
    arbol = ast.parse(_ARCHIVO_JOURNAL.read_text(encoding="utf-8"))
    return {n.name: n for n in arbol.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_ancla_helpers_de_modulo_que_emiten_sql_esta_congelada() -> None:
    """Igualdad literal del conjunto de helpers de módulo que emiten SQL.

    Un helper nuevo entra solo al conjunto detectado y rompe el ancla hasta que
    se le escriba su verificación de callers (el test de abajo). Es la pieza que
    faltaba: los dos inventarios de la clase no pueden verlos.
    """
    detectados = {nombre for nombre, nodo in _helpers_de_modulo_de_journal().items() if _emite_sql(nodo)}
    assert detectados == _HELPERS_DE_MODULO_QUE_EMITEN_SQL, (
        f"cambió el conjunto de helpers de módulo que emiten SQL: {sorted(detectados)}"
    )


def test_ancla_callers_de_helpers_de_modulo_serializan() -> None:
    """Todo caller de un helper de módulo que emite SQL lo invoca bajo una
    serialización: o toma el boundary del lifecycle / ``self._lock`` por su
    cuenta, o es un helper que ASUME el boundary del escritor público.

    Reversión que esto ataja: mover una lectura del oracle fuera del lock —o
    invocar el helper desde un camino nuevo sin serializar— intercalaría esa
    lectura con un boundary de reemplazo/resume y devolvería candidatas
    inconsistentes (falso INDETERMINATE, o evidencia sin absorber). Antes de
    este ancla, ese cambio pasaba CI en verde.
    """
    helpers_de_modulo = _helpers_de_modulo_de_journal()
    sin_serializar: list[str] = []

    def _llama_a_un_helper(nodo: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _HELPERS_DE_MODULO_QUE_EMITEN_SQL
            for n in ast.walk(nodo)
        )

    for metodo in _cuerpo_de_operation_journal():
        if not _llama_a_un_helper(metodo):
            continue
        if metodo.name in _HELPERS_QUE_ASUMEN_BOUNDARY:
            continue  # el escritor público sostiene la serialización
        if metodo.name == "open":
            continue  # open() ejecuta schema y migración bajo operation() con lifecycle / nullcontext standalone
        if _toma_boundary_del_lifecycle(metodo) and _toma_el_lock_local(metodo):
            continue
        sin_serializar.append(metodo.name)

    # Helper de módulo que llama a otro: ambos asumen el boundary del caller, y
    # ese caller ya quedó verificado arriba.
    helpers_de_modulo_conocidos = _HELPERS_DE_MODULO_QUE_EMITEN_SQL | {"_sellar_evidencia_de_artifact"}
    for nombre, nodo in helpers_de_modulo.items():
        if nombre in helpers_de_modulo_conocidos:
            continue
        if _llama_a_un_helper(nodo):
            sin_serializar.append(f"{nombre} (helper de módulo fuera del conjunto congelado)")

    assert not sin_serializar, f"callers del oracle/sello sin serialización propia: {sorted(sin_serializar)}"
