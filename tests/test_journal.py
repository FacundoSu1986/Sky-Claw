# tests/test_journal.py

import asyncio
import contextlib
import hashlib
import json
import logging
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
        # El estado solo NO alcanza: este test afirmaba únicamente FAILED y por eso
        # la suite quedaba verde mientras el mensaje de error se perdía.
        assert entry.metadata == {"error": "Test error"}

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


class TestFailOperationEvidencia:
    """Contrato de evidencia de ``fail_operation``.

    El defecto que cierran estos tests: ``metadata`` era solo un gate de
    truthiness —nunca se persistía— y el ``error`` únicamente se escribía cuando
    ese gate se cumplía, así que ``fail_operation(id, error="boom")`` —la forma
    que usan los cuatro call sites de producción— dejaba la fila ``FAILED`` sin
    mensaje.
    """

    @staticmethod
    async def _entrada(journal, *, metadata=None) -> int:
        """Crea una operación STARTED y devuelve su id."""
        tx_id = await journal.begin_transaction(description="tx evidencia", mod_id=None)
        return await journal.begin_operation(
            agent_id="agente_evidencia",
            operation_type=OperationType.FILE_MODIFY,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
            metadata=metadata,
        )

    # -- 1. error sin metadata: EL caso de producción ------------------------

    @pytest.mark.asyncio
    async def test_error_sin_metadata_persiste_el_mensaje(self, journal):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="boom")

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "boom"}

    # -- 2. metadata sin error -----------------------------------------------

    @pytest.mark.asyncio
    async def test_metadata_sin_error_se_persiste_entera(self, journal):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, metadata={"exit_code": 3, "tool": "loot"})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"exit_code": 3, "tool": "loot"}

    # -- 3. error + metadata --------------------------------------------------

    @pytest.mark.asyncio
    async def test_error_y_metadata_conviven(self, journal):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="boom", metadata={"exit_code": 3})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"exit_code": 3, "error": "boom"}

    # -- 4 y 5. metadata previa y claves ajenas preservadas -------------------

    @pytest.mark.asyncio
    async def test_metadata_previa_se_conserva_y_se_fusiona(self, journal):
        entry_id = await self._entrada(
            journal,
            metadata={"kind": "teardown_incomplete", "paths": ["a", "b"]},
        )

        await journal.fail_operation(entry_id, error="boom", metadata={"exit_code": 3})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {
            "kind": "teardown_incomplete",
            "paths": ["a", "b"],
            "exit_code": 3,
            "error": "boom",
        }

    @pytest.mark.asyncio
    async def test_solo_error_no_borra_la_metadata_previa(self, journal):
        entry_id = await self._entrada(journal, metadata={"kind": "x", "paths": ["a"]})

        await journal.fail_operation(entry_id, error="boom")

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"kind": "x", "paths": ["a"], "error": "boom"}

    @pytest.mark.asyncio
    async def test_la_metadata_nueva_pisa_la_previa_en_colision(self, journal):
        """Precedencia documentada: el argumento es información más nueva."""
        entry_id = await self._entrada(journal, metadata={"intento": 1, "ajena": "intacta"})

        await journal.fail_operation(entry_id, metadata={"intento": 2})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"intento": 2, "ajena": "intacta"}

    # -- 6 y 7. vacíos --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_metadata_vacia_no_impide_registrar_el_error(self, journal):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="boom", metadata={})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_error_vacio_no_crea_la_clave(self, journal):
        """Ausencia de mensaje y mensaje vacío no son lo mismo."""
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="", metadata={"exit_code": 3})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"exit_code": 3}
        assert "error" not in entry.metadata

    @pytest.mark.asyncio
    async def test_sin_error_ni_metadata_solo_marca_failed(self, journal):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata is None

    # -- 8. entrada inexistente ----------------------------------------------

    @pytest.mark.asyncio
    async def test_entrada_inexistente_es_no_op_sin_excepcion(self, journal):
        """Contrato preexistente: no lanza y no crea filas."""
        await journal.fail_operation(999_999, error="boom", metadata={"exit_code": 3})

        assert await journal.get_operation_by_id(999_999) is None

    # -- 9. JSON válido en la columna ----------------------------------------

    @pytest.mark.asyncio
    async def test_la_columna_queda_con_json_valido(self, journal):
        entry_id = await self._entrada(journal, metadata={"previa": True})

        await journal.fail_operation(entry_id, error="boom", metadata={"anidado": {"a": [1, 2]}})

        async with journal._db.execute(
            "SELECT json_valid(metadata), metadata FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            valido, crudo = await cursor.fetchone()
        assert valido == 1
        assert json.loads(crudo) == {
            "previa": True,
            "anidado": {"a": [1, 2]},
            "error": "boom",
        }

    @pytest.mark.asyncio
    async def test_un_valor_none_se_conserva_no_se_borra(self, journal):
        """No se usa json_patch: RFC 7396 borraría la clave por ser null."""
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, metadata={"exit_code": None})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"exit_code": None}

    # -- 10. reintentos / idempotencia ---------------------------------------

    @pytest.mark.asyncio
    async def test_reintento_acumula_sin_perder_lo_anterior(self, journal):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="primero", metadata={"intento": 1})
        await journal.fail_operation(entry_id, error="segundo", metadata={"otra": "x"})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"intento": 1, "otra": "x", "error": "segundo"}

    # -- 11. atomicidad -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_fallo_de_db_revierte_y_deja_la_conexion_usable(self, journal, monkeypatch):
        entry_id = await self._entrada(journal, metadata={"previa": True})
        original = journal._db.execute
        llamadas = {"n": 0}

        def execute_que_falla_en_el_segundo_update(sql, *args, **kwargs):
            if sql.strip().startswith("UPDATE journal_entries SET metadata"):
                llamadas["n"] += 1
                raise sqlite3.OperationalError("fallo forzado antes del commit")
            return original(sql, *args, **kwargs)

        monkeypatch.setattr(journal._db, "execute", execute_que_falla_en_el_segundo_update)

        with pytest.raises(sqlite3.OperationalError):
            await journal.fail_operation(entry_id, error="boom")

        monkeypatch.undo()
        assert llamadas["n"] == 1
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

        # La conexión sigue siendo usable tras el rollback.
        await journal.fail_operation(entry_id, error="ahora sí")
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"previa": True, "error": "ahora sí"}

    @pytest.mark.asyncio
    async def test_cancelacion_antes_del_commit_no_deja_estado_parcial(self, journal, monkeypatch):
        """``CancelledError`` hereda de ``BaseException``: ``except Exception`` no la ve.

        Sin el manejo explícito, la cancelación entre el ``UPDATE`` del estado y
        el ``commit`` soltaba el lock con la transacción abierta y el siguiente
        escritor de la misma conexión commiteaba el ``FAILED`` sin evidencia.
        """
        entry_id = await self._entrada(journal, metadata={"previa": True})
        original = journal._db.execute

        def execute_cancelado_en_el_segundo_update(sql, *args, **kwargs):
            if sql.strip().startswith("UPDATE journal_entries SET metadata"):
                raise asyncio.CancelledError
            return original(sql, *args, **kwargs)

        monkeypatch.setattr(journal._db, "execute", execute_cancelado_en_el_segundo_update)

        with pytest.raises(asyncio.CancelledError):
            await journal.fail_operation(entry_id, error="boom")

        monkeypatch.undo()
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

        # La conexión no quedó con una transacción abierta a medias.
        await journal.fail_operation(entry_id, error="ahora sí")
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"previa": True, "error": "ahora sí"}

    @pytest.mark.asyncio
    async def test_rollback_fallido_conserva_evidencia_y_no_reporta_exito(self, journal, monkeypatch, caplog):
        """Invariante de ``app/db/AGENTS.md``: un rollback fallido deja rastro.

        Si el rollback también falla, se propaga el error ORIGINAL —que es lo que
        el caller necesita— y el del rollback se registra en vez de tragarse.
        """
        entry_id = await self._entrada(journal, metadata={"previa": True})
        original_execute = journal._db.execute

        def execute_que_falla(sql, *args, **kwargs):
            if sql.strip().startswith("UPDATE journal_entries SET metadata"):
                raise sqlite3.OperationalError("fallo de la operación")
            return original_execute(sql, *args, **kwargs)

        async def rollback_que_tambien_falla():
            raise sqlite3.OperationalError("fallo del rollback")

        monkeypatch.setattr(journal._db, "execute", execute_que_falla)
        monkeypatch.setattr(journal._db, "rollback", rollback_que_tambien_falla)

        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(sqlite3.OperationalError, match="fallo de la operación"),
        ):
            await journal.fail_operation(entry_id, error="boom")

        monkeypatch.undo()
        assert any("rollback de fail_operation falló" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cancelacion_externa_no_suelta_el_boundary_hasta_terminar_el_rollback(self, journal, monkeypatch):
        """Cancelación REAL: ``task.cancel()`` desde afuera, con el rollback bloqueado.

        No alcanza con que el rollback "quede en curso": si ``fail_operation``
        abandona el ``async with self._lock`` con la transacción todavía abierta,
        el siguiente escritor de la misma conexión commitea el estado parcial. Este
        test retiene el rollback con un ``Event`` y exige que la corrutina **siga
        adentro** del boundary mientras tanto.
        """
        entry_id = await self._entrada(journal, metadata={"previa": True})
        original_execute = journal._db.execute
        original_rollback = journal._db.rollback

        suspendido = asyncio.Event()  # se alcanzó el punto posterior al UPDATE de estado
        rollback_iniciado = asyncio.Event()
        permitir_rollback = asyncio.Event()  # lo libera el test
        rollback_terminado = asyncio.Event()

        def execute_que_se_suspende(sql, *args, **kwargs):
            if sql.strip().startswith("UPDATE journal_entries SET metadata"):

                async def suspender():
                    suspendido.set()
                    await asyncio.Event().wait()  # queda acá hasta que la cancelen
                    raise AssertionError("inalcanzable")

                return suspender()
            return original_execute(sql, *args, **kwargs)

        async def rollback_retenido():
            rollback_iniciado.set()
            await permitir_rollback.wait()
            await original_rollback()
            rollback_terminado.set()

        monkeypatch.setattr(journal._db, "execute", execute_que_se_suspende)
        monkeypatch.setattr(journal._db, "rollback", rollback_retenido)

        tarea = asyncio.create_task(journal.fail_operation(entry_id, error="boom"))
        await asyncio.wait_for(suspendido.wait(), timeout=5)
        assert journal._lock.locked(), "el boundary debería estar tomado"

        tarea.cancel()
        await asyncio.wait_for(rollback_iniciado.wait(), timeout=5)

        # Segunda cancelación, YA con el rollback en vuelo. Es la que discrimina:
        # `task.cancel()` entrega `CancelledError` una sola vez, en el await donde la
        # corrutina estaba suspendida, y ese primer golpe lo absorbe el `except`. Un
        # `await asyncio.shield(rollback)` suelto sobrevive a eso, así que sin este
        # segundo cancel el test pasaría también con la implementación defectuosa
        # (verificado por mutación). Con el shield suelto, este cancel lo hace
        # abandonar el boundary con el rollback retenido; con el bucle, sigue esperando.
        tarea.cancel()

        # Con el rollback retenido, la corrutina NO puede haber salido del boundary.
        for _ in range(20):
            await asyncio.sleep(0)
        assert not tarea.done(), "soltó la cancelación antes de terminar el rollback"
        assert journal._lock.locked(), "soltó el lock con la transacción abierta"
        assert not rollback_terminado.is_set()

        permitir_rollback.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea

        assert rollback_terminado.is_set(), "el rollback debía terminar antes de propagar"
        assert not journal._lock.locked()

        monkeypatch.undo()
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

        # La conexión sigue usable tras la cancelación.
        await journal.fail_operation(entry_id, error="ahora sí")
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"previa": True, "error": "ahora sí"}

    @pytest.mark.asyncio
    async def test_cancelacion_durante_el_rollback_no_se_pierde_con_fallo_original_de_db(self, journal, monkeypatch):
        """El otro eje de la cancelación: el fallo original NO es la cancelación.

        El test de arriba cubre el caso en que la propia cancelación es el fallo
        que entra al boundary, y ahí el ``raise`` pelado la re-propaga sola. Acá el
        fallo original es un ``OperationalError`` real y la cancelación llega
        **después**, con el rollback ya en vuelo: el bucle la absorbe para no
        abandonar la transacción a medias, y si no la devuelve, la task termina con
        ``cancelled() == False``. Quien canceló creería que canceló algo que en
        realidad siguió su curso.

        El contrato exigido es que las dos cosas sobrevivan: la cancelación como
        excepción propagada, y el fallo original como su causa.
        """
        entry_id = await self._entrada(journal, metadata={"previa": True})
        original_execute = journal._db.execute
        original_rollback = journal._db.rollback

        fallo = sqlite3.OperationalError("disk I/O error simulado")
        rollback_iniciado = asyncio.Event()
        permitir_rollback = asyncio.Event()  # lo libera el test
        rollback_terminado = asyncio.Event()

        def execute_que_falla(sql, *args, **kwargs):
            if sql.strip().startswith("UPDATE journal_entries SET metadata"):

                async def explotar():
                    raise fallo

                return explotar()
            return original_execute(sql, *args, **kwargs)

        async def rollback_retenido():
            rollback_iniciado.set()
            await permitir_rollback.wait()
            await original_rollback()
            rollback_terminado.set()

        monkeypatch.setattr(journal._db, "execute", execute_que_falla)
        monkeypatch.setattr(journal._db, "rollback", rollback_retenido)

        tarea = asyncio.create_task(journal.fail_operation(entry_id, error="boom"))
        await asyncio.wait_for(rollback_iniciado.wait(), timeout=5)

        # Cancelación externa REAL con el rollback ya en vuelo. A diferencia del test
        # anterior, acá alcanza con una: la corrutina no está esperando el UPDATE,
        # está esperando el rollback, así que este cancel golpea directamente el
        # `await asyncio.shield(...)` del bucle.
        tarea.cancel()

        try:
            for _ in range(20):
                await asyncio.sleep(0)
            assert not tarea.done(), "abandonó el boundary con el rollback retenido"
            assert journal._lock.locked(), "soltó el lock con la transacción abierta"
        finally:
            permitir_rollback.set()

        with pytest.raises(asyncio.CancelledError) as excinfo:
            await tarea

        assert rollback_terminado.is_set(), "el rollback debía terminar antes de propagar"
        assert tarea.cancelled(), "la cancelación externa se perdió: la task no quedó cancelada"
        assert excinfo.value.__cause__ is fallo, "el fallo original desapareció de la cadena"
        assert not journal._lock.locked()

        # Sin transacción parcial: ni el estado ni la metadata se movieron.
        monkeypatch.undo()
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

        # Y la conexión sigue usable.
        await journal.fail_operation(entry_id, error="ahora sí")
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"previa": True, "error": "ahora sí"}

    @pytest.mark.parametrize("no_finito", [float("nan"), float("inf"), float("-inf")], ids=repr)
    @pytest.mark.asyncio
    async def test_no_finito_no_puede_escribir_json_invalido(self, journal, no_finito):
        """``json.dumps`` emite ``NaN``/``Infinity``, que no son JSON válido.

        Sin ``allow_nan=False`` la columna quedaba con ``json_valid() == 0`` y
        ``json_extract`` devolvía ``NULL`` en silencio: evidencia perdida
        disfrazada de dato presente. La columna tiene que terminar válida — pero
        eso se consigue **descartando** la metadata auxiliar, no abortando el
        reporte del fallo (ver `TestFailOperationMetadataArgumentoInvalida`).
        """
        entry_id = await self._entrada(journal, metadata={"previa": True})

        await journal.fail_operation(entry_id, error="boom", metadata={"ratio": no_finito})

        async with journal._db.execute(
            "SELECT json_valid(metadata) FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            (valido,) = await cursor.fetchone()
        assert valido == 1

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"previa": True, "error": "boom"}

    @pytest.mark.parametrize(
        "invalida",
        [
            pytest.param({"ratio": float("nan")}, id="nan"),
            pytest.param({"ratio": float("inf")}, id="inf"),
            pytest.param({"ratio": float("-inf")}, id="-inf"),
            pytest.param({"obj": object()}, id="no_serializable"),
        ],
    )
    @pytest.mark.asyncio
    async def test_begin_operation_sigue_siendo_estricto(self, journal, invalida):
        """El hermano NO degrada, y la asimetría es deliberada.

        ``begin_operation`` está *creando* una operación: ante metadata inválida
        lo correcto es no crearla. ``fail_operation`` está *reportando un fallo
        que ya ocurrió*: ahí abortar perdería el error real. Este test congela
        que la política estricta sigue viva de este lado, para que la
        degradación del otro no se propague por copia.
        """
        tx_id = await journal.begin_transaction(description="tx estricta", mod_id=None)

        with pytest.raises((ValueError, TypeError)):
            await journal.begin_operation(
                agent_id="agente_evidencia",
                operation_type=OperationType.FILE_MODIFY,
                target_path="/test/path/mod.esp",
                transaction_id=tx_id,
                metadata=invalida,
            )

    # -- 12. colisión con la clave reservada ----------------------------------

    @pytest.mark.asyncio
    async def test_metadata_no_puede_secuestrar_la_clave_error(self, journal):
        """``error`` es canónico del argumento, no de ``metadata``."""
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="canónico", metadata={"error": "impostor"})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"error": "canónico"}

    @pytest.mark.asyncio
    async def test_sin_error_la_metadata_si_puede_traer_su_clave_error(self, journal):
        """Sin argumento canónico no hay nada que proteger: el dato del caller vale."""
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, metadata={"error": "del caller"})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"error": "del caller"}


