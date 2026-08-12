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
    async def test_fallo_de_serializacion_no_deja_estado_parcial(self, journal):
        """El UPDATE de estado ya corrió; el fallo posterior debe revertirlo."""
        entry_id = await self._entrada(journal, metadata={"previa": True})

        with pytest.raises(TypeError):
            await journal.fail_operation(entry_id, error="boom", metadata={"malo": object()})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

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
    async def test_nan_no_puede_escribir_json_invalido(self, journal, no_finito):
        """``json.dumps`` emite ``NaN``/``Infinity``, que no son JSON válido.

        Sin ``allow_nan=False`` la columna quedaba con ``json_valid() == 0`` y
        ``json_extract`` devolvía ``NULL`` en silencio: evidencia perdida
        disfrazada de dato presente.
        """
        entry_id = await self._entrada(journal, metadata={"previa": True})

        with pytest.raises(ValueError):
            await journal.fail_operation(entry_id, error="boom", metadata={"ratio": no_finito})

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

        async with journal._db.execute(
            "SELECT json_valid(metadata) FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            (valido,) = await cursor.fetchone()
        assert valido == 1

    @pytest.mark.asyncio
    async def test_begin_operation_tampoco_puede_escribir_json_invalido(self, journal):
        """El hermano: ``begin_operation`` es el otro escritor de la columna."""
        tx_id = await journal.begin_transaction(description="tx nan", mod_id=None)

        with pytest.raises(ValueError):
            await journal.begin_operation(
                agent_id="agente_evidencia",
                operation_type=OperationType.FILE_MODIFY,
                target_path="/test/path/mod.esp",
                transaction_id=tx_id,
                metadata={"ratio": float("nan")},
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

    # -- la otra mitad del contrato: input nuevo inválido NO es lo mismo ------

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
    async def test_metadata_nueva_invalida_deja_la_fila_intacta(self, journal, invalida):
        """Input nuevo inválido: excepción, estado anterior y metadata anteriores intactos."""
        entry_id = await self._entrada_con_metadata_cruda(journal, '{"previa": true}')

        with pytest.raises((ValueError, TypeError)):
            await journal.fail_operation(entry_id, error="boom", metadata=invalida)

        entry = await journal.get_operation_by_id(entry_id)
        assert entry.status == OperationStatus.STARTED
        assert entry.metadata == {"previa": True}

    @pytest.mark.parametrize(("crudo", "motivo"), CORRUPTAS)
    @pytest.mark.asyncio
    async def test_input_invalido_tampoco_toca_una_fila_ya_corrupta(self, journal, crudo, motivo):
        """La recuperación de la fila legacy no puede convertirse en excusa para escribirla.

        Si el caller manda evidencia irrepresentable, no hay nada válido con qué
        reemplazar el blob: la fila queda tal cual estaba, corrupta y ``started``.
        """
        entry_id = await self._entrada_con_metadata_cruda(journal, crudo)

        with pytest.raises(ValueError):
            await journal.fail_operation(entry_id, error="boom", metadata={"ratio": float("nan")})

        estado, metadata = await self._fila_cruda(journal, entry_id)
        assert estado == OperationStatus.STARTED.value
        assert metadata == crudo

    @pytest.mark.asyncio
    async def test_la_validacion_del_input_ocurre_antes_de_tocar_la_db(self, journal, monkeypatch):
        """Discrimina fail-fast de rollback: con la DB inalcanzable, igual falla por el input.

        Si la validación viviera dentro de la transacción, este test vería el
        centinela de ``_ensure_connected`` en vez del ``ValueError`` — y eso
        significaría que se abrió una transacción para después revertirla.
        """

        async def _explota():
            raise AssertionError("no se debe tocar la DB con metadata inválida")

        monkeypatch.setattr(journal, "_ensure_connected", _explota)

        with pytest.raises(ValueError):
            await journal.fail_operation(1, error="boom", metadata={"ratio": float("nan")})
