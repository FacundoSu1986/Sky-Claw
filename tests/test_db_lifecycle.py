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
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from pydantic import ValidationError

from sky_claw.app.core.db_lifecycle import (
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


async def test_sidecars_de_otro_owner_no_se_reportan_como_checkpoint_incompleto(tmp_path, caplog) -> None:
    """Con dos owners sobre el mismo archivo, el primero en cerrar no debe alarmar.

    Codex P2 en #371. SQLite solo borra ``-wal``/``-shm`` al cerrar la ÚLTIMA
    conexión, así que el manager que cierra primero SIEMPRE los ve presentes.
    Emitir ahí "This may indicate incomplete checkpoint" es un falso positivo
    permanente en todo apagado limpio de la GUI (la UI y el SupervisorAgent
    abren dos ``DatabaseAgent`` sobre el mismo ``sky_claw_state.db``), y
    envenena cualquier triage real de WAL.

    Si el checkpoint TRUNCATE reportó éxito, los datos ya son durables: los
    sidecars que quedan los explica el otro owner, no un checkpoint a medias.
    """
    import logging

    db_path = tmp_path / "compartida.db"

    primero = DatabaseLifecycleManager(db_paths=[db_path])
    await primero.init_all()
    await primero.get_connection(str(db_path))

    segundo = DatabaseLifecycleManager(db_paths=[db_path])
    await segundo.init_all()
    await segundo.get_connection(str(db_path))

    try:
        with caplog.at_level(logging.DEBUG, logger="SkyClaw.DatabaseLifecycle"):
            await primero.shutdown_all()

        # Los sidecars siguen ahí porque el segundo owner tiene el archivo abierto.
        assert (tmp_path / "compartida.db-wal").exists()

        avisos = [r for r in caplog.records if r.levelno >= logging.WARNING and "WAL/SHM" in r.getMessage()]
        assert avisos == [], (
            "el primer owner en cerrar reportó 'checkpoint incompleto' por sidecars "
            f"que explica el segundo owner: {[r.getMessage() for r in avisos]}"
        )
    finally:
        await segundo.shutdown_all()

    # Cerrada la última conexión, SQLite sí eliminó los sidecars.
    assert not (tmp_path / "compartida.db-wal").exists()
    assert not (tmp_path / "compartida.db-shm").exists()


async def test_sidecars_con_ruta_relativa_tambien_consultan_el_checkpoint(tmp_path, monkeypatch, caplog) -> None:
    """El caso de PRODUCCIÓN usa una ruta relativa: la clave debe canonizarse.

    CodeRabbit en #371. ``get_connection`` indexa ``_connections`` con
    ``str(path.resolve())`` y ``checkpoint_all`` hereda esa clave, pero
    ``_db_paths`` guarda la ruta tal cual se pasó. ``DatabaseAgent`` usa
    ``db_path="sky_claw_state.db"`` (relativa) por defecto, así que sin
    canonizar, el ``checkpoints.get(...)`` del paso 3 falla siempre y vuelve el
    falso warning justo en el único caso que importa.
    """
    import logging

    monkeypatch.chdir(tmp_path)
    relativa = Path("sky_claw_state.db")

    primero = DatabaseLifecycleManager(db_paths=[relativa])
    await primero.init_all()
    await primero.get_connection(str(relativa))

    segundo = DatabaseLifecycleManager(db_paths=[relativa])
    await segundo.init_all()
    await segundo.get_connection(str(relativa))

    try:
        with caplog.at_level(logging.DEBUG, logger="SkyClaw.DatabaseLifecycle"):
            await primero.shutdown_all()

        assert Path("sky_claw_state.db-wal").exists()
        avisos = [r for r in caplog.records if r.levelno >= logging.WARNING and "WAL/SHM" in r.getMessage()]
        assert avisos == [], (
            "con ruta relativa la clave del checkpoint no matcheó y volvió el falso "
            f"warning: {[r.getMessage() for r in avisos]}"
        )
    finally:
        await segundo.shutdown_all()


# ---------------------------------------------------------------------------
# Carrera de inicialización concurrente (H1 — CI run 33223088993)
# ---------------------------------------------------------------------------
#
# Mecanismo demostrado con SQLite real, no asumido:
#
#   `PRAGMA journal_mode=WAL` sobre un archivo que NO está en WAL promueve el
#   lock a EXCLUSIVE. Cuando DOS conexiones corren la conversión a la vez (dos
#   managers independientes sobre el mismo archivo: los locks Python por path
#   son por-INSTANCIA y SQLite es el único árbitro), SQLite devuelve
#   SQLITE_BUSY al perdedor INMEDIATAMENTE, sin invocar el busy handler — es la
#   evitación de deadlock documentada para la promoción de locks. Ningún
#   busy_timeout salva ese caso porque el handler no llega a correr: el
#   perdedor moría con "database is locked" en el primer intento
#   (`_apply_pragmas` → `journal_mode=WAL` → JournalConnectionError, CI
#   run 33223088993, windows-latest / py3.11).
#
#   Evidencia empírica (SQLite 3.45, Windows): con un holder en RESERVED, el
#   pragma falla en 0.00s CON busy_timeout=15000 instalado antes; con un holder
#   solo-lectura (SHARED) el handler SÍ espera; sobre un archivo ya-WAL el
#   pragma es un no-op instantáneo aunque haya writers activos. La única
#   ventana de carrera es la conversión misma.
#
# Los holders de acá abajo son conexiones SQLite REALES que sostienen
# exactamente el estado de lock requerido; no se simula ningún error.


class _SostieneLockSQLite(threading.Thread):
    """Conexión auxiliar real que sostiene el lock incompatible con la conversión.

    ``escritura=True``  → BEGIN IMMEDIATE + escritura: lock RESERVED. Es el
    estado que deja la conversión a WAL de otro manager en vuelo (promoción a
    EXCLUSIVE bloqueada por RESERVED) y el que dispara el BUSY instantáneo sin
    handler. ``escritura=False`` → BEGIN + SELECT: lock SHARED, contra el que
    el busy handler SÍ espera.
    """

    def __init__(self, db_file: Path, *, escritura: bool = True) -> None:
        super().__init__(daemon=True, name="holder-lock-sqlite")
        self._db_file = db_file
        self._escritura = escritura
        self.adquirido = threading.Event()
        self.liberar = threading.Event()

    def run(self) -> None:
        conn = sqlite3.connect(str(self._db_file), timeout=0.05)
        try:
            if self._escritura:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE TABLE IF NOT EXISTS holder_t (x)")
                conn.execute("INSERT INTO holder_t VALUES (1)")
            else:
                conn.execute("BEGIN")
                conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            self.adquirido.set()
            self.liberar.wait(30)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
                conn.close()


def _sembrar_db_en_modo_delete(db_file: Path) -> None:
    """DB legacy real: modo journal DELETE (default), cerrada y sin locks."""
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    finally:
        conn.close()


async def test_conversion_wal_con_holder_reservado_transitorio_triunfa(db_path: Path) -> None:
    """H1-A/B: la conversión a WAL espera al holder de la carrera y progresa.

    RED pre-fix: el perdedor de la conversión moría INSTANTÁNEAMENTE con
    "database is locked" (handler bypaseado), sin esperar la ventana
    configurada. Post-fix: reintenta dentro de la ventana del config y gana
    cuando el lock incompatible se libera.
    """
    _sembrar_db_en_modo_delete(db_path)
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=5000),
    )
    holder = _SostieneLockSQLite(db_path)
    holder.start()
    assert holder.adquirido.wait(5), "el holder real no consiguió su lock"

    async def liberar_tarde() -> None:
        await asyncio.sleep(0.3)
        holder.liberar.set()

    liberador = asyncio.create_task(liberar_tarde())
    try:
        await manager._init_single(db_path)

        path_str = str(db_path.resolve())
        assert path_str in manager._connections, "la conversión debe terminar registrando la conexión"
        conn = manager._connections[path_str]
        async with conn.execute("PRAGMA journal_mode") as cur:
            assert (await cur.fetchone())[0] == "wal"
    finally:
        liberador.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await liberador
        holder.liberar.set()
        holder.join(5)
        with contextlib.suppress(Exception):
            await manager.shutdown_all()


