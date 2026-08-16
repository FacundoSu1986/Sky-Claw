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

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from sky_claw.app.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
    DatabaseLifecycleShuttingDownError,
)
from sky_claw.app.db.async_registry import AsyncModRegistry, DatabaseError
from sky_claw.app.db.journal import (
    JournalTransactionError,
    OperationJournal,
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
