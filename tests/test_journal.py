# tests/test_journal.py

import ast
import asyncio
import contextlib
import inspect
import json
import logging
import pathlib
import sqlite3
import textwrap
from unittest.mock import AsyncMock

import pytest

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.app.db.journal import (
    JournalConnectionError,
    NoOpJournal,
    OperationJournal,
    OperationStatus,
    OperationType,
    StagingJournal,
    TransactionStatus,
)
from sky_claw.app.db.rollback_manager import RollbackManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager


@pytest.fixture
async def journal(tmp_path):
    """Fixture que provides a fresh journal instance (M-01: lifecycle injected)."""
    db_path = tmp_path / "test_journal.db"
    lifecycle = DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    journal = OperationJournal(db_path, lifecycle=lifecycle)
    await journal.open()
    yield journal
    await journal.close()
    await lifecycle.shutdown_all()


@pytest.fixture
async def snapshot_manager(tmp_path):
    """Fixture that provides a snapshot manager instance."""
    snapshot_dir = tmp_path / "snapshots"
    manager = FileSnapshotManager(snapshot_dir)
    yield manager


class TestOperationJournal:
    """Tests for OperationJournal."""

    @pytest.mark.asyncio
    async def test_open_close(self, journal):
        """Test opening and closing journal."""
        assert journal._db is not None

    @pytest.mark.asyncio
    async def test_begin_operation(self, journal):
        """Test beginning an operation."""
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.MOD_INSTALL,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
        )
        assert entry_id > 0

    @pytest.mark.asyncio
    async def test_complete_operation(self, journal):
        """Test completing an operation."""
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.MOD_INSTALL,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
        )
        await journal.complete_operation(entry_id)

        entry = await journal.get_last_operation("test_agent")
        assert entry is not None
        assert entry.status == OperationStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fail_operation(self, journal):
        """Test failing an operation."""
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.MOD_INSTALL,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
        )
        await journal.fail_operation(entry_id, "Test error")

        entry = await journal.get_last_operation("test_agent")
        assert entry is not None
        assert entry.status == OperationStatus.FAILED

    @pytest.mark.asyncio
    async def test_open_con_schema_fallido_conserva_conexion_hasta_shutdown(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "journal_schema_fallido.db"
        lifecycle = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn = await lifecycle.get_connection(db_path)
        path_key = str(db_path.resolve())
        journal = OperationJournal(db_path, lifecycle=lifecycle)
        error_schema = sqlite3.OperationalError("schema deliberadamente fallido")

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    conn,
                    "executescript",
                    AsyncMock(side_effect=error_schema),
                )

                with pytest.raises(JournalConnectionError) as exc_info:
                    await journal.open()

            assert exc_info.value.__cause__ is error_schema
            assert lifecycle._connections.get(path_key) is conn
            assert journal._db is None

            await lifecycle.shutdown_all()
            assert lifecycle.managed_paths == []
        finally:
            with contextlib.suppress(Exception):
                await conn.close()


class TestFileSnapshotManager:
    """Tests for FileSnapshotManager."""

    @pytest.mark.asyncio
    async def test_create_snapshot(self, snapshot_manager, tmp_path):
        """Test creating a snapshot."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        snapshot_info = await snapshot_manager.create_snapshot(test_file)
        assert snapshot_info is not None
        assert snapshot_info.original_path == str(test_file)
        assert pathlib.Path(snapshot_info.snapshot_path).exists()
        assert pathlib.Path(snapshot_info.snapshot_path).read_text() == "test content"

    @pytest.mark.asyncio
    async def test_restore_snapshot(self, snapshot_manager, tmp_path):
        """Test restoring a snapshot."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        snapshot_info = await snapshot_manager.create_snapshot(test_file)

        # Modify original file
        test_file.write_text("modified content")

        # Restore
        result = await snapshot_manager.restore_snapshot(snapshot_info.snapshot_path, test_file)
        assert result
        assert test_file.read_text() == "original content"