class TestFailOperationMetadataLegacyCorrupta:
    """Una fila legacy corrupta no puede impedir que se registre un fallo nuevo.

    Regresión de la fusión en Python: al leer la metadata previa y hacerle
    ``.update()``, cuatro formas de fila corrupta —``[]``, ``"texto"``, JSON
    truncado y ``{"ratio": NaN}``— hacían estallar ``fail_operation`` con
    ``AttributeError``/``JSONDecodeError``/``ValueError`` **después** del UPDATE
    del estado, así que el rollback dejaba la operación en ``started``: el fallo
    real nunca quedaba registrado, y encima la excepción que veía el caller no
    era la suya.

    El contrato que fijan estos tests distingue dos cosas que se parecen y no lo
    son: **dato legacy roto** (se descarta, se avisa, la operación queda FAILED)
    contra **input nuevo inválido** (se rechaza fail-fast, la fila no se toca).
    """

    #: (blob crudo en la columna, motivo esperado en el WARNING)
    CORRUPTAS = [
        pytest.param("[]", "non_object_json", id="lista_json"),
        pytest.param('"texto"', "non_object_json", id="string_json"),
        pytest.param("42", "non_object_json", id="numero_json"),
        pytest.param("texto plano", "invalid_json", id="texto_plano"),
        pytest.param('{"a":', "invalid_json", id="json_truncado"),
        pytest.param('{"ratio": NaN}', "non_serializable_json", id="nan_legacy"),
    ]

    @staticmethod
    async def _entrada_con_metadata_cruda(journal, crudo) -> int:
        """Crea una operación STARTED y le inyecta ``crudo`` en la columna.

        Por SQL directo a propósito: ``begin_operation`` serializa con
        ``allow_nan=False`` y no puede producir estas filas. Son filas escritas
        por versiones anteriores o por corrupción, que es exactamente el caso que
        se está cubriendo.
        """
        tx_id = await journal.begin_transaction(description="tx legacy", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="agente_legacy",
            operation_type=OperationType.FILE_MODIFY,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
        )
        await journal._db.execute(
            "UPDATE journal_entries SET metadata = ? WHERE id = ?",
            (crudo, entry_id),
        )
        await journal._db.commit()
        return entry_id

    @staticmethod
    async def _fila_cruda(journal, entry_id):
        """Lee estado y metadata por SQL, sin pasar por ``get_operation_by_id``.

        Hace falta acá porque ``_row_to_entry`` hace ``json.loads`` sin guarda:
        leer una fila con metadata corrupta por la API pública explota. Es un
        defecto del camino de LECTURA, previo a este PR y fuera de su alcance —
        estos tests cubren el de ESCRITURA y no pueden depender de él.
        """
        async with journal._db.execute(
            "SELECT status, metadata FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            return await cursor.fetchone()

    # -- el fallo real gana sobre el defecto legacy ---------------------------

    @pytest.mark.parametrize(("crudo", "motivo"), CORRUPTAS)
    @pytest.mark.asyncio
    async def test_metadata_previa_corrupta_no_impide_registrar_el_fallo(self, journal, crudo, motivo):
        entry_id = await self._entrada_con_metadata_cruda(journal, crudo)

        await journal.fail_operation(entry_id, error="fallo real")

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}

    @pytest.mark.parametrize(("crudo", "motivo"), CORRUPTAS)
    @pytest.mark.asyncio
    async def test_metadata_previa_corrupta_no_propaga_su_excepcion(self, journal, crudo, motivo):
        """Ni ``JSONDecodeError``, ni ``AttributeError``, ni ``ValueError``."""
        entry_id = await self._entrada_con_metadata_cruda(journal, crudo)

        await journal.fail_operation(entry_id, error="fallo real", metadata={"exit_code": 3})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"exit_code": 3, "error": "fallo real"}

    @pytest.mark.parametrize(("crudo", "motivo"), CORRUPTAS)
    @pytest.mark.asyncio
    async def test_la_columna_queda_con_json_valido_tras_descartar(self, journal, crudo, motivo):
        """El descarte no puede dejar la fila igual de rota que estaba."""
        entry_id = await self._entrada_con_metadata_cruda(journal, crudo)

        await journal.fail_operation(entry_id, error="fallo real")

        async with journal._db.execute(
            "SELECT json_valid(metadata), json_extract(metadata, '$.error') FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            valido, mensaje = await cursor.fetchone()
        assert valido == 1
        assert mensaje == "fallo real"

    # -- el descarte se avisa, y el aviso no filtra el blob -------------------

    @pytest.mark.parametrize(("crudo", "motivo"), CORRUPTAS)
    @pytest.mark.asyncio
    async def test_el_descarte_emite_warning_con_diagnostico_seguro(self, journal, caplog, crudo, motivo):
        entry_id = await self._entrada_con_metadata_cruda(journal, crudo)

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="fallo real")

        avisos = [r for r in caplog.records if getattr(r, "event", None) == "journal_metadata_previa_descartada"]
        assert len(avisos) == 1, "el descarte de metadata previa no puede ser silencioso"
        aviso = avisos[0]
        assert aviso.entry_id == entry_id
        assert aviso.motivo == motivo
        assert aviso.metadata_previa_bytes == len(crudo.encode("utf-8"))
        assert aviso.metadata_previa_sha256 == hashlib.sha256(crudo.encode("utf-8")).hexdigest()

    @pytest.mark.asyncio
    async def test_el_warning_no_incluye_el_blob_descartado(self, journal, caplog):
        """El blob puede traer rutas, perfiles de MO2 o nombres de mod."""
        secreto = r'{"path": "C:\\Users\\facundo\\Documents\\My Games\\Skyrim", '
        entry_id = await self._entrada_con_metadata_cruda(journal, secreto)

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="fallo real")

        (aviso,) = [r for r in caplog.records if getattr(r, "event", None) == "journal_metadata_previa_descartada"]
        rendido = aviso.getMessage() + repr(aviso.__dict__)
        assert "facundo" not in rendido
        assert "Skyrim" not in rendido
        assert secreto not in rendido

    # -- control: la metadata previa sana sigue fusionándose ------------------

    @pytest.mark.asyncio
    async def test_control_metadata_previa_valida_se_fusiona_y_no_avisa(self, journal, caplog):
        entry_id = await self._entrada_con_metadata_cruda(journal, '{"kind": "ok"}')

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="fallo real")

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"kind": "ok", "error": "fallo real"}
        assert not [r for r in caplog.records if getattr(r, "event", None) == "journal_metadata_previa_descartada"]

    # -- las dos fuentes corruptas a la vez -----------------------------------

    @pytest.mark.parametrize(("crudo", "motivo"), CORRUPTAS)
    @pytest.mark.asyncio
    async def test_previa_corrupta_y_argumento_invalido_igual_registran_el_fallo(self, journal, crudo, motivo):
        """Las dos fuentes de metadata rotas a la vez: sobrevive el error canónico.

        Se descartan las dos, la operación queda ``FAILED`` y la columna termina
        con JSON válido — nunca con el blob corrupto que tenía antes.
        """
        entry_id = await self._entrada_con_metadata_cruda(journal, crudo)

        await journal.fail_operation(entry_id, error="fallo real", metadata={"ratio": float("nan")})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}

        async with journal._db.execute(
            "SELECT json_valid(metadata) FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            (valido,) = await cursor.fetchone()
        assert valido == 1


class TestFailOperationMetadataArgumentoInvalida:
    """``fail_operation`` es un failure-reporting boundary, no un validador de entrada.

    Política revisada: la metadata auxiliar que el caller no puede representar
    como JSON estricto **se descarta con un warning**, no hace fallar el reporte.
    Antes se propagaba el ``ValueError``/``TypeError``, con la consecuencia de que
    una operación realmente fallida quedaba en ``STARTED`` y su error canónico
    desaparecía del journal: el reporte del fallo se convertía en el fallo.

    La prioridad es ``FAILED`` + error primario **por encima de** conservar
    metadata auxiliar inválida. La asimetría con ``begin_operation`` —que sigue
    siendo estricto— es deliberada y está congelada por
    ``test_begin_operation_sigue_siendo_estricto``.
    """

    INVALIDAS = [
        pytest.param({"ratio": float("nan")}, "non_finite_number", id="nan"),
        pytest.param({"ratio": float("inf")}, "non_finite_number", id="inf"),
        pytest.param({"ratio": float("-inf")}, "non_finite_number", id="-inf"),
        pytest.param({"obj": object()}, "non_serializable_value", id="objeto"),
        pytest.param({"conjunto": {1, 2}}, "non_serializable_value", id="set"),
        pytest.param({"exc": ValueError("secreto")}, "non_serializable_value", id="excepcion"),
        pytest.param({"ruta": pathlib.Path("/home/usuario/mods")}, "non_serializable_value", id="path"),
    ]

    @staticmethod
    async def _entrada(journal, *, metadata=None) -> int:
        tx_id = await journal.begin_transaction(description="tx argumento", mod_id=None)
        return await journal.begin_operation(
            agent_id="agente_argumento",
            operation_type=OperationType.FILE_MODIFY,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
            metadata=metadata,
        )

    # -- el fallo se registra igual -------------------------------------------

    @pytest.mark.parametrize(("invalida", "motivo"), INVALIDAS)
    @pytest.mark.asyncio
    async def test_no_propaga_y_deja_la_operacion_failed(self, journal, invalida, motivo):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="fallo real", metadata=invalida)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}

    @pytest.mark.parametrize(("invalida", "motivo"), INVALIDAS)
    @pytest.mark.asyncio
    async def test_la_metadata_previa_valida_se_conserva(self, journal, invalida, motivo):
        """Descartar la auxiliar inválida no puede llevarse puesta la previa buena."""
        entry_id = await self._entrada(journal, metadata={"kind": "ok", "paths": ["a"]})

        await journal.fail_operation(entry_id, error="fallo real", metadata=invalida)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"kind": "ok", "paths": ["a"], "error": "fallo real"}

    @pytest.mark.parametrize(("invalida", "motivo"), INVALIDAS)
    @pytest.mark.asyncio
    async def test_sin_error_igual_marca_failed_y_no_inventa_la_clave(self, journal, invalida, motivo):
        """``error=""`` sigue sin crear ``{"error": ""}``, pero la fila queda FAILED."""
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="", metadata=invalida)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata is None

    @pytest.mark.parametrize(("invalida", "motivo"), INVALIDAS)
    @pytest.mark.asyncio
    async def test_la_columna_nunca_queda_con_json_invalido(self, journal, invalida, motivo):
        entry_id = await self._entrada(journal, metadata={"previa": True})

        await journal.fail_operation(entry_id, error="fallo real", metadata=invalida)

        async with journal._db.execute(
            "SELECT json_valid(metadata) FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            (valido,) = await cursor.fetchone()
        assert valido == 1

    # -- el descarte se avisa, y el aviso no filtra nada -----------------------

    @pytest.mark.parametrize(("invalida", "motivo"), INVALIDAS)
    @pytest.mark.asyncio
    async def test_el_descarte_emite_warning_con_motivo_estable(self, journal, caplog, invalida, motivo):
        entry_id = await self._entrada(journal)

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="fallo real", metadata=invalida)

        avisos = [r for r in caplog.records if getattr(r, "event", None) == "journal_metadata_argumento_descartada"]
        assert len(avisos) == 1, "descartar la metadata del caller no puede ser silencioso"
        assert avisos[0].entry_id == entry_id
        assert avisos[0].motivo == motivo

    @pytest.mark.asyncio
    async def test_el_warning_no_filtra_el_contenido_descartado(self, journal, caplog):
        """El dict puede traer rutas, nombres de mod o el texto de una excepción."""
        entry_id = await self._entrada(journal)
        # Claves y valores centinela: cadenas que no pueden aparecer por casualidad
        # en un LogRecord (``exc_info``/``exc_text`` ya contienen "exc", por eso no
        # sirve un nombre de clave genérico para esta comprobación).
        invalida = {
            "clave_centinela_ruta": pathlib.Path(r"C:/Users/facundo/Documents/My Games/Skyrim"),
            "clave_centinela_exc": ValueError("token-secreto-en-el-mensaje"),
        }

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="fallo real", metadata=invalida)

        (aviso,) = [r for r in caplog.records if getattr(r, "event", None) == "journal_metadata_argumento_descartada"]
        rendido = aviso.getMessage() + repr(aviso.__dict__)
        for prohibido in (
            "facundo",
            "Skyrim",
            "My Games",
            "token-secreto-en-el-mensaje",
            "clave_centinela_ruta",
            "clave_centinela_exc",
        ):
            assert prohibido not in rendido, f"el warning filtró {prohibido!r}"

    @pytest.mark.asyncio
    async def test_metadata_valida_no_avisa_y_se_fusiona_igual_que_antes(self, journal, caplog):
        """Control: el camino sano no cambió."""
        entry_id = await self._entrada(journal, metadata={"kind": "ok"})

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="boom", metadata={"exit_code": 3, "anidado": {"a": [1, 2]}})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"kind": "ok", "exit_code": 3, "anidado": {"a": [1, 2]}, "error": "boom"}
        assert not [r for r in caplog.records if getattr(r, "event", None) == "journal_metadata_argumento_descartada"]

    # -- la degradación NO puede tragarse errores reales de DB -----------------

    @pytest.mark.asyncio
    async def test_un_error_de_db_real_sigue_propagando_y_revirtiendo(self, journal, monkeypatch):
        """La degradación cubre la representación de la metadata, nada más.

        Si esto se hubiera implementado como un ``except Exception`` alrededor de
        todo, un fallo de SQLite quedaría enmascarado y la fila se reportaría
        ``FAILED`` sin haberse escrito. Este test lo impide.
        """
        entry_id = await self._entrada(journal, metadata={"previa": True})
        original = journal._db.execute

        def execute_que_falla(sql, *args, **kwargs):
            if sql.strip().startswith("UPDATE journal_entries SET metadata"):
                raise sqlite3.OperationalError("disk I/O error simulado")
            return original(sql, *args, **kwargs)

        monkeypatch.setattr(journal._db, "execute", execute_que_falla)

        with pytest.raises(sqlite3.OperationalError):
            await journal.fail_operation(entry_id, error="boom", metadata={"ratio": float("nan")})

        monkeypatch.undo()
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}


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


