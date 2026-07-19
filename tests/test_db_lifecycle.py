"""Tests for DatabaseLifecycleManager — FASE 1.5.2 SQLite WAL hardening.

Validates:
- WAL mode activation and pragma application
- Orphaned WAL recovery on init
- Checkpoint (PASSIVE and TRUNCATE)
- Graceful shutdown eliminates WAL files
- Health check (healthy/warning/critical)
- Concurrent writers don't lock up
- Pragma verification on connect
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from pydantic import ValidationError

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
    DatabaseShutdownIncompleteError,
    WALHealth,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_dir(tmp_path: Path) -> Path:
    """Temporary directory for test databases."""
    return tmp_path


@pytest.fixture
def db_path(tmp_db_dir: Path) -> Path:
    """Single test database path."""
    return tmp_db_dir / "test_lifecycle.db"


@pytest.fixture
def lifecycle(db_path: Path) -> DatabaseLifecycleManager:
    """Default lifecycle manager with one database."""
    return DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )


# ---------------------------------------------------------------------------
# Initialization + Pragmas
# ---------------------------------------------------------------------------


class TestInit:
    @pytest.mark.asyncio
    async def test_init_creates_wal_mode(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)
        assert conn is not None

        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
            assert row[0] == "wal"

        await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_pragma_application_on_connect(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)

        # Verify all pragmas
        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
            assert row[0] == "wal"

        async with conn.execute("PRAGMA foreign_keys") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 1

        async with conn.execute("PRAGMA synchronous") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 1  # NORMAL = 1

        async with conn.execute("PRAGMA busy_timeout") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 5000

        async with conn.execute("PRAGMA temp_store") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 2  # MEMORY = 2

        await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_init_single_cierra_conexion_si_pragmas_fallan(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn = MagicMock(name="conexion_sin_registrar")
        conn.close = AsyncMock()
        error_pragmas = RuntimeError("fallo deliberado de pragmas")

        async def conectar(_path: str) -> MagicMock:
            return conn

        monkeypatch.setattr(aiosqlite, "connect", conectar)
        monkeypatch.setattr(
            manager,
            "_apply_pragmas",
            AsyncMock(side_effect=error_pragmas),
        )

        with pytest.raises(RuntimeError) as exc_info:
            await manager._init_single(db_path)

        assert exc_info.value is error_pragmas
        conn.close.assert_awaited_once_with()
        assert manager.managed_paths == []

    @pytest.mark.asyncio
    async def test_init_single_preserva_error_pragmas_si_close_falla(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn = MagicMock(name="conexion_con_close_fallido")
        conn.close = AsyncMock(side_effect=OSError("close deliberadamente fallido"))
        error_pragmas = RuntimeError("pragmas deliberadamente fallidos")

        async def conectar(_path: str) -> MagicMock:
            return conn

        monkeypatch.setattr(aiosqlite, "connect", conectar)
        monkeypatch.setattr(
            manager,
            "_apply_pragmas",
            AsyncMock(side_effect=error_pragmas),
        )

        with pytest.raises(RuntimeError) as exc_info:
            await manager._init_single(db_path)

        assert exc_info.value is error_pragmas
        conn.close.assert_awaited_once_with()
        assert "no se pudo cerrar la conexión no registrada" in caplog.text
        assert "close deliberadamente fallido" in caplog.text
        assert manager.managed_paths == []

    @pytest.mark.asyncio
    async def test_init_single_cancelado_espera_close_y_preserva_cancelacion_original(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        pragmas_iniciados = asyncio.Event()
        cierre_iniciado = asyncio.Event()
        liberar_cierre = asyncio.Event()

        conn = MagicMock(name="conexion_cancelada_sin_registrar")

        async def conectar(_path: str) -> MagicMock:
            return conn

        async def aplicar_pragmas_bloqueados(
            _conn: MagicMock,
            _path: str,
        ) -> None:
            pragmas_iniciados.set()
            await asyncio.Event().wait()

        async def cerrar_bloqueado() -> None:
            cierre_iniciado.set()
            await liberar_cierre.wait()

        conn.close = AsyncMock(side_effect=cerrar_bloqueado)
        monkeypatch.setattr(aiosqlite, "connect", conectar)
        monkeypatch.setattr(manager, "_apply_pragmas", aplicar_pragmas_bloqueados)

        task = asyncio.create_task(manager._init_single(db_path))
        await pragmas_iniciados.wait()
        task.cancel("cancelación inicial")

        try:
            await asyncio.wait_for(cierre_iniciado.wait(), timeout=0.5)
            task.cancel("cancelación repetida")
            await asyncio.sleep(0)
            assert not task.done()

            liberar_cierre.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await task
            assert exc_info.value.args == ("cancelación inicial",)
            conn.close.assert_awaited_once_with()
            assert manager.managed_paths == []
        finally:
            liberar_cierre.set()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Recovery from Orphaned WAL
# ---------------------------------------------------------------------------


class TestRecovery:
    @pytest.mark.asyncio
    async def test_recovery_from_orphan_wal(self, db_path: Path) -> None:
        """Simulate crash: write data, force-close without checkpoint, then recover."""
        # Step 1: Create DB and write data
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        await conn.execute("INSERT INTO test (val) VALUES ('before_crash')")
        await conn.commit()
        # DO NOT checkpoint — simulate crash by just closing
        await conn.close()

        # Step 2: Verify WAL file exists (orphaned)
        # WAL may or may not exist depending on SQLite's auto-checkpoint,
        # but the recovery logic should handle both cases gracefully

        # Step 3: Init with lifecycle manager (should recover)
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db_path],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        await lifecycle.init_all()

        # Step 4: Verify data is accessible
        conn = await lifecycle.get_connection(db_path)
        async with conn.execute("SELECT val FROM test") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "before_crash"

        await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_init_with_clean_state(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        """Init on a clean database should work without issues."""
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)
        assert conn is not None
        await lifecycle.shutdown_all()


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------


class TestCheckpoint:
    @pytest.mark.asyncio
    async def test_passive_checkpoint(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)

        # Write some data to generate WAL content
        await conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        await conn.execute("INSERT INTO test (val) VALUES ('checkpoint_test')")
        await conn.commit()

        # Execute PASSIVE checkpoint
        results = await lifecycle.checkpoint_all(mode="PASSIVE")
        assert str(db_path) in results
        assert "error" not in results[str(db_path)]

        await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_truncate_checkpoint(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)

        await conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        await conn.execute("INSERT INTO test (val) VALUES ('truncate_test')")
        await conn.commit()

        # Execute TRUNCATE checkpoint
        results = await lifecycle.checkpoint_all(mode="TRUNCATE")
        assert str(db_path) in results
        assert "error" not in results[str(db_path)]

        await lifecycle.shutdown_all()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_conserva_fallida_cierra_vecina_y_reintenta(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        error_cierre = RuntimeError("cierre deliberadamente fallido")
        conn_fallida = MagicMock(name="conexion_fallida")
        conn_fallida.close = AsyncMock(side_effect=[error_cierre, None])
        conn_vecina = MagicMock(name="conexion_vecina")
        conn_vecina.close = AsyncMock()
        manager._connections = {
            "fallida.db": conn_fallida,
            "vecina.db": conn_vecina,
        }
        monkeypatch.setattr(manager, "checkpoint_all", AsyncMock(return_value={}))

        with pytest.raises(RuntimeError) as exc_info:
            await manager.shutdown_all()

        assert exc_info.value is error_cierre
        assert manager._connections == {"fallida.db": conn_fallida}
        conn_fallida.close.assert_awaited_once_with()
        conn_vecina.close.assert_awaited_once_with()

        await manager.shutdown_all()

        assert manager.managed_paths == []
        assert conn_fallida.close.await_count == 2

    @pytest.mark.asyncio
    async def test_shutdown_reemplazado_falla_y_conserva_reemplazo_para_reintento(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn_original = MagicMock(name="conexion_original")
        conn_reemplazo = MagicMock(name="conexion_reemplazo")
        conn_reemplazo.close = AsyncMock()

        async def cerrar_y_reemplazar() -> None:
            manager._connections["compartida.db"] = conn_reemplazo

        conn_original.close = AsyncMock(side_effect=cerrar_y_reemplazar)
        manager._connections = {"compartida.db": conn_original}
        monkeypatch.setattr(manager, "checkpoint_all", AsyncMock(return_value={}))

        with pytest.raises(DatabaseShutdownIncompleteError) as exc_info:
            await manager.shutdown_all()

        assert exc_info.value.retained_paths == ("compartida.db",)
        assert manager._connections == {"compartida.db": conn_reemplazo}
        conn_reemplazo.close.assert_not_awaited()

        await manager.shutdown_all()
        assert manager.managed_paths == []

    @pytest.mark.asyncio
    async def test_shutdown_cancelado_conserva_conexiones_y_permite_reintento(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        cierre_iniciado = asyncio.Event()
        intentos_cierre = 0
        conn_actual = MagicMock(name="conexion_actual")
        conn_restante = MagicMock(name="conexion_restante")
        conn_restante.close = AsyncMock()

        async def cerrar_actual() -> None:
            nonlocal intentos_cierre
            intentos_cierre += 1
            if intentos_cierre == 1:
                cierre_iniciado.set()
                await asyncio.Event().wait()

        conn_actual.close = AsyncMock(side_effect=cerrar_actual)
        manager._connections = {
            "actual.db": conn_actual,
            "restante.db": conn_restante,
        }
        monkeypatch.setattr(manager, "checkpoint_all", AsyncMock(return_value={}))

        shutdown_task = asyncio.create_task(manager.shutdown_all())
        await cierre_iniciado.wait()
        shutdown_task.cancel("cancelacion deliberada")

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await shutdown_task

        assert exc_info.value.args == ("cancelacion deliberada",)
        assert manager._connections == {
            "actual.db": conn_actual,
            "restante.db": conn_restante,
        }
        conn_restante.close.assert_not_awaited()

        await manager.shutdown_all()

        assert manager.managed_paths == []
        assert intentos_cierre == 2
        conn_restante.close.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_shutdown_no_orphan_wal(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        """After shutdown, WAL and SHM files should be eliminated."""
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)

        await conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        await conn.commit()

        await lifecycle.shutdown_all()

        # Verify WAL/SHM files are gone
        wal_path = Path(str(db_path) + "-wal")
        shm_path = Path(str(db_path) + "-shm")
        assert not wal_path.exists(), f"WAL file still exists: {wal_path}"
        assert not shm_path.exists(), f"SHM file still exists: {shm_path}"

    @pytest.mark.asyncio
    async def test_shutdown_preserves_data(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        """Data written before shutdown must persist after reopening."""
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)

        await conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        await conn.execute("INSERT INTO test (val) VALUES ('persistent')")
        await conn.commit()

        await lifecycle.shutdown_all()

        # Reopen and verify
        conn2 = await aiosqlite.connect(str(db_path))
        async with conn2.execute("SELECT val FROM test") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "persistent"
        await conn2.close()


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        await lifecycle.init_all()
        await lifecycle.shutdown_all()

        # After clean shutdown, WAL should not exist
        health = await lifecycle.health_check()
        db_health = health[str(db_path)]
        assert isinstance(db_health, WALHealth)
        assert db_health.status == "healthy"
        assert db_health.wal_exists is False
        assert db_health.wal_size_bytes == 0

    @pytest.mark.asyncio
    async def test_health_check_with_wal(self, db_path: Path) -> None:
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db_path],
            config=DatabaseLifecycleConfig(
                enable_signal_handlers=False,
                wal_warning_threshold_bytes=1,  # Very low threshold for testing
            ),
        )
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)

        # Write data to generate WAL content
        await conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        await conn.execute("INSERT INTO test (val) VALUES ('x')")
        await conn.commit()

        health = await lifecycle.health_check()
        db_health = health[str(db_path)]
        # WAL should exist and may be warning/critical depending on size
        assert isinstance(db_health, WALHealth)

        await lifecycle.shutdown_all()


# ---------------------------------------------------------------------------
# Concurrent Writers
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_writers_no_lockup(self, db_path: Path) -> None:
        """10 concurrent writers for 5 seconds should not cause database lock errors."""
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db_path],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        await lifecycle.init_all()
        conn = await lifecycle.get_connection(db_path)

        await conn.execute("CREATE TABLE concurrent_test (id INTEGER PRIMARY KEY, val TEXT, thread_id TEXT)")
        await conn.commit()

        errors: list[str] = []
        success_count = 0
        lock = asyncio.Lock()

        async def writer(worker_id: int) -> None:
            nonlocal success_count
            try:
                for i in range(10):
                    await conn.execute(
                        "INSERT INTO concurrent_test (val, thread_id) VALUES (?, ?)",
                        (f"worker_{worker_id}_iter_{i}", str(worker_id)),
                    )
                    await conn.commit()
                    await asyncio.sleep(0.01)
                async with lock:
                    success_count += 1
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower():
                    errors.append(f"Worker {worker_id}: {e}")
                else:
                    raise
            except Exception:
                raise

        # Launch 10 concurrent writers
        tasks = [asyncio.create_task(writer(i)) for i in range(10)]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Verify no "database is locked" errors
        assert len(errors) == 0, f"Database lock errors: {errors}"
        assert success_count == 10

        # Verify all writes persisted
        async with conn.execute("SELECT COUNT(*) FROM concurrent_test") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 100  # 10 workers × 10 iterations

        await lifecycle.shutdown_all()


# ---------------------------------------------------------------------------
# Config Immutability
# ---------------------------------------------------------------------------


class TestConfig:
    def test_config_is_frozen(self) -> None:
        cfg = DatabaseLifecycleConfig()
        with pytest.raises(ValidationError):
            cfg.wal_checkpoint_interval_seconds = 999  # type: ignore[misc]

    def test_config_strict_validation(self) -> None:
        with pytest.raises(ValidationError):
            DatabaseLifecycleConfig(busy_timeout_ms="not_int")  # type: ignore[arg-type]

    def test_wal_health_is_frozen(self) -> None:
        health = WALHealth(
            db_path="test.db",
            wal_exists=False,
            wal_size_bytes=0,
            status="healthy",
        )
        with pytest.raises(ValidationError):
            health.status = "critical"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# get_connection contract (M-01 PR A)
# ---------------------------------------------------------------------------


class TestGetConnection:
    """Validates the M-01 contract for DatabaseLifecycleManager.get_connection.

    The method must:
    - Return the same connection instance for the same path (singleton).
    - Apply hardened pragmas even when the path was not pre-registered.
    - Lazy-initialize unknown paths on first call.
    """

    @pytest.mark.asyncio
    async def test_get_connection_returns_singleton(self, db_path: Path) -> None:
        """Two calls with the same path return the same connection instance."""
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db_path],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        try:
            await lifecycle.init_all()
            conn1 = await lifecycle.get_connection(db_path)
            conn2 = await lifecycle.get_connection(db_path)
            assert conn1 is conn2
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_get_connection_returns_singleton_for_equivalent_paths(self, db_path: Path) -> None:
        """Equivalent paths must map to the same managed connection key."""
        alias_parent = db_path.parent / "alias_parent"
        alias_parent.mkdir()
        equivalent_path = alias_parent / ".." / db_path.name
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        try:
            conn1 = await lifecycle.get_connection(db_path)
            conn2 = await lifecycle.get_connection(equivalent_path)
            assert conn1 is conn2
            assert lifecycle.managed_paths == [str(db_path.resolve(strict=False))]
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_get_connection_concurrent_calls_share_single_connection(self, db_path: Path) -> None:
        """Concurrent first access must not create duplicate connections."""
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        connections: list[aiosqlite.Connection] = []
        try:
            connections = await asyncio.gather(
                *(lifecycle.get_connection(db_path) for _ in range(12)),
            )
            assert len({id(conn) for conn in connections}) == 1
        finally:
            await lifecycle.shutdown_all()
            seen: set[int] = set()
            for conn in connections:
                conn_id = id(conn)
                if conn_id in seen:
                    continue
                seen.add(conn_id)
                with contextlib.suppress(Exception):
                    await conn.close()

    @pytest.mark.asyncio
    async def test_get_connection_applies_pragmas(self, db_path: Path) -> None:
        """Lazy-init via get_connection applies WAL + busy_timeout + FK pragmas."""
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],  # empty — force lazy-init through get_connection
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        try:
            conn = await lifecycle.get_connection(db_path)

            async with conn.execute("PRAGMA journal_mode") as cur:
                row = await cur.fetchone()
                assert row[0] == "wal"

            async with conn.execute("PRAGMA busy_timeout") as cur:
                row = await cur.fetchone()
                assert row[0] == 5000

            async with conn.execute("PRAGMA foreign_keys") as cur:
                row = await cur.fetchone()
                assert row[0] == 1
        finally:
            await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_get_connection_lazy_init(self, db_path: Path) -> None:
        """Paths not declared in __init__ are initialized on demand."""
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        try:
            assert str(db_path) not in lifecycle.managed_paths
            conn = await lifecycle.get_connection(db_path)
            assert conn is not None
            assert str(db_path) in lifecycle.managed_paths
        finally:
            await lifecycle.shutdown_all()