class TestRollbackManager:
    """Tests for RollbackManager."""

    @pytest.fixture
    async def rollback_manager(self, journal, snapshot_manager):
        """Fixture that provides a rollback manager instance."""
        manager = RollbackManager(journal, snapshot_manager)
        yield manager

    @pytest.mark.asyncio
    async def test_undo_last_operation(self, rollback_manager, journal, tmp_path):
        """Test undoing last operation."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        # Create snapshot
        snapshot_info = await rollback_manager._snapshots.create_snapshot(test_file)

        # Begin and complete operation
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.FILE_MODIFY,
            target_path=str(test_file),
            transaction_id=tx_id,
            snapshot_path=snapshot_info.snapshot_path,
        )
        await journal.complete_operation(entry_id)

        # Modify file
        test_file.write_text("modified content")

        # Rollback
        result = await rollback_manager.undo_last_operation("test_agent")
        assert result.success
        assert test_file.read_text() == "original content"


class TestTransactionContextManager:
    """transaction() — commit/rollback garantizado (hardening jun-2026).

    Sin context manager, una excepción entre begin_transaction() y
    commit_transaction() deja la fila PENDING para siempre.
    """

    @pytest.mark.asyncio
    async def test_transaction_commits_on_clean_exit(self, journal):
        async with journal.transaction("ctx tx ok") as tx_id:
            assert tx_id > 0

        tx = await journal.get_transaction(tx_id)
        assert tx is not None
        assert tx.status is TransactionStatus.COMMITTED

    @pytest.mark.asyncio
    async def test_transaction_rolls_back_on_exception(self, journal):
        with pytest.raises(RuntimeError, match="boom"):
            async with journal.transaction("ctx tx fail") as tx_id:
                raise RuntimeError("boom")

        tx = await journal.get_transaction(tx_id)
        assert tx is not None
        assert tx.status is TransactionStatus.ROLLED_BACK
        assert tx.rolled_back_at is not None

    @pytest.mark.asyncio
    async def test_rollback_transaction_marks_pending_tx(self, journal):
        tx_id = await journal.begin_transaction("manual rollback")

        await journal.rollback_transaction(tx_id)

        tx = await journal.get_transaction(tx_id)
        assert tx is not None
        assert tx.status is TransactionStatus.ROLLED_BACK
        assert tx.rolled_back_at is not None

    @pytest.mark.asyncio
    async def test_sweep_stale_pending_rolls_back_old_transactions(self, journal):
        """El sweeper marca ROLLED_BACK solo las PENDING más viejas que el umbral."""
        tx_old = await journal.begin_transaction("stale orphan")
        await journal._db.execute(
            "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
            (tx_old,),
        )
        await journal._db.commit()
        tx_new = await journal.begin_transaction("fresh")

        swept = await journal.sweep_stale_pending(max_age_hours=24.0)

        assert swept == 1
        assert (await journal.get_transaction(tx_old)).status is TransactionStatus.ROLLED_BACK
        assert (await journal.get_transaction(tx_new)).status is TransactionStatus.PENDING

    @pytest.mark.asyncio
    async def test_open_sweeps_stale_pending(self, tmp_path):
        """Las PENDING huérfanas de una sesión anterior se barren al abrir."""
        db_path = tmp_path / "sweep_on_open.db"
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        try:
            j1 = OperationJournal(db_path, lifecycle=lifecycle)
            await j1.open()
            tx_id = await j1.begin_transaction("orphan from previous session")
            await j1._db.execute(
                "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
                (tx_id,),
            )
            await j1._db.commit()
            await j1.close()

            j2 = OperationJournal(db_path, lifecycle=lifecycle)
            await j2.open()
            tx = await j2.get_transaction(tx_id)
            assert tx is not None
            assert tx.status is TransactionStatus.ROLLED_BACK
            await j2.close()
        finally:
            await lifecycle.shutdown_all()


class TestJournalWriteLockCompartido:
    """El lock de escritura acompaña a la CONEXIÓN, no a la instancia.

    ``DatabaseLifecycleManager.get_connection`` es singleton por path resuelto:
    dos ``OperationJournal`` sobre la misma DB comparten conexión. Con un
    ``asyncio.Lock`` propio cada uno, sus transacciones se intercalan sobre esa
    conexión compartida —uno abre, el otro commitea lo que el primero llevaba a
    medias— y ninguno de los dos locks lo impide, porque cada uno protege a su
    dueño de sí mismo y de nadie más.

    Es la misma propiedad que ``get_write_lock`` ya sostiene para
    ``AsyncModRegistry``, y su docstring la enuncia: *"a per-wrapper lock would
    leave the transaction-interleaving race open"*. Estos tests la congelan del
    lado del journal, por comportamiento observable y no por prohibición
    sintáctica: lo que importa no es qué tipo de lock se construye, sino que dos
    journals sobre la misma conexión no puedan intercalar transacciones.
    """

    @staticmethod
    def _lifecycle() -> DatabaseLifecycleManager:
        return DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )

    # -- identidad y separación del lock --------------------------------------

    @pytest.mark.asyncio
    async def test_mismo_lifecycle_y_path_comparten_el_mismo_objeto_lock(self, tmp_path):
        lifecycle = self._lifecycle()
        db_path = tmp_path / "compartida.db"
        try:
            j1 = OperationJournal(db_path, lifecycle=lifecycle)
            j2 = OperationJournal(db_path, lifecycle=lifecycle)
            await j1.open()
            await j2.open()

            assert j1._db is j2._db, "premisa: la conexión del lifecycle es singleton por path"
            assert j1._lock is j2._lock, "dos journals sobre la misma conexión deben compartir el lock"
            assert j1._lock is lifecycle.get_write_lock(db_path), "debe ser el lock del lifecycle, no uno propio"

            await j1.close()
            await j2.close()
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_paths_distintos_no_comparten_lock(self, tmp_path):
        """Compartir de más serializaría DBs sin relación."""
        lifecycle = self._lifecycle()
        try:
            j1 = OperationJournal(tmp_path / "una.db", lifecycle=lifecycle)
            j2 = OperationJournal(tmp_path / "otra.db", lifecycle=lifecycle)
            await j1.open()
            await j2.open()

            assert j1._db is not j2._db
            assert j1._lock is not j2._lock

            await j1.close()
            await j2.close()
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_standalone_conserva_locks_locales_independientes(self, tmp_path):
        """Sin lifecycle cada journal abre su PROPIA conexión: no hay nada que compartir."""
        db_path = tmp_path / "standalone.db"
        j1 = OperationJournal(db_path)
        j2 = OperationJournal(db_path)
        try:
            await j1.open()
            await j2.open()

            assert j1._owns_conn and j2._owns_conn
            assert j1._db is not j2._db
            assert j1._lock is not j2._lock, "el camino standalone no debe adoptar un lock compartido"
        finally:
            await j1.close()
            await j2.close()

    @pytest.mark.asyncio
    async def test_el_lock_ya_esta_puesto_antes_del_schema_y_del_sweep(self, tmp_path):
        """El rebind va antes de cualquier SQL de ``open()``, no después.

        Si ocurriera al final, el ``executescript`` del schema y el sweep de
        arranque —que toma el lock— correrían bajo el lock equivocado.
        """
        lifecycle = self._lifecycle()
        db_path = tmp_path / "orden.db"
        observado: list[bool] = []
        journal = OperationJournal(db_path, lifecycle=lifecycle)
        compartido = lifecycle.get_write_lock(db_path)

        original_sweep = journal.sweep_stale_pending

        async def sweep_espia():
            observado.append(journal._lock is compartido)
            await original_sweep()

        journal.sweep_stale_pending = sweep_espia  # type: ignore[method-assign]
        try:
            await journal.open()
            assert observado == [True], "el sweep de arranque corrió con el lock viejo"
            await journal.close()
        finally:
            await lifecycle.shutdown_all()

    # -- la propiedad que realmente importa: no hay interleaving ---------------

    @pytest.mark.asyncio
    async def test_dos_journals_no_intercalan_transacciones_sobre_la_misma_conexion(self, tmp_path):
        """Carrera real: dos instancias, misma DB, transacciones concurrentes.

        Cada ``begin_transaction`` hace ``BEGIN`` + ``INSERT`` + ``COMMIT`` sobre
        la conexión compartida. Sin el lock compartido, el ``COMMIT`` de uno
        cierra la transacción abierta del otro y las secciones críticas se
        solapan. Se mide el solapamiento contando entradas/salidas concurrentes
        de la sección crítica, no por timing.
        """
        lifecycle = self._lifecycle()
        db_path = tmp_path / "carrera.db"
        try:
            j1 = OperationJournal(db_path, lifecycle=lifecycle)
            j2 = OperationJournal(db_path, lifecycle=lifecycle)
            await j1.open()
            await j2.open()

            en_vuelo = 0
            solapamiento_maximo = 0
            original_execute = j1._db.execute

            def execute_instrumentado(sql, *args, **kwargs):
                nonlocal en_vuelo, solapamiento_maximo
                if sql.strip().upper().startswith("INSERT INTO TRANSACTIONS"):

                    async def con_ventana():
                        nonlocal en_vuelo, solapamiento_maximo
                        en_vuelo += 1
                        solapamiento_maximo = max(solapamiento_maximo, en_vuelo)
                        try:
                            # Cede el loop DENTRO de la sección crítica: si el
                            # lock no está compartido, el otro journal entra acá.
                            await asyncio.sleep(0)
                            return await original_execute(sql, *args, **kwargs)
                        finally:
                            en_vuelo -= 1

                    return con_ventana()
                return original_execute(sql, *args, **kwargs)

            j1._db.execute = execute_instrumentado  # type: ignore[method-assign]

            await asyncio.gather(
                *(j.begin_transaction(description=f"tx-{i}", mod_id=None) for i, j in enumerate([j1, j2] * 6)),
            )

            j1._db.execute = original_execute  # type: ignore[method-assign]
            assert solapamiento_maximo == 1, (
                f"dos journals entraron a la sección crítica a la vez "
                f"(solapamiento={solapamiento_maximo}): el lock no está compartido"
            )

            # Y las 12 transacciones quedaron persistidas, ninguna perdida.
            transacciones = await j1.list_recent_transactions(limit=50)
            assert len(transacciones) == 12

            await j1.close()
            await j2.close()
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_un_open_tardio_no_puede_commitear_la_transaccion_de_otro(self, tmp_path):
        """P1: asignar el lock compartido **no** es ejecutar bajo el lock compartido.

        ``executescript`` emite un ``COMMIT`` implícito de cualquier transacción
        pendiente en la conexión. Como las conexiones del lifecycle son singleton
        por path, un ``open()`` tardío corría ese ``executescript`` sobre la
        conexión que otro journal estaba usando —y sin tomar el lock—, así que
        commiteaba su trabajo a medias y el ``rollback()`` posterior de la
        víctima ya no lo podía deshacer.

        La sincronización es por ``Event``, no por ``sleep``: el test suelta el
        lock recién cuando comprobó que ``j2.open()`` sigue bloqueado.
        """
        lifecycle = self._lifecycle()
        db_path = tmp_path / "commit_ajeno.db"
        try:
            j1 = OperationJournal(db_path, lifecycle=lifecycle)
            await j1.open()
            conexion = j1._db

            j2 = OperationJournal(db_path, lifecycle=lifecycle)
            schema_ejecutado = asyncio.Event()
            original_executescript = conexion.executescript

            async def executescript_observado(sql):
                schema_ejecutado.set()
                return await original_executescript(sql)

            conexion.executescript = executescript_observado  # type: ignore[method-assign]

            apertura: asyncio.Task[None]
            async with j1._lock:
                # j1 deja trabajo a medias en la conexión compartida.
                await conexion.execute("BEGIN")
                await conexion.execute(
                    "INSERT INTO transactions (description, status) VALUES ('parcial de j1', 'pending')"
                )
                assert conexion._conn.in_transaction, "premisa: hay una transacción abierta"

                # j2 arranca su open() MIENTRAS j1 sostiene el lock.
                apertura = asyncio.create_task(j2.open())
                for _ in range(50):
                    await asyncio.sleep(0)

                assert not schema_ejecutado.is_set(), (
                    "j2 ejecutó el schema sin el lock: puede commitear la transacción de j1"
                )
                assert not apertura.done(), "j2.open() no esperó al lock"
                assert conexion._conn.in_transaction, "la transacción de j1 dejó de estar pendiente"

                # j1 descarta su trabajo. Nadie debió haberlo commiteado antes.
                await conexion.rollback()

            await asyncio.wait_for(apertura, timeout=5)
            conexion.executescript = original_executescript  # type: ignore[method-assign]

            transacciones = await j1.list_recent_transactions(limit=50)
            assert [t.description for t in transacciones] == [], (
                "el trabajo parcial de j1 sobrevivió: alguien lo commiteó por su cuenta"
            )

            # Y j2 quedó plenamente utilizable tras esperar su turno.
            assert j2._db is conexion
            assert j2._lock is j1._lock
            tx_id = await j2.begin_transaction(description="post-apertura", mod_id=None)
            assert tx_id > 0

            await j1.close()
            await j2.close()
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_dos_formas_del_mismo_path_comparten_conexion_y_lock(self, tmp_path):
        """La clave del lock debe normalizarse igual que la de la conexión.

        Si ``get_write_lock`` cambiara a indexar por el string crudo mientras
        ``get_connection`` sigue resolviendo, dos journals compartirían conexión
        pero no lock — el defecto exacto que este PR cierra, reintroducido por la
        puerta de atrás. El symlink produce dos formas distintas del mismo
        archivo sin usar ``..``, que el ``PathTraversalValidator`` rechaza.
        """
        real = tmp_path / "real"
        real.mkdir()
        enlace = tmp_path / "enlace"
        try:
            enlace.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover
            # No se filtra por plataforma: Windows con Developer Mode sí puede.
            pytest.skip(f"el entorno no permite crear symlinks: {exc}")

        via_real = real / "alias.db"
        via_enlace = enlace / "alias.db"
        assert str(via_real) != str(via_enlace), "premisa: son dos formas distintas"
        assert via_real.resolve() == via_enlace.resolve(), "premisa: apuntan al mismo archivo"

        lifecycle = self._lifecycle()
        try:
            j1 = OperationJournal(via_real, lifecycle=lifecycle)
            j2 = OperationJournal(via_enlace, lifecycle=lifecycle)
            await j1.open()
            await j2.open()

            assert j1._db is j2._db, "premisa: la conexión se resuelve por path canónico"
            assert j1._lock is j2._lock, "el lock debe usar la MISMA clave que la conexión"

            await j1.close()
            await j2.close()
        finally:
            await lifecycle.shutdown_all()


# =============================================================================
# fail_operation — EVIDENCIA DEL FALLO
#
# `fail_operation` es el *failure-reporting boundary* del journal: cuando una
# operación falla, esta es la única pieza que deja constancia de POR QUÉ. El
# defecto que estos tests cierran hacía que esa constancia se perdiera en el
# 100% de las invocaciones de producción, y la suite quedaba verde porque el
# único test del método afirmaba `status == FAILED` y nunca miraba la evidencia.
#
# Las TRES propiedades que se congelan acá:
#
#   P1 — Evidencia canónica. Si `error != ""`, la fila queda FAILED *y* con la
#        clave `error`, con independencia de que se haya pasado `metadata`.
#   P2 — Transición atómica. `status` y evidencia son UNA transición: nunca
#        queda FAILED sin evidencia ni evidencia sin FAILED.
#   P3 — Fusión no destructiva. previa + argumento + {"error": error}.
#
# **Por qué TODO se parametriza sobre las dos ramas.** `fail_operation` tiene
# dos caminos —`self._lifecycle is not None` y el standalone pre-M-01— y son el
# "hermano en el mismo archivo" que AGENTS.md nombra como la clase de defecto
# dominante del repo: arreglar uno y dejar el otro. La parametrización es lo que
# convierte "verificar el árbol de callers" en un test que falla. Arreglar una
# sola rama tumba la mitad de esta clase.
#
# **Por qué se lee la fila con `sqlite3` crudo.** `_row_to_entry` hace
# `json.loads` sin guarda: leer por la API pública mezclaría el defecto de
# ESCRITURA que estos tests cubren con el de LECTURA, que es un hermano
# preexistente y fuera de alcance. La fila cruda es la única evidencia que no
# depende del camino auditado.
# =============================================================================


@pytest.fixture(params=["lifecycle", "standalone"])
async def journal_ambas_ramas(request, tmp_path):
    """Journal en las DOS ramas de `fail_operation`.

    En producción siempre se inyecta lifecycle (``app_context`` y el bootloader
    de la GUI), pero el camino standalone sigue vivo como fallback pre-M-01 y es
    el que usan varios tests. Un contrato que sólo se cumple en uno de los dos
    no está cumplido.
    """
    db_path = tmp_path / f"evidencia_{request.param}.db"
    lifecycle = None
    if request.param == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
    journal = OperationJournal(db_path, lifecycle=lifecycle)
    await journal.open()
    try:
        yield journal
    finally:
        await journal.close()
        if lifecycle is not None:
            await lifecycle.shutdown_all()


def _fila_cruda(journal, entry_id):
    """(status, metadata) con una conexión INDEPENDIENTE, sin `_row_to_entry`."""
    con = sqlite3.connect(str(journal._db_path))
    try:
        return con.execute(
            "SELECT status, metadata FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
    finally:
        con.close()


def _evidencia(journal, entry_id):
    """La metadata persistida, parseada. `None` si la columna está vacía."""
    crudo = _fila_cruda(journal, entry_id)[1]
    return json.loads(crudo) if crudo else None


def _json_valido(journal, entry_id):
    """Lo que SQLite piensa de la columna: `json_valid()` es la autoridad acá."""
    con = sqlite3.connect(str(journal._db_path))
    try:
        return con.execute(
            "SELECT json_valid(metadata) FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0]
    finally:
        con.close()


def _escribir_metadata_cruda(journal, entry_id, valor):
    """Inyecta una metadata que la API pública no podría escribir (fila legacy)."""
    con = sqlite3.connect(str(journal._db_path))
    try:
        con.execute("UPDATE journal_entries SET metadata = ? WHERE id = ?", (valor, entry_id))
        con.commit()
    finally:
        con.close()


async def _entrada(journal, metadata=None):
    """Una operación STARTED lista para fallar."""
    tx_id = await journal.begin_transaction(description="tx de evidencia", mod_id=None)
    return await journal.begin_operation(
        agent_id="agente_evidencia",
        operation_type=OperationType.FILE_MODIFY,
        target_path="/mods/objetivo.esp",
        transaction_id=tx_id,
        metadata=metadata,
    )


def _romper_el_write_de_evidencia(journal, excepcion):
    """Hace fallar el UPDATE de la evidencia dejando pasar el del estado.

    Es el fallo REAL del caso reproducido: con una fila legacy corrupta, el
    `json_set` de la implementación vieja tiraba `malformed JSON` DESPUÉS de que
    el estado ya estaba commiteado en su propia transacción.
    """
    real = journal._db.execute

    def espia(sql, *args, **kwargs):
        if isinstance(sql, str) and "SET metadata" in sql:
            raise excepcion
        return real(sql, *args, **kwargs)

    journal._db.execute = espia


def _cancelar_antes_del_write_de_evidencia(journal, tarea):
    """Cancela la tarea en la ventana entre el UPDATE del estado y el de evidencia.

    Es la ventana que la implementación de dos boundaries dejaba abierta: el
    estado ya commiteado, la evidencia todavía no escrita. Se devuelve una
    coroutine (no un proxy) porque ese UPDATE se consume con `await`, nunca con
    `async with`.
    """
    real = journal._db.execute

    def espia(sql, *args, **kwargs):
        if isinstance(sql, str) and "SET metadata" in sql:

            async def con_cancelacion():
                tarea["t"].cancel()
                await asyncio.sleep(0)  # punto de entrega de la cancelación
                return await real(sql, *args, **kwargs)

            return con_cancelacion()
        return real(sql, *args, **kwargs)

    journal._db.execute = espia


class TestFailOperationEvidencia:
    """P1 + P3: el error canónico y la fusión de la metadata."""

    # -- P1: el error se persiste SIEMPRE -------------------------------------

    @pytest.mark.asyncio
    async def test_error_sin_metadata_persiste_el_error(self, journal_ambas_ramas):
        """P1. **La forma que usan los 4 call sites de producción**, ninguno de
        los cuales pasa `metadata`: `sync_engine` (x2), `rollback_manager` —cuya
        fachada no expone el parámetro— y `grass_cache_service`.

        El gate era `if metadata:`, sobre una variable distinta de la que el
        UPDATE escribía: sin metadata no se escribía nada, ni el error.
        """
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)

        await journal.fail_operation(entry_id, error="boom")

        status, _ = _fila_cruda(journal, entry_id)
        assert status == OperationStatus.FAILED.value
        assert _evidencia(journal, entry_id) == {"error": "boom"}, "el error canónico se perdió"

    @pytest.mark.asyncio
    async def test_previa_con_metadata_y_sin_argumento_persiste_el_error(self, journal_ambas_ramas):
        """P1. El caso de `grass_cache_service`: la FILA ya tiene metadata (la
        puso `begin_operation`) pero el ARGUMENTO es None.

        El gate mirando el argumento hacía que ni siquiera se intentara el
        write, con la columna lista para recibir el error. Ese caller documenta
        en `grass_cache_service.py:588` que mete la metadata estructurada en
        `begin_operation` "porque fail_operation solo persiste $.error" — y ni
        eso pasaba.
        """
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal, metadata={"kind": "teardown_incompleto", "paths": ["/a"]})

        await journal.fail_operation(entry_id, error="no se pudo borrar /a")

        assert _evidencia(journal, entry_id) == {
            "kind": "teardown_incompleto",
            "paths": ["/a"],
            "error": "no se pudo borrar /a",
        }

    @pytest.mark.asyncio
    async def test_error_vacio_no_crea_la_clave(self, journal_ambas_ramas):
        """P1. Ausencia de mensaje y mensaje vacío no son lo mismo: `{"error": ""}`
        es ruido que no distingue ninguno de los dos casos."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)

        await journal.fail_operation(entry_id)

        status, metadata = _fila_cruda(journal, entry_id)
        assert status == OperationStatus.FAILED.value, "el estado se registra igual"
        assert metadata is None, f"no debe crearse la clave error: {metadata!r}"

    @pytest.mark.asyncio
    async def test_error_vacio_conserva_la_metadata_previa(self, journal_ambas_ramas):
        """P1 + P3: un `error` vacío no crea la clave, pero tampoco destruye lo
        que ya había."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal, metadata={"kind": "manifiesto"})

        await journal.fail_operation(entry_id, error="")

        assert _evidencia(journal, entry_id) == {"kind": "manifiesto"}

    # -- P3: fusión y precedencia ---------------------------------------------

    @pytest.mark.asyncio
    async def test_metadata_argumento_sobrevive(self, journal_ambas_ramas):
        """P3. El argumento se usaba SOLO como booleano: el UPDATE escribía
        `$.error` y el dict nunca se serializaba."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)

        await journal.fail_operation(entry_id, error="boom", metadata={"source": "test"})

        assert _evidencia(journal, entry_id) == {"source": "test", "error": "boom"}

    @pytest.mark.asyncio
    async def test_metadata_previa_y_argumento_se_fusionan(self, journal_ambas_ramas):
        """P3. Las tres fuentes coexisten. Nótese que la implementación vieja SÍ
        conservaba la previa (`json_set` fusiona) — lo que perdía era el
        argumento."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal, metadata={"before": "keep"})

        await journal.fail_operation(entry_id, error="boom", metadata={"after": "keep"})

        assert _evidencia(journal, entry_id) == {
            "before": "keep",
            "after": "keep",
            "error": "boom",
        }

    @pytest.mark.asyncio
    async def test_argumento_pisa_la_previa(self, journal_ambas_ramas):
        """P3. Precedencia: el argumento es información MÁS NUEVA sobre la misma
        operación."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal, metadata={"intento": 1, "kind": "install"})

        await journal.fail_operation(entry_id, error="boom", metadata={"intento": 2})

        assert _evidencia(journal, entry_id) == {"intento": 2, "kind": "install", "error": "boom"}

    @pytest.mark.asyncio
    async def test_error_canonico_pisa_metadata_con_clave_error(self, journal_ambas_ramas):
        """P3. La clave `error` es canónica del PARÁMETRO `error` y se aplica al
        final: un `metadata={"error": ...}` no puede sustituirla en silencio. Si
        el caller quiere ese texto como mensaje, lo pasa por su parámetro."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)

        await journal.fail_operation(
            entry_id,
            error="el error real",
            metadata={"error": "impostor"},
        )

        assert _evidencia(journal, entry_id)["error"] == "el error real"

    @pytest.mark.asyncio
    async def test_valor_none_en_la_previa_no_se_borra(self, journal_ambas_ramas):
        """P3. La fusión se resuelve en Python y NO con `json_patch` a propósito:
        RFC 7396 **borra** las claves cuyo valor es `null`, que sería la misma
        pérdida de evidencia que este contrato cierra."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        _escribir_metadata_cruda(journal, entry_id, json.dumps({"snapshot": None}))

        await journal.fail_operation(entry_id, error="boom", metadata={"aux": None})

        assert _evidencia(journal, entry_id) == {"snapshot": None, "aux": None, "error": "boom"}

    # -- representación -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_evidencia_nace_json_valido(self, journal_ambas_ramas):
        """La evidencia es dato NUEVO y debe nacer válida: el default de
        `json.dumps` emite `NaN`/`Infinity`, que no son JSON válido — la columna
        quedaría con `json_valid() == 0` y `json_extract` devolvería NULL en
        silencio, otra pérdida de evidencia disfrazada de dato presente."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)

        await journal.fail_operation(entry_id, error="boom", metadata={"ratio": 0.5})

        assert _json_valido(journal, entry_id) == 1
        assert _evidencia(journal, entry_id) == {"ratio": 0.5, "error": "boom"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "valor",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="infinity"),
            pytest.param(float("-inf"), id="menos_infinity"),
        ],
    )
    async def test_metadata_argumento_no_finita_propaga_sin_corromper_la_columna(self, journal_ambas_ramas, valor):
        """La metadata del ARGUMENTO se mantiene estricta y propaga.

        Es la asimetría deliberada con la metadata previa: ahí hay un dato
        preexistente que rescatar y el fallo nuevo tiene prioridad; acá el dato es
        del caller y tragárselo ocultaría un bug suyo — mismo criterio que
        ``begin_operation``, el otro escritor de la columna.

        Lo que **no** puede pasar es que la columna quede con JSON inválido: el
        default de ``json.dumps`` emite ``NaN``/``Infinity``, que SQLite no
        considera JSON (``json_valid() == 0``) y sobre los que ``json_extract``
        devuelve NULL en silencio — evidencia perdida disfrazada de dato presente.

        Además prueba P2 desde el otro lado: el fallo de serialización ocurre
        DENTRO del boundary, así que el ``UPDATE`` del estado se deshace.
        """
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)

        with pytest.raises(ValueError):
            await journal.fail_operation(entry_id, error="boom", metadata={"x": valor})

        status, metadata = _fila_cruda(journal, entry_id)
        assert status == OperationStatus.STARTED.value, "el UPDATE del estado no se deshizo"
        assert metadata is None
        assert _json_valido(journal, entry_id) is None, "la columna no puede quedar con JSON inválido"

    @pytest.mark.asyncio
    async def test_entrada_inexistente_es_no_op(self, journal_ambas_ramas):
        """Contrato preexistente que no cambia: un `entry_id` que no existe no
        crea filas ni propaga."""
        journal = journal_ambas_ramas

        await journal.fail_operation(999_999, error="boom", metadata={"k": "v"})

        con = sqlite3.connect(str(journal._db_path))
        try:
            assert con.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        finally:
            con.close()


class TestFailOperationAtomicidad:
    """P2: `status` y evidencia forman UNA transición.

    Antes eran dos: `_update_status` abría, commiteaba y cerraba su propia
    transacción, y el write de evidencia era otra. El propio código lo declaraba
    —"DOS boundaries separados (deuda #466 de atomicidad preservada, no resuelta
    acá)"—, y la consecuencia observada era una fila FAILED con la evidencia
    ausente de forma DURABLE.
    """

    @staticmethod
    def _sin_estado_parcial(journal, entry_id, previa):
        """El invariante de P2, en su forma observable.

        No se afirma un desenlace único —según dónde caiga la cancelación, el
        commit puede haber completado o no, y las dos cosas son correctas— sino
        que el par (status, evidencia) es CONSISTENTE: o la transición se aplicó
        entera, o no se aplicó. Lo prohibido es el estado intermedio.
        """
        status, metadata = _fila_cruda(journal, entry_id)
        aplicado = status == OperationStatus.FAILED.value and metadata and "error" in json.loads(metadata)
        intacto = status == OperationStatus.STARTED.value and metadata == previa
        assert aplicado or intacto, f"estado PARCIAL: status={status!r} metadata={metadata!r} (previa={previa!r})"

    @pytest.mark.asyncio
    async def test_atomicidad_fail_operation(self, journal_ambas_ramas):
        """P2. Si el write de la evidencia falla, el UPDATE del estado se
        deshace: no queda FAILED sin su evidencia."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        previa = _fila_cruda(journal, entry_id)[1]

        _romper_el_write_de_evidencia(journal, sqlite3.OperationalError("malformed JSON"))

        with pytest.raises(sqlite3.OperationalError):
            await journal.fail_operation(entry_id, error="boom", metadata={"k": "v"})

        status, metadata = _fila_cruda(journal, entry_id)
        assert status == OperationStatus.STARTED.value, "el UPDATE del estado no se deshizo"
        assert metadata == previa

    @pytest.mark.asyncio
    async def test_sin_transaccion_colgante_tras_fallo(self, journal_ambas_ramas):
        """P2. Soltar el boundary con la transacción abierta deja que el
        siguiente escritor de la conexión compartida commitee el estado parcial
        que este contrato descarta."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        _romper_el_write_de_evidencia(journal, sqlite3.OperationalError("boom"))

        with pytest.raises(sqlite3.OperationalError):
            await journal.fail_operation(entry_id, error="boom", metadata={"k": "v"})

        assert journal._db.in_transaction is False

    @pytest.mark.asyncio
    async def test_cancelacion_antes_del_commit_no_deja_estado_parcial(self, journal_ambas_ramas):
        """P2 bajo cancelación, en la ventana EXACTA que los dos boundaries
        dejaban abierta: estado ya commiteado, evidencia todavía no escrita."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        previa = _fila_cruda(journal, entry_id)[1]

        tarea = {}
        _cancelar_antes_del_write_de_evidencia(journal, tarea)
        tarea["t"] = asyncio.create_task(journal.fail_operation(entry_id, error="boom", metadata={"k": "v"}))

        with contextlib.suppress(asyncio.CancelledError):
            await tarea["t"]

        self._sin_estado_parcial(journal, entry_id, previa)

    @pytest.mark.asyncio
    async def test_la_cancelacion_no_se_convierte_en_exito(self, journal_ambas_ramas):
        """Una cancelación no puede terminar como retorno normal: quien canceló
        creería haber cancelado algo que siguió su curso."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)

        tarea = {}
        _cancelar_antes_del_write_de_evidencia(journal, tarea)
        tarea["t"] = asyncio.create_task(journal.fail_operation(entry_id, error="boom", metadata={"k": "v"}))

        with contextlib.suppress(asyncio.CancelledError):
            await tarea["t"]

        assert tarea["t"].cancelled() or tarea["t"].exception() is not None, (
            "la cancelación se absorbió y la tarea terminó como éxito"
        )


class TestFailOperationMetadataPreviaIlegible:
    """Una fila con metadata que no se puede fusionar no puede impedir que se
    registre el fallo.

    Es una asimetría DELIBERADA con `begin_operation`, que valida estricto y
    propaga: ese método *crea* una operación y no crearla ante datos malos es la
    respuesta correcta. Este *reporta un fallo que ya ocurrió*: si tirara, la
    operación quedaría en STARTED y el error real desaparecería del journal — el
    reporte del fallo se convertiría en el fallo.

    El alcance de la degradación es EXCLUSIVAMENTE la metadata previa ilegible.
    Errores de SQLite, del commit o del rollback conservan su contrato y
    propagan (`TestFailOperationAtomicidad`).
    """

    # Cada forma viaja con el MOTIVO exacto que debe reportar el clasificador: un
    # motivo nuevo sin caso, o un caso que cambia de motivo, rompe el ancla de
    # igualdad de más abajo en vez de pasar con un `assert motivo` genérico.
    FORMAS_ILEGIBLES = [
        pytest.param("texto plano no json", "json_invalido", id="no_es_json"),
        pytest.param("{trunca", "json_invalido", id="json_truncado"),
        pytest.param("[]", "raiz_no_objeto", id="raiz_lista"),
        pytest.param('"solo un string"', "raiz_no_objeto", id="raiz_string"),
        pytest.param("42", "raiz_no_objeto", id="raiz_numero"),
        pytest.param("null", "raiz_no_objeto", id="raiz_null"),
        pytest.param("true", "raiz_no_objeto", id="raiz_bool"),
        pytest.param('{"x": NaN}', "no_reserializable", id="nan_de_fila_legacy"),
    ]

    # Los tests que solo necesitan el blob, derivados de la MISMA fuente para que
    # no puedan desincronizarse.
    SOLO_CRUDOS = [pytest.param(caso.values[0], id=caso.id) for caso in FORMAS_ILEGIBLES]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("crudo", "motivo_esperado"), FORMAS_ILEGIBLES)
    async def test_registra_el_fallo_igual(self, journal_ambas_ramas, caplog, crudo, motivo_esperado):
        """La fila queda FAILED, con el error canónico y el motivo EXACTO."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        _escribir_metadata_cruda(journal, entry_id, crudo)

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="boom")

        status, _ = _fila_cruda(journal, entry_id)
        assert status == OperationStatus.FAILED.value
        assert _evidencia(journal, entry_id) == {"error": "boom"}

        motivos = [
            r.motivo for r in caplog.records if getattr(r, "event", None) == "journal_metadata_previa_descartada"
        ]
        assert motivos == [motivo_esperado], f"motivo inesperado para {crudo!r}: {motivos}"

    def test_ancla_motivos_de_descarte_cubiertos(self):
        """Igualdad literal: los motivos que el clasificador puede emitir son
        EXACTAMENTE los que estos casos cubren.

        Sin esto, agregar un motivo nuevo a `_metadata_previa_fusionable` no
        rompía nada — que es cómo `limite_de_recursion` llegó a existir sin caso
        propio hasta que lo señaló la revisión.
        """
        fuente = textwrap.dedent(inspect.getsource(OperationJournal._metadata_previa_fusionable))
        emitidos = {
            nodo.value.value
            for nodo in ast.walk(ast.parse(fuente))
            if isinstance(nodo, ast.Assign)
            and isinstance(nodo.value, ast.Constant)
            and isinstance(nodo.value.value, str)
            and any(isinstance(t, ast.Name) and t.id == "motivo" for t in nodo.targets)
        }
        emitidos |= {
            nodo.value.value
            for nodo in ast.walk(ast.parse(fuente))
            if isinstance(nodo, ast.Return)
            and isinstance(nodo.value, ast.Tuple)
            and len(nodo.value.elts) == 2
            and isinstance(nodo.value.elts[1], ast.Constant)
            and isinstance(nodo.value.elts[1].value, str)
            for _ in [0]
        }
        cubiertos = {caso.values[1] for caso in self.FORMAS_ILEGIBLES}
        # `limite_de_recursion` tiene sus propios tests (por inyección y por
        # profundidad real), no un blob en FORMAS_ILEGIBLES: no se puede escribir
        # una columna que desborde el parser sin desbordarlo al escribirla.
        cubiertos |= {"limite_de_recursion"}
        assert emitidos == cubiertos, (
            f"motivos sin caso que los cubra: {sorted(emitidos - cubiertos)}; "
            f"casos que esperan un motivo inexistente: {sorted(cubiertos - emitidos)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("crudo", SOLO_CRUDOS)
    async def test_no_propaga(self, journal_ambas_ramas, crudo):
        """Reproducción del caso fatal: la implementación vieja tiraba
        `sqlite3.OperationalError: malformed JSON` con el estado YA commiteado —
        y en `sync_engine.py:428` esa excepción reemplaza la original y saltea el
        `undo_operation` que revierte el archivo."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        _escribir_metadata_cruda(journal, entry_id, crudo)

        await journal.fail_operation(entry_id, error="boom", metadata={"aux": "v"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("crudo", SOLO_CRUDOS)
    async def test_la_columna_queda_json_valido(self, journal_ambas_ramas, crudo):
        """La fila se repara al pasar: deja de ser ilegible para el siguiente
        lector."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        _escribir_metadata_cruda(journal, entry_id, crudo)

        await journal.fail_operation(entry_id, error="boom", metadata={"aux": "v"})

        assert _json_valido(journal, entry_id) == 1
        assert _evidencia(journal, entry_id) == {"aux": "v", "error": "boom"}

    @pytest.mark.asyncio
    async def test_el_warning_no_filtra_el_contenido_descartado(self, journal_ambas_ramas, caplog):
        """El blob descartado puede traer rutas, nombres de perfil de MO2 o
        nombres de mod: sale el `entry_id` y el motivo, nunca el contenido."""
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        secreto = '{"perfil": "C:/Users/facundo/MO2/profiles/SECRETO"'
        _escribir_metadata_cruda(journal, entry_id, secreto)

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="boom")

        avisos = [r for r in caplog.records if getattr(r, "event", None) == "journal_metadata_previa_descartada"]
        assert avisos, "descartar evidencia previa sin avisar la vuelve invisible"
        texto_completo = " ".join([r.getMessage() for r in caplog.records] + [str(r.__dict__) for r in caplog.records])
        assert "SECRETO" not in texto_completo, "el WARNING filtró el contenido descartado"
        assert getattr(avisos[0], "entry_id", None) == entry_id
        assert getattr(avisos[0], "motivo", None)

    # -- desborde de recursión (hallazgo de Codex en #487, reproducido) --------
    #
    # `RecursionError` hereda de `RuntimeError`, no de `ValueError`/`TypeError`:
    # se escapaba del clasificador y hundía el reporte del fallo, que es
    # exactamente lo que su docstring promete que no pasa. Y en
    # `sync_engine.py:428` esa excepción además saltea el `undo_operation`.
    #
    # Se prueba INYECTANDO el error, no anidando 1000 niveles: el límite de
    # recursión del parser JSON en C se separó del de Python en 3.12, así que un
    # test atado a la profundidad pasa en verde SIN ejercitar el brazo en algunas
    # versiones — cobertura fantasma. El test de profundidad real va aparte y se
    # saltea explícitamente si el runtime no desborda, en vez de pasar en falso.

    @pytest.mark.asyncio
    @pytest.mark.parametrize("donde", ["loads", "dumps"])
    async def test_desborde_de_recursion_no_impide_registrar_el_fallo(self, journal_ambas_ramas, monkeypatch, donde):
        """El contrato, sin depender de cuántos frames consume el parser de C.

        Los dos puntos donde la metadata previa puede desbordar: al DECODIFICARLA
        y al RE-SERIALIZARLA para el chequeo de representabilidad.
        """
        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        centinela = {"profunda": True}
        _escribir_metadata_cruda(journal, entry_id, json.dumps(centinela))

        real = getattr(json, donde)

        def desborda(valor, *args, **kwargs):
            # Solo para NUESTRO payload: parchear json global sin filtro rompería
            # a terceros durante el test.
            if valor == (json.dumps(centinela) if donde == "loads" else centinela):
                raise RecursionError("maximum recursion depth exceeded")
            return real(valor, *args, **kwargs)

        monkeypatch.setattr(json, donde, desborda)

        await journal.fail_operation(entry_id, error="boom")

        status, _ = _fila_cruda(journal, entry_id)
        assert status == OperationStatus.FAILED.value, "el desborde hundió el reporte del fallo"
        assert _evidencia(journal, entry_id) == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_previa_realmente_profunda_no_impide_registrar_el_fallo(self, journal_ambas_ramas):
        """Gemelo del anterior con profundidad REAL, no inyectada.

        Verifica primero que el runtime actual desborde de verdad: si una versión
        futura tolera esta profundidad, hace `skip` con el motivo en vez de pasar
        en falso sin ejercitar nada.
        """
        niveles = 20_000
        blob = '{"a":' * niveles + "null" + "}" * niveles
        try:
            json.loads(blob)
        except RecursionError:
            pass
        else:  # pragma: no cover — depende de la versión de CPython
            pytest.skip(f"este runtime no desborda con {niveles} niveles; el test inyectado cubre el contrato")

        journal = journal_ambas_ramas
        entry_id = await _entrada(journal)
        _escribir_metadata_cruda(journal, entry_id, blob)

        await journal.fail_operation(entry_id, error="boom")

        assert _fila_cruda(journal, entry_id)[0] == OperationStatus.FAILED.value
        assert _evidencia(journal, entry_id) == {"error": "boom"}