class TestFailOperationBoundaryEndurecido:
    """Segunda ronda de revisión: el boundary no puede morir por su propio payload.

    Los tres defectos que cierran estos tests comparten una forma: algo que
    ``fail_operation`` manipula para *reportar* un fallo terminaba impidiendo
    que el fallo se registrara, o dejaba la conexión en un estado del que otro
    escritor se contagiaba.
    """

    @staticmethod
    def _lifecycle() -> DatabaseLifecycleManager:
        return DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )

    @staticmethod
    async def _entrada(journal, *, metadata=None) -> int:
        tx_id = await journal.begin_transaction(description="tx endurecido", mod_id=None)
        return await journal.begin_operation(
            agent_id="agente_endurecido",
            operation_type=OperationType.FILE_MODIFY,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
            metadata=metadata,
        )

    # -- C3: un rollback fallido no puede contagiar al siguiente escritor -----

    @pytest.mark.asyncio
    async def test_rollback_fallido_pone_la_conexion_en_cuarentena(self, tmp_path):
        """Si el rollback falla, la conexión sucia NO vuelve al pool.

        Sin cuarentena, la transacción queda abierta sobre la conexión
        compartida y el siguiente escritor commitea el ``FAILED`` parcial que
        este boundary quería descartar — exactamente el estado que promete
        impedir.
        """
        lifecycle = self._lifecycle()
        db_path = tmp_path / "cuarentena.db"
        try:
            j1 = OperationJournal(db_path, lifecycle=lifecycle)
            await j1.open()
            j2 = OperationJournal(db_path, lifecycle=lifecycle)
            await j2.open()
            entry_id = await self._entrada(j1)

            conexion = j1._db
            original_execute = conexion.execute

            def execute_que_falla(sql, *args, **kwargs):
                if sql.strip().startswith("UPDATE journal_entries SET metadata"):

                    async def explotar():
                        raise sqlite3.OperationalError("fallo de la operación")

                    return explotar()
                return original_execute(sql, *args, **kwargs)

            async def rollback_que_tambien_falla():
                raise sqlite3.OperationalError("fallo del rollback")

            conexion.execute = execute_que_falla  # type: ignore[method-assign]
            conexion.rollback = rollback_que_tambien_falla  # type: ignore[method-assign]

            with pytest.raises(sqlite3.OperationalError, match="fallo de la operación"):
                await j1.fail_operation(entry_id, error="boom")

            # La conexión salió del pool y j1 la soltó.
            assert j1._db is None, "el journal siguió apuntando a la conexión sucia"
            assert lifecycle._connections.get(str(db_path.resolve())) is not conexion, (
                "la conexión sucia seguía registrada en el lifecycle"
            )

            # El siguiente escritor obtiene una conexión LIMPIA y no puede
            # confirmar el FAILED parcial.
            await j2.close()
            j3 = OperationJournal(db_path, lifecycle=lifecycle)
            await j3.open()
            assert j3._db is not conexion
            await j3.begin_transaction(description="del siguiente escritor", mod_id=None)

            entry = await j3.get_operation_by_id(entry_id)
            assert entry.status == OperationStatus.STARTED, "el FAILED parcial fue commiteado por un escritor posterior"
            await j1.close()
            await j3.close()
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_rollback_exitoso_no_pone_la_conexion_en_cuarentena(self, journal, monkeypatch):
        """La cuarentena es para el rollback FALLIDO; el exitoso no la dispara."""
        entry_id = await self._entrada(journal, metadata={"previa": True})
        conexion = journal._db
        original = journal._db.execute

        def execute_que_falla(sql, *args, **kwargs):
            if sql.strip().startswith("UPDATE journal_entries SET metadata"):
                raise sqlite3.OperationalError("fallo de la operación")
            return original(sql, *args, **kwargs)

        monkeypatch.setattr(journal._db, "execute", execute_que_falla)
        with pytest.raises(sqlite3.OperationalError):
            await journal.fail_operation(entry_id, error="boom")
        monkeypatch.undo()

        assert journal._db is conexion, "el rollback exitoso no debe descartar la conexión"
        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

    # -- Q3: el payload canónico no puede hundir el reporte -------------------

    @pytest.mark.asyncio
    async def test_un_error_no_str_igual_registra_el_fallo(self, journal):
        """``error=exc`` en vez de ``error=str(exc)`` no puede dejar STARTED.

        mypy no cubre este módulo, así que nada ataja esa confusión del caller.
        """
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error=ValueError("boom"))  # type: ignore[arg-type]

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "boom"}, "se perdió el mensaje del error"

    @pytest.mark.asyncio
    async def test_un_error_cuyo_str_falla_cae_a_un_centinela(self, journal):
        entry_id = await self._entrada(journal)

        class SinStr:
            def __str__(self):
                raise RuntimeError("ni siquiera str() funciona")

        await journal.fail_operation(entry_id, error=SinStr())  # type: ignore[arg-type]

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "<error no representable>"}

    @pytest.mark.asyncio
    async def test_un_error_no_serializable_igual_registra_el_fallo(self, journal):
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error=object())  # type: ignore[arg-type]

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata is not None
        assert "error" in entry.metadata

    @pytest.mark.asyncio
    async def test_un_error_str_se_persiste_exactamente_igual_que_antes(self, journal):
        """El contrato de los callers válidos no cambia."""
        entry_id = await self._entrada(journal)
        exacto = 'texto con "comillas", \\backslash y ünïcode'

        await journal.fail_operation(entry_id, error=exacto)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.metadata == {"error": exacto}

    @pytest.mark.asyncio
    async def test_la_degradacion_del_error_avisa_sin_filtrar_el_valor(self, journal, caplog):
        entry_id = await self._entrada(journal)

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error=ValueError("secreto-en-el-mensaje"))  # type: ignore[arg-type]

        (aviso,) = [r for r in caplog.records if getattr(r, "event", None) == "journal_error_degradado"]
        assert aviso.entry_id == entry_id
        assert aviso.tipo_recibido == "ValueError"
        assert "secreto-en-el-mensaje" not in aviso.getMessage() + repr(aviso.__dict__)

    # -- C1: RecursionError en los dos clasificadores hermanos ----------------

    #: Profundidad que desborda la recursión del parser JSON. NO es un número
    #: mágico estable: py3.11 revienta a 1000, pero py3.12/3.13 aguantan hasta
    #: ~5000 (el límite de recursión en C se separó del de Python). Un test
    #: atado a 1000 pasaba en 3.12 sin ejercitar nada — falló en CI justamente
    #: así. Por eso los tests que la usan VERIFICAN primero que desborde en el
    #: runtime actual y se saltean si no, en vez de degradar en silencio.
    PROFUNDIDAD_DESBORDE = 20_000

    @staticmethod
    def _anidado(profundidad: int) -> list:
        """Construye la estructura por iteración: hacerlo recursivo ya reventaría."""
        valor: list = []
        for _ in range(profundidad):
            valor = [valor]
        return valor

    @classmethod
    def _texto_desbordante(cls) -> str:
        """JSON crudo demasiado profundo, o skip si este runtime lo tolera."""
        crudo = "[" * cls.PROFUNDIDAD_DESBORDE + "]" * cls.PROFUNDIDAD_DESBORDE
        try:
            json.loads(crudo)
        except RecursionError:
            return crudo
        pytest.skip(f"json.loads tolera {cls.PROFUNDIDAD_DESBORDE} niveles en este runtime")

    @classmethod
    def _valor_desbordante(cls) -> list:
        """Estructura Python demasiado profunda, o skip si este runtime la tolera."""
        valor = cls._anidado(cls.PROFUNDIDAD_DESBORDE)
        try:
            json.dumps(valor, allow_nan=False)
        except RecursionError:
            return valor
        pytest.skip(f"json.dumps tolera {cls.PROFUNDIDAD_DESBORDE} niveles en este runtime")

    @pytest.mark.asyncio
    async def test_metadata_legacy_demasiado_profunda_no_hunde_el_reporte(self, journal):
        """``json.loads`` levanta ``RecursionError``, que no es ValueError ni TypeError."""
        entry_id = await self._entrada(journal)
        hondo = self._texto_desbordante()
        await journal._db.execute("UPDATE journal_entries SET metadata = ? WHERE id = ?", (hondo, entry_id))
        await journal._db.commit()

        await journal.fail_operation(entry_id, error="fallo real")

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}

    @pytest.mark.asyncio
    async def test_metadata_argumento_demasiado_profunda_no_hunde_el_reporte(self, journal):
        """El hermano: ``json.dumps`` sobre una estructura profunda del caller."""
        entry_id = await self._entrada(journal)

        await journal.fail_operation(entry_id, error="fallo real", metadata={"hondo": self._valor_desbordante()})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}

    @pytest.mark.parametrize(
        ("caso", "motivo"),
        [
            pytest.param("legacy", "recursion_limit", id="previa"),
            pytest.param("argumento", "recursion_limit", id="argumento"),
        ],
    )
    @pytest.mark.asyncio
    async def test_el_descarte_por_profundidad_se_avisa(self, journal, caplog, caso, motivo):
        entry_id = await self._entrada(journal)
        evento = "journal_metadata_previa_descartada" if caso == "legacy" else "journal_metadata_argumento_descartada"
        if caso == "legacy":
            hondo = self._texto_desbordante()
            await journal._db.execute("UPDATE journal_entries SET metadata = ? WHERE id = ?", (hondo, entry_id))
            await journal._db.commit()
            kwargs = {}
        else:
            kwargs = {"metadata": {"hondo": self._valor_desbordante()}}

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.db.journal"):
            await journal.fail_operation(entry_id, error="fallo real", **kwargs)

        avisos = [r for r in caplog.records if getattr(r, "event", None) == evento]
        assert len(avisos) == 1
        assert avisos[0].motivo == motivo

    # -- Q2: la fila que este PR repara queda públicamente legible ------------

    @pytest.mark.parametrize(
        "crudo",
        ["[]", '"texto"', '{"a":', '{"ratio": NaN}', "texto plano"],
    )
    @pytest.mark.asyncio
    async def test_la_fila_reparada_es_legible_por_la_api_publica(self, journal, crudo):
        """Congela el contrato de SALIDA de este PR, no el del reader en general.

        Dos de estos blobs son ilegibles por ``get_operation_by_id`` ANTES de
        pasar por ``fail_operation`` —``_row_to_entry`` hace ``json.loads`` sin
        guarda, defecto preexistente del camino de lectura y fuera de alcance—.
        Lo que este test exige es que lo que ESTE PR escribe sí sea legible.
        """
        entry_id = await self._entrada(journal)
        await journal._db.execute("UPDATE journal_entries SET metadata = ? WHERE id = ?", (crudo, entry_id))
        await journal._db.commit()

        await journal.fail_operation(entry_id, error="fallo real")

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}

    # -- Q1: SQLite serializa incluso entre conexiones distintas --------------

    @pytest.mark.asyncio
    async def test_dos_lifecycles_distintos_no_pierden_evidencia(self, tmp_path):
        """El ``UPDATE status`` previo toma la write transaction antes del SELECT.

        El read-modify-write (SELECT → merge → UPDATE) parece una ventana TOCTOU,
        pero ``fail_operation`` ejecuta antes un ``UPDATE`` que adquiere la
        transacción de escritura de SQLite: el segundo escritor queda bloqueado
        aunque tenga otra conexión y otro lock. Este test congela esa propiedad
        por si alguien reordena las sentencias.
        """
        lifecycle_a = self._lifecycle()
        lifecycle_b = self._lifecycle()
        db_path = tmp_path / "dos_managers.db"
        try:
            ja = OperationJournal(db_path, lifecycle=lifecycle_a)
            jb = OperationJournal(db_path, lifecycle=lifecycle_b)
            await ja.open()
            await jb.open()
            assert ja._db is not jb._db, "premisa: conexiones distintas"
            assert ja._lock is not jb._lock, "premisa: locks distintos"

            entry_id = await self._entrada(ja)
            await asyncio.gather(
                ja.fail_operation(entry_id, metadata={"a": 1}),
                jb.fail_operation(entry_id, metadata={"b": 2}),
            )

            entry = await ja.get_operation_by_id(entry_id)
            assert entry.metadata == {"a": 1, "b": 2}, "se perdió la evidencia de un escritor"
            await ja.close()
            await jb.close()
        finally:
            await lifecycle_a.shutdown_all()
            await lifecycle_b.shutdown_all()

    # -- C1 sin depender de la versión: se inyecta el RecursionError ----------
    #
    # Los tests de profundidad real se saltean si el runtime tolera 20 000
    # niveles, así que por sí solos no garantizan cobertura. Estos dos la
    # garantizan siempre: prueban el CONTRATO —el clasificador tolera
    # RecursionError— sin depender de cuántos frames consume el parser de C en
    # la versión de turno, que es justo el detalle que hizo pasar en falso al
    # test anterior en py3.12.

    @pytest.mark.asyncio
    async def test_recursion_error_en_la_metadata_previa_no_hunde_el_reporte(self, journal, monkeypatch):
        entry_id = await self._entrada(journal, metadata={"previa": True})
        original = json.loads

        def loads_que_desborda(s, *args, **kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(json, "loads", loads_que_desborda)
        try:
            await journal.fail_operation(entry_id, error="fallo real")
        finally:
            monkeypatch.setattr(json, "loads", original)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}

    @pytest.mark.asyncio
    async def test_recursion_error_en_la_metadata_del_caller_no_hunde_el_reporte(self, journal, monkeypatch):
        entry_id = await self._entrada(journal)
        original = json.dumps
        vistos: list[object] = []

        def dumps_que_desborda_solo_el_auxiliar(obj, *args, **kwargs):
            # Solo la PRIMERA serialización (la validación del argumento) desborda;
            # la de la fusión debe seguir funcionando para que la fila se escriba.
            vistos.append(obj)
            if len(vistos) == 1:
                raise RecursionError("maximum recursion depth exceeded")
            return original(obj, *args, **kwargs)

        monkeypatch.setattr(json, "dumps", dumps_que_desborda_solo_el_auxiliar)
        try:
            await journal.fail_operation(entry_id, error="fallo real", metadata={"hondo": [1]})
        finally:
            monkeypatch.setattr(json, "dumps", original)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.FAILED
        assert entry.metadata == {"error": "fallo real"}