async def test_conversion_wal_con_holder_permanente_falla_acotada(db_path: Path) -> None:
    """H1-C: lock retenido más allá de la ventana → fallo limitado y diagnosticable.

    El fallo NO es el BUSY instantáneo del handler bypaseado: la conversión
    reintentó hasta agotar la ventana configurada (≥ busy_timeout_ms), y no
    queda conexión huérfana registrada. Nunca se espera infinitamente.
    """
    _sembrar_db_en_modo_delete(db_path)
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=300),
    )
    holder = _SostieneLockSQLite(db_path)
    holder.start()
    assert holder.adquirido.wait(5), "el holder real no consiguió su lock"
    try:
        inicio = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await manager._init_single(db_path)
        transcurrido = time.monotonic() - inicio

        assert transcurrido >= 0.25, (
            "el fallo fue instantáneo: la conversión no reintentó dentro de la "
            f"ventana configurada (transcurrido={transcurrido:.3f}s)"
        )
        assert transcurrido < 10, "el reintento no puede esperar indefinidamente"
        assert manager._connections == {}, "un init fallido no debe registrar conexiones"
    finally:
        holder.liberar.set()
        holder.join(5)


async def test_busy_timeout_configurado_gobierna_la_conversion(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1-B (knob): el busy_timeout de la CONFIG gobierna desde el primer statement.

    ``sqlite3.connect`` instala un busy handler de 5s por defecto; pre-fix,
    ``journal_mode=WAL`` corría ANTES del pragma ``busy_timeout`` y la ventana
    configurada no alcanzaba a gobernar el primer statement que espera por otro
    owner. El monkeypatch SOLO baja ese default de connect (0.1s) para que la
    discriminación sea rápida; los locks son SQLite real: un holder LECTOR
    (SHARED), contra el que el handler sí espera, liberado a los ~0.3s.
    Pre-fix: la conversión usaba el default (fallaba a los ~0.1s). Post-fix:
    el pragma configurado (15s) la cubre y progresa cuando el holder libera.
    """
    _sembrar_db_en_modo_delete(db_path)
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=15000),
    )

    conectar_real = aiosqlite.connect

    async def conectar_con_default_corto(database: object, **kwargs: object) -> aiosqlite.Connection:
        kwargs.setdefault("timeout", 0.1)
        return await conectar_real(database, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(aiosqlite, "connect", conectar_con_default_corto)

    holder = _SostieneLockSQLite(db_path, escritura=False)
    holder.start()
    assert holder.adquirido.wait(5), "el holder real no consiguió su lock"

    async def liberar_tarde() -> None:
        await asyncio.sleep(0.3)
        holder.liberar.set()

    liberador = asyncio.create_task(liberar_tarde())
    try:
        await manager._init_single(db_path)
        assert str(db_path.resolve()) in manager._connections
    finally:
        liberador.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await liberador
        holder.liberar.set()
        holder.join(5)
        with contextlib.suppress(Exception):
            await manager.shutdown_all()


async def test_paths_distintos_no_se_bloquean_entre_si(tmp_db_dir: Path) -> None:
    """H1-D: el holder sobre el path A no bloquea la inicialización de B."""
    path_a = tmp_db_dir / "path_a.db"
    path_b = tmp_db_dir / "path_b.db"
    _sembrar_db_en_modo_delete(path_a)
    _sembrar_db_en_modo_delete(path_b)
    manager = DatabaseLifecycleManager(
        db_paths=[path_a, path_b],
        config=DatabaseLifecycleConfig(busy_timeout_ms=500),
    )
    holder = _SostieneLockSQLite(path_a)
    holder.start()
    assert holder.adquirido.wait(5), "el holder real no consiguió su lock"
    try:
        # B inicializa mientras A sigue bloqueado por el holder real.
        await manager._init_single(path_b)
        assert str(path_b.resolve()) in manager._connections
        assert str(path_a.resolve()) not in manager._connections

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await manager._init_single(path_a)
        assert str(path_a.resolve()) not in manager._connections
    finally:
        holder.liberar.set()
        holder.join(5)
        with contextlib.suppress(Exception):
            await manager.shutdown_all()


async def test_cancelacion_durante_conversion_no_deja_conexion_huerfana(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1-F (foco en el reintento): cancelar a mitad del retry no registra conexión.

    La cancelación atraviesa el reintento con su identidad (el loop sólo atrapa
    OperationalError de contención) y el cierre de la conexión NO registrada
    corre según el contrato existente de ``_init_single``.

    El sleep del reintento se agranda vía monkeypatch para que la cancelación
    caiga DETERMINÍSTICAMENTE dentro del ``await asyncio.sleep`` del loop (la
    fase alternativa —el ``execute``— propaga igual, así que un cancel que
    cayera ahí no discriminaría una mutación que trague la cancelación del
    sleep).
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    monkeypatch.setattr(db_lifecycle, "_CONVERSION_WAL_RETRY_SLEEP_S", 0.5)
    _sembrar_db_en_modo_delete(db_path)
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=5000),
    )
    holder = _SostieneLockSQLite(db_path)
    holder.start()
    assert holder.adquirido.wait(5), "el holder real no consiguió su lock"
    try:
        tarea = asyncio.create_task(manager._init_single(db_path))
        await asyncio.sleep(0.1)  # dentro del primer sleep del reintento
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea

        assert manager._connections == {}, "la cancelación no debe dejar conexión huérfana registrada"
    finally:
        holder.liberar.set()
        holder.join(5)