class TestFailOperationRollbackFallidoStandalone:
    """Rollback fallido en el camino standalone: la conexión se RETIRA.

    Hallazgo de Codex en #487, reproducido antes de aceptarlo: si el write de
    evidencia falla y el `rollback` TAMBIÉN falla, la transacción queda abierta
    sobre la conexión propia y el siguiente escritor la commitea. Observado con
    `complete_operation` de una operación **no relacionada**: el `FAILED` parcial
    —sin su evidencia— quedaba durable.

    Solo aplica al camino standalone. Con lifecycle, `transaction()` ya cierra la
    conexión y pone el path en cuarentena vía `_invalidate_under_boundary`; hacer
    lo mismo acá competiría con esa política. Y NO se usa `evict_connection`: esa
    API está congelada sin callers de producción
    (`test_g2_evict_connection_quedo_sin_callers_en_produccion`) y acá no hay
    lifecycle al que pedírsela — la conexión es propia, alcanza cerrarla y soltar
    la referencia para que `_ensure_connected()` reabra una limpia.

    Invariante de `app/db/AGENTS.md`: *"un rollback fallido conserva evidencia y
    no se reporta como éxito"*.
    """

    @staticmethod
    def _romper_write_y_rollback(journal):
        real_execute = journal._db.execute

        def execute_que_falla(sql, *args, **kwargs):
            if isinstance(sql, str) and "SET metadata" in sql:
                raise sqlite3.OperationalError("write de evidencia falla")
            return real_execute(sql, *args, **kwargs)

        async def rollback_que_falla():
            raise sqlite3.OperationalError("rollback falla")

        journal._db.execute = execute_que_falla
        journal._db.rollback = rollback_que_falla

    @pytest.fixture
    async def journal_standalone(self, tmp_path):
        journal = OperationJournal(tmp_path / "rollback_sucio.db")
        await journal.open()
        try:
            yield journal
        finally:
            await journal.close()

    @pytest.mark.asyncio
    async def test_conserva_la_excepcion_primaria(self, journal_standalone):
        """El fallo del rollback no puede reemplazar el del write."""
        journal = journal_standalone
        entry_id = await _entrada(journal)
        self._romper_write_y_rollback(journal)

        with pytest.raises(sqlite3.OperationalError, match="write de evidencia falla"):
            await journal.fail_operation(entry_id, error="boom", metadata={"k": "v"})

    @pytest.mark.asyncio
    async def test_retira_la_conexion(self, journal_standalone):
        """La conexión sucia no queda instalada para el próximo escritor."""
        journal = journal_standalone
        entry_id = await _entrada(journal)
        sucia = journal._db
        self._romper_write_y_rollback(journal)

        with contextlib.suppress(sqlite3.OperationalError):
            await journal.fail_operation(entry_id, error="boom", metadata={"k": "v"})

        assert journal._db is None, "la conexión con la transacción abierta quedó instalada"
        assert journal._db is not sucia

    @pytest.mark.asyncio
    async def test_el_siguiente_escritor_no_commitea_el_fallo_parcial(self, journal_standalone):
        """La propiedad que de verdad importa: sin contaminación cruzada.

        Reproducido sobre el código sin este arreglo: `complete_operation` de otra
        operación commiteaba el `FAILED` sin evidencia de la primera.
        """
        journal = journal_standalone
        fallida = await _entrada(journal)
        otra = await _entrada(journal)
        self._romper_write_y_rollback(journal)

        with contextlib.suppress(sqlite3.OperationalError):
            await journal.fail_operation(fallida, error="boom", metadata={"k": "v"})

        # El siguiente escritor reabre una conexión limpia y commitea LO SUYO.
        await journal.complete_operation(otra)

        status, metadata = _fila_cruda(journal, fallida)
        assert status == OperationStatus.STARTED.value, (
            f"el FAILED parcial se commiteó con el trabajo de otra operación: metadata={metadata!r}"
        )
        assert metadata is None
        assert _fila_cruda(journal, otra)[0] == OperationStatus.COMPLETED.value, (
            "retirar la conexión no puede impedir el trabajo posterior"
        )

    # -- cancelación NUEVA durante la limpieza (hallazgo de Qodo en #487) ------
    #
    # Dos invariantes que parecían en tensión y NO lo están:
    #
    #   1. Una cancelación nueva durante el cleanup conserva su identidad y
    #      propaga — nunca se absorbe.
    #   2. Una conexión con la transacción abierta no puede quedar instalada.
    #
    # El primer intento cumplía (1) y rompía (2): el `except Exception` del
    # rollback deja pasar la `CancelledError` —correcto— pero con eso se salteaba
    # el retiro. Reproducido: `in_transaction: True`, conexión instalada, y
    # `complete_operation` de otra entrada commiteando el `FAILED` parcial.
    #
    # Se cumplen las dos: el retiro corre en `finally` (no absorbe nada) y suelta
    # la referencia ANTES de intentar el cierre, que es la parte que no puede
    # depender de que un `await` sobreviva a la cancelación.

    @staticmethod
    def _romper_write_y_cancelar_rollback(journal):
        real_execute = journal._db.execute

        def execute_que_falla(sql, *args, **kwargs):
            if isinstance(sql, str) and "SET metadata" in sql:
                raise sqlite3.OperationalError("write de evidencia falla")
            return real_execute(sql, *args, **kwargs)

        async def rollback_cancelado():
            raise asyncio.CancelledError()

        journal._db.execute = execute_que_falla
        journal._db.rollback = rollback_cancelado

    @pytest.mark.asyncio
    async def test_cancelacion_durante_el_rollback_propaga(self, journal_standalone):
        """Invariante 1: no se absorbe."""
        journal = journal_standalone
        entry_id = await _entrada(journal)
        self._romper_write_y_cancelar_rollback(journal)

        with pytest.raises(asyncio.CancelledError):
            await journal.fail_operation(entry_id, error="boom", metadata={"k": "v"})

    @pytest.mark.asyncio
    async def test_cancelacion_durante_el_rollback_igual_retira_la_conexion(self, journal_standalone):
        """Invariante 2: propagar la cancelación no puede saltear el retiro."""
        journal = journal_standalone
        entry_id = await _entrada(journal)
        self._romper_write_y_cancelar_rollback(journal)

        with contextlib.suppress(asyncio.CancelledError):
            await journal.fail_operation(entry_id, error="boom", metadata={"k": "v"})

        assert journal._db is None, "la cancelación del rollback salteó el retiro de la conexión"

    @pytest.mark.asyncio
    async def test_cancelacion_durante_el_rollback_no_contamina_al_siguiente(self, journal_standalone):
        """La propiedad observable: sin `FAILED` parcial durable."""
        journal = journal_standalone
        fallida = await _entrada(journal)
        otra = await _entrada(journal)
        self._romper_write_y_cancelar_rollback(journal)

        with contextlib.suppress(asyncio.CancelledError):
            await journal.fail_operation(fallida, error="boom", metadata={"k": "v"})

        await journal.complete_operation(otra)

        status, metadata = _fila_cruda(journal, fallida)
        assert status == OperationStatus.STARTED.value, (
            f"el FAILED parcial se commiteó tras una cancelación del rollback: metadata={metadata!r}"
        )
        assert _fila_cruda(journal, otra)[0] == OperationStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_cancelacion_durante_el_cierre_igual_suelta_la_referencia(self, journal_standalone):
        """El cierre es best-effort; soltar la referencia NO.

        `await db.close()` puede recibir la cancelación pendiente y no completar.
        Por eso la referencia se suelta antes: aunque el fd quede vivo, ningún
        escritor del journal puede volver a alcanzar esa conexión sucia.
        """
        journal = journal_standalone
        entry_id = await _entrada(journal)
        self._romper_write_y_cancelar_rollback(journal)

        sucia = journal._db
        cierre_real = sucia.close

        async def close_cancelado():
            raise asyncio.CancelledError()

        sucia.close = close_cancelado
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await journal.fail_operation(entry_id, error="boom", metadata={"k": "v"})

            assert journal._db is None, "un cierre cancelado dejó la conexión sucia instalada"
        finally:
            # El fd (y su hilo worker non-daemon) sobreviven a un cierre
            # cancelado: es el costo aceptado de garantizar el no-reuso, y acá lo
            # limpia el test para no disparar el guard de `pytest_unconfigure`,
            # que sale con os._exit(3) ante hilos sin cerrar.
            sucia.close = cierre_real
            with contextlib.suppress(Exception):
                await sucia.close()


