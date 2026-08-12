# tests/test_journal.py

import asyncio
import contextlib
import pathlib
import sqlite3
from unittest.mock import AsyncMock

import pytest

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.app.db.journal import (
    JournalConnectionError,
    OperationJournal,
    OperationStatus,
    OperationType,
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