class TestFailOperationHermanos:
    """Los otros caminos que llegan al MISMO recurso.

    Enumerados sobre el código actual, no heredados de una lista histórica. Las
    variantes de journal y la fachada de `RollbackManager` deben satisfacer P1
    sin cambios propios: si alguna dejara de reenviar, este bloque lo detecta.

    La familia de subclases se descubre por INTROSPECCIÓN y se congela: una
    variante nueva de journal rompe el ancla hasta que se le escriba su caso, en
    vez de heredar en silencio un `fail_operation` que quizá no reenvía.
    """

    # Subclases de OperationJournal, congeladas por igualdad. Cada una necesita
    # su prueba de comportamiento más abajo.
    SUBCLASES_ESPERADAS = {"NoOpJournal", "StagingJournal"}

    def test_ancla_familia_de_journals_congelada(self):
        """Igualdad literal sobre `__subclasses__()`: una variante nueva rompe acá."""
        observadas = {sub.__name__ for sub in OperationJournal.__subclasses__()}
        assert observadas == self.SUBCLASES_ESPERADAS, (
            "cambió la familia de journals: la variante nueva necesita su propia "
            "prueba de que fail_operation sigue satisfaciendo P1 (reenviar o no "
            "tener efecto), no heredarla por inercia"
        )

    def test_ancla_firma_de_fail_operation_compatible_en_toda_la_familia(self):
        """Toda subclase acepta la MISMA llamada que la base.

        `StagingJournal` usa `*args/**kwargs` —compatible por construcción— y
        `NoOpJournal` repite la firma explícita. Lo que se verifica es que la
        llamada canónica se pueda ligar en todas, no que el texto coincida:
        `NoOpJournal` con un parámetro renombrado rompería a los callers que usan
        keywords, y hoy nada lo detectaba.
        """
        for clase in [OperationJournal, *OperationJournal.__subclasses__()]:
            firma = inspect.signature(clase.fail_operation)
            firma.bind(
                object(),  # self
                1,
                error="boom",
                metadata={"k": "v"},
            )

    @pytest.mark.asyncio
    async def test_rollback_manager_fachada_persiste_el_error(self, tmp_path):
        """El camino REAL de `sync_engine`: la fachada no expone `metadata`, así
        que su única forma de uso es exactamente la que se perdía."""
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        journal = OperationJournal(tmp_path / "fachada.db", lifecycle=lifecycle)
        await journal.open()
        try:
            rm = RollbackManager(
                journal=journal,
                snapshot_manager=FileSnapshotManager(snapshot_dir=tmp_path / "snaps"),
            )
            entry_id = await _entrada(journal)

            await rm.fail_operation(entry_id, error="fallo via fachada")

            assert _evidencia(journal, entry_id) == {"error": "fallo via fachada"}
        finally:
            await journal.close()
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_staging_journal_hereda_el_contrato(self, tmp_path):
        """Reenvía con `*args/**kwargs`: hereda el fix sin tocarse."""
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        real = OperationJournal(tmp_path / "staging.db", lifecycle=lifecycle)
        await real.open()
        try:
            entry_id = await _entrada(real)
            staging = StagingJournal(real)

            await staging.fail_operation(entry_id, error="boom", metadata={"source": "staging"})

            assert _evidencia(real, entry_id) == {"source": "staging", "error": "boom"}
        finally:
            await real.close()
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_noop_journal_firma_compatible(self):
        """Firma idéntica y sin efecto: un `fail_operation` con metadata no puede
        tirar `TypeError` en el modo no-op."""
        await NoOpJournal().fail_operation(1, error="boom", metadata={"k": "v"})
