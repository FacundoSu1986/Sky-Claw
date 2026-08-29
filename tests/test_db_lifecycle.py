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
#   pragma devolvió 'wal' sin esperar en los estados observados (writer o
#   reader en vuelo). La carrera REPRODUCIDA corresponde a la conversión
#   misma; no se afirma que el pragma jamás devuelva BUSY por otra vía (el
#   retry de producción clasifica por código de error, no por origen).
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


async def test_cancelacion_durante_conversion_cierra_conexion_fisicamente(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F7: cancelación durante la conversión cierra la conexión físicamente.

    La cancelación atraviesa el reintento con su identidad (el loop sólo
    atrapa ``OperationalError`` de contención) y el cierre de la
    conexión corre según el contrato existente de ``_init_single``.

    Oracle físico: la conexión capturada por spy queda en estado
    ``_connection is None`` (``aiosqlite.Connection._conn`` levanta
    ``ValueError: no active connection`` cuando la conexión fue
    cerrada). Sin este oracle, basta con que la conexión nunca haya
    sido REGISTRADA para que ``manager._connections == {}`` se cumpla
    — la conexión puede haber quedado abierta en el limbo, sin
    ownership. El test de acá no acepta ese falso verde: la conexión
    capturada DEBE estar cerrada.

    El sleep del reintento se agranda vía monkeypatch para que la
    cancelación caiga DETERMINÍSTICAMENTE dentro del
    ``await asyncio.sleep`` del loop (la fase alternativa —el
    ``execute``— propaga igual, así que un cancel que cayera ahí no
    discriminaría una mutación que trague la cancelación del sleep).
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    monkeypatch.setattr(db_lifecycle, "_CONVERSION_WAL_RETRY_SLEEP_S", 0.5)
    _sembrar_db_en_modo_delete(db_path)

    # Spy sobre ``aiosqlite.connect`` que captura la conexión real.
    conn_creadas: list[aiosqlite.Connection] = []
    conectar_real = aiosqlite.connect

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        conn_creadas.append(conn)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)

    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=5000),
    )
    holder = _SostieneLockSQLite(db_path)
    holder.start()
    assert holder.adquirido.wait(5), "el holder real no consiguió su lock"
    try:
        tarea = asyncio.create_task(manager._init_single(db_path))
        # Esperar a que la conexión haya sido abierta.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if conn_creadas:
                break
        assert conn_creadas, "el spy debió capturar la conexión de init"
        # Cancelar en plena fase de sleep (ventana 0.5s lo permite).
        await asyncio.sleep(0.05)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea

        # ORACLE 1: la conexión capturada por spy está cerrada.
        # ``aiosqlite.Connection._conn`` levanta ``ValueError`` si la
        # conexión está cerrada; sin el close, la propiedad devuelve
        # el ``sqlite3.Connection`` interno y NO levanta.
        conn_capturada = conn_creadas[0]
        with pytest.raises(ValueError, match="no active connection"):
            _ = conn_capturada._conn  # type: ignore[attr-defined]
        # ORACLE 2: el registro del manager no contiene la conexión.
        assert manager._connections == {}, "la cancelación no debe dejar conexión huérfana registrada"
    finally:
        holder.liberar.set()
        holder.join(5)


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


def _sidecars_renombrados(db_path: Path) -> bool:
    """True si quedaron archivos ``-wal.corrupted.*`` o ``-shm.corrupted.*``."""
    raiz = str(db_path)
    candidatos = list(db_path.parent.glob(Path(raiz).name + "-wal.corrupted.*"))
    candidatos.extend(db_path.parent.glob(Path(raiz).name + "-shm.corrupted.*"))
    return any(p.exists() for p in candidatos)


class _RaiseAsyncCtx:
    """Awaitable+async-CM que sólo lanza al ser usado.

    ``aiosqlite.Connection.execute`` se invoca como
    ``await conn.execute(sql)`` (await del ``@asynccontextmanager``) y el
    ``_apply_pragmas`` lo usa como ``async with conn.execute(sql) as cur``
    indistintamente. Esta clase soporta las dos formas: ``__await__``
    lanza para el caso ``await``, y ``__aenter__`` lanza para
    ``async with``. La semántica es equivalente a que el ``execute``
    original haya raiseado.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __await__(self):  # type: ignore[no-untyped-def]
        async def _raise() -> object:
            raise self._exc

        return _raise().__await__()

    async def __aenter__(self) -> object:
        raise self._exc

    async def __aexit__(self, *args: object) -> bool:
        return False


async def test_busy_timeout_cero_ejecuta_un_intento(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``busy_timeout_ms=0`` debe ejecutar exactamente UN intento del pragma.

    Semántica de ``busy_timeout_ms=0``: fail-fast. La conversión se
    INTENTA una vez (SQLite real) y, si hay contención, falla de
    inmediato — sin esperar. NO es "no intentar": el bug pre-fix
    calculaba ``restante = 0 - elapsed = 0`` ANTES del primer execute
    y lanzaba un ``OperationalError`` sintético sin tocar SQLite.

    Oracle: el spy de ``execute`` cuenta las invocaciones de
    ``PRAGMA journal_mode=WAL``. Con ``busy_timeout_ms=0`` y SIN
    contención, la conversión debe PROGRESAR (un intento exitoso) y
    registrar la conexión.

    Red pre-fix: ``raise sqlite3.OperationalError("database is locked")``
    antes de ejecutar el pragma → el spy cuenta 0 intentos y
    ``_init_single`` falla aunque no haya contención.
    """

    intentos_wal: list[str] = []

    class _SpyCuentaWAL:
        """Delega execute al sqlite real, contando los pragmas WAL."""

        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql:
                intentos_wal.append(sql)
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    conectar_real = aiosqlite.connect

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        return _SpyCuentaWAL(conn)  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
    _sembrar_db_en_modo_delete(db_path)
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=0),
    )
    try:
        await manager._init_single(db_path)
        assert len(intentos_wal) == 1, (
            f"con busy_timeout_ms=0 debe ejecutarse exactamente UN intento del "
            f"pragma (fail-fast), se ejecutaron {len(intentos_wal)}: {intentos_wal}"
        )
        assert str(db_path.resolve()) in manager._connections, (
            "sin contención, el intento único debe progresar y registrar la conexión"
        )
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()


async def test_recovery_operational_error_no_busy_no_se_clasifica_contencion(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un ``OperationalError`` no-BUSY en recovery va a clase C (rename forense).

    La clase A actual atrapa TODO ``sqlite3.OperationalError`` como
    contención, pero un ``OperationalError`` de I/O, permisos o schema
    NO es contención. El fix debe clasificar dentro del except con
    ``_es_contencion_de_bloqueo()``: si no es contención, no debe
    propagar como si lo fuera — debe ir a la clase C (rename forense +
    log + return sin raise).

    Spy inyecta un ``OperationalError("disk I/O error")`` sin
    ``sqlite_errorcode`` poblado (el camino del fallback textual: el
    mensaje NO es "database is locked" ni "database table is locked",
    así que ``_es_contencion_de_bloqueo`` devuelve False).

    Red pre-fix: la clase A lo atrapa y propaga (raise) sin rename.
    """

    class _SpyIOError:
        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn
            self._disparado = False

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql and not self._disparado:
                self._disparado = True
                return _RaiseAsyncCtx(sqlite3.OperationalError("disk I/O error"))
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    conectar_real = aiosqlite.connect

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        return _SpyIOError(conn)  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
    _sembrar_db_en_modo_delete(db_path)
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    wal_path.write_bytes(b"orphan")
    shm_path.write_bytes(b"orphan")
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=200),
    )
    try:
        # Clase C traga el error y renombra — NO debe propagar.
        await manager._recover_orphaned_wal(db_path, wal_path, shm_path)
        # Post-fix: rename forense existe.
        assert _sidecars_renombrados(db_path), "un OperationalError no-BUSY debe ir a clase C (rename forense)"
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()


async def test_recovery_close_no_confirmado_conserva_ownership(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close de la conexión temporal NO confirmado → ownership retenido.

    Decisión explícita: si el cierre físico de la conexión temporal del
    recovery no se confirma, el path queda fail-closed (cuarentena) y la
    conexión se registra en ``_connections`` para que ``shutdown_all()``
    pueda reintentar el cierre. No se traga el fallo de ownership: el
    path nunca vuelve a servir sin que antes se cierre la conexión.

    Spy: la conexión capturada por ``aiosqlite.connect`` falla al
    ``close()``. El recovery debe propagar la contención (clase A) y la
    conexión debe quedar registrada + path cuarentenado.

    Red pre-fix: ``_cerrar_conexion_de_recovery`` sólo loguea
    ``critical`` y suelta la conexión — sin registro ni cuarentena, el
    path volvería a servir conexiones nuevas mientras la vieja queda
    viva sin dueño.
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    class _SpyCloseFalla:
        """Delega execute, pero el close físico falla deliberadamente."""

        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn
            self._cerrada = False

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql:
                err = sqlite3.OperationalError("database is locked")
                err.__dict__["sqlite_errorcode"] = sqlite3.SQLITE_BUSY
                err.__dict__["sqlite_errorname"] = "SQLITE_BUSY"
                return _RaiseAsyncCtx(err)
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        async def close(self) -> None:
            self._cerrada = True
            raise OSError("close deliberadamente fallido")

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    conectar_real = aiosqlite.connect
    spys: list[_SpyCloseFalla] = []
    conexiones_reales: list[aiosqlite.Connection] = []

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        conexiones_reales.append(conn)
        spy = _SpyCloseFalla(conn)  # type: ignore[arg-type]
        spys.append(spy)
        return spy  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
    monkeypatch.setattr(db_lifecycle, "_CONVERSION_WAL_RETRY_SLEEP_S", 0.001)
    _sembrar_db_en_modo_delete(db_path)
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    wal_path.write_bytes(b"orphan")
    shm_path.write_bytes(b"orphan")
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=50),
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await manager._recover_orphaned_wal(db_path, wal_path, shm_path)
        assert spys, "el spy debió capturar la conexión"
        assert spys[0]._cerrada, "el close de la conexión temporal se intentó"
        # Ownership retenido: la conexión quedó registrada para que
        # shutdown_all() pueda reintentar el cierre.
        path_str = str(db_path.resolve())
        assert path_str in manager._connections, (
            "un close no confirmado debe conservar la conexión en _connections para que shutdown_all() la reintente"
        )
        assert path_str in manager._quarantined_paths, (
            "un close no confirmado debe dejar el path fail-closed (cuarentena)"
        )
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()
        # Limpieza del thread worker: la conexión real (envuelta por el
        # spy cuyo close falla) nunca se cerró; la cerramos por fuera
        # para no dejar el proceso colgado.
        for real in conexiones_reales:
            with contextlib.suppress(Exception):
                await real.close()


async def test_recovery_exception_generica_va_a_clase_c_rename(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Exception`` genérica (no-sqlite) en recovery va a clase C (rename).

    Ancla el ORDEN de los excepts: ``sqlite3.OperationalError`` →
    ``Exception`` → ``BaseException``. Si ``BaseException`` queda antes
    de ``Exception`` (bug de reorden), una ``OSError`` genérica caería
    en clase B (propagar sin rename) en vez de clase C (rename forense).

    El spy inyecta una ``OSError`` en el execute del WAL pragma. Clase C
    debe tragar el error y renombrar.

    Red pre-fix (con orden invertido): ``OSError`` cae en
    ``except BaseException`` → PROPAGA sin rename.
    """

    class _SpyOSError:
        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn
            self._disparado = False

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if (
                isinstance(sql, str)
                and "PRAGMA journal_mode=WAL" in sql
                and not self._disparado
            ):
                self._disparado = True
                return _RaiseAsyncCtx(OSError("fallo de E/S deliberado"))
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    conectar_real = aiosqlite.connect
    conexiones_reales: list[aiosqlite.Connection] = []

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        conexiones_reales.append(conn)
        return _SpyOSError(conn)  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
    _sembrar_db_en_modo_delete(db_path)
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    wal_path.write_bytes(b"orphan")
    shm_path.write_bytes(b"orphan")
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=200),
    )
    try:
        # Clase C traga el error y renombra — NO debe propagar.
        await manager._recover_orphaned_wal(db_path, wal_path, shm_path)
        assert _sidecars_renombrados(db_path), (
            "una Exception genérica debe ir a clase C (rename forense)"
        )
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()
        for real in conexiones_reales:
            with contextlib.suppress(Exception):
                await real.close()


async def test_recovery_contencion_propagada_sin_renombrar_wal(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: contención legítima en recovery NO es corrupción.

    El ``_enter_wal_mode`` REAL corre (no se monkeypatchea): el spy
    intercepta SÓLO el ``execute`` de ``PRAGMA journal_mode=WAL`` e
    inyecta BUSY (``sqlite_errorcode=5``, la MISMA forma que el bypass
    real del busy handler en la promoción SHARED→EXCLUSIVE). Con
    ``busy_timeout_ms=50`` el loop de reintento agota la ventana en
    ~50ms y propaga el último error real. La clase A del recovery debe
    propagarlo SIN renombrar el WAL (CONTENCIÓN != CORRUPCIÓN).

    A diferencia del test previo, aquí no se sustituye ``_enter_wal_mode``:
    la decisión de retry y deadline la toma el código de producción real;
    el spy sólo modela la fuente de BUSY que un holder real dispararía.

    Red pre-fix: la clase A renombra el WAL como evidencia forense.
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    class _SpyBUSYReal:
        """Inyecta BUSY (code 5) en el execute del WAL pragma, delega el resto."""

        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn
            self._busy_error: sqlite3.OperationalError | None = None
            err = sqlite3.OperationalError("database is locked")
            err.__dict__["sqlite_errorcode"] = sqlite3.SQLITE_BUSY
            err.__dict__["sqlite_errorname"] = "SQLITE_BUSY"
            self._busy_error = err

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql:
                return _RaiseAsyncCtx(self._busy_error)
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    conectar_real = aiosqlite.connect

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        return _SpyBUSYReal(conn)  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
    # Sleep del retry chico para que la ventana se agote rápido.
    monkeypatch.setattr(db_lifecycle, "_CONVERSION_WAL_RETRY_SLEEP_S", 0.001)

    _sembrar_db_en_modo_delete(db_path)
    # Sidecars huérfanos: pre-condición para que el camino del recovery corra.
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    wal_path.write_bytes(b"orphan")
    shm_path.write_bytes(b"orphan")

    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=50),
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await manager._recover_orphaned_wal(
                db_path,
                wal_path,
                shm_path,
            )
        # INVARIANTE: no hay forensic rename. La contención no corrompió el archivo.
        assert not _sidecars_renombrados(db_path), (
            "contención NO es corrupción: el WAL original debe seguir existiendo sin rename forense"
        )
        assert wal_path.exists(), "el WAL huérfano original debe sobrevivir al recovery fallido"
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()


async def test_deadline_total_no_excede_ventana_dos_fases(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3: el tiempo total del reintento no puede exceder ``busy_timeout_ms``.

    La propiedad: ``configured window = total maximum waiting budget``. Cada
    intento de ``PRAGMA journal_mode=WAL`` debe ajustar su propio
    ``busy_timeout`` al RESTANTE de la ventana, no reusar el valor
    configurado completo.

    Escenario de dos fases (adversarial):
    - Fase 1: ``busy_timeout=300ms``, el primer intento recibe BUSY
      INMEDIATO vía bypass del handler (la misma forma que la race real,
      simulada por spy para controlar la ventana).
    - Fase 2: una vez consumido ~el 80% del budget, el spy deja pasar
      un execute. La nueva situación es la normal (SQLite usaría su
      busy handler si la espera supera la ventana restante).

    Pre-fix: el segundo ``conn.execute`` arranca con ``busy_timeout=300ms``
    (el configurado), no con el restante. Si la espera legítima del
    handler excede la ventana restante, el reintento total puede
    duplicar o triplicar el budget.

    Post-fix: el segundo ``execute`` corre con ``busy_timeout <= restante``.
    El total respeta el budget configurado (0.3s + 0.15s de tolerancia
    explícita por scheduling = assertion < 0.45s).
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    # Spy sobre la conexión aiosqlite del recovery: modela la
    # interacción con el busy handler REAL. La primera invocación
    # ``PRAGMA journal_mode=WAL`` "gasta" 240ms (espera legítima del
    # busy handler con un lock sostenido) y luego ``raise`` BUSY. Las
    # siguientes invocaciones pasan. El bug del over-budget se
    # reproduce: el segundo intento entra con el busy_timeout
    # configurado (300ms) en vez del restante (~60ms).
    class _AisqliteSpyDeadline:
        def __init__(self) -> None:
            self._disparos = 0
            self._inicio = time.monotonic()

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and sql.strip().startswith("PRAGMA busy_timeout="):
                # El cap por intento: extraemos el valor numérico del
                # busy_timeout que el ``_enter_wal_mode`` fija antes de
                # cada intento. Lo guardamos para que el spy lo use
                # como la espera del busy handler del siguiente
                # ``journal_mode=WAL``.
                try:
                    self._busy_timeout_ms = int(sql.split("=", 1)[1])
                except (IndexError, ValueError):
                    self._busy_timeout_ms = 0
                return self._conn.execute(sql, *args, **kwargs)  # type: ignore[attr-defined]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql:
                self._disparos += 1
                if self._disparos == 1:
                    # Primer intento: espera ~80% del busy_timeout
                    # configurado (240ms de 300ms) y luego BUSY. El
                    # resto (60ms) queda como ventana para el
                    # segundo intento. El spy simula la espera
                    # legítima del busy handler, que se agota SIN
                    # completar la conversión.
                    return _DelayedRaise(
                        0.24,
                        sqlite3.OperationalError("database is locked"),
                    )
                if self._disparos == 2:
                    # Segundo intento: el busy_timeout del intento
                    # debería ser el RESTANTE (~60ms) si el fix
                    # funciona. Sin el fix, sería 300ms. El spy
                    # espera lo que el busy_timeout diga (porque
                    # la espera del busy handler usa el busy_timeout
                    # del stmt). Si el fix funciona, el spy espera
                    # 60ms y completa la espera de la ventana
                    # restante — el total queda en ~300ms. Si el
                    # fix NO funciona (M-D1), el spy espera 300ms
                    # y empuja el total a ~600ms.
                    espera = (self._busy_timeout_ms or 300) / 1000.0
                    return _DelayedRaise(
                        espera,
                        sqlite3.OperationalError("database is locked"),
                    )
                # Tercer intento: pasa.
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[attr-defined]

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    class _DelayedRaise(_RaiseAsyncCtx):
        """Espera ``delay_s`` y luego ``raise`` al ser awaited o ``__aenter__``."""

        def __init__(self, delay_s: float, exc: BaseException) -> None:
            super().__init__(exc)
            self._delay = delay_s

        def __await__(self):  # type: ignore[no-untyped-def]
            async def _raise() -> object:
                await asyncio.sleep(self._delay)
                raise self._exc

            return _raise().__await__()

        async def __aenter__(self) -> object:
            await asyncio.sleep(self._delay)
            raise self._exc

    spy_factory_calls: list[_AisqliteSpyDeadline] = []
    conectar_real = aiosqlite.connect

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        spy = _AisqliteSpyDeadline()
        spy._conn = conn  # type: ignore[attr-defined]
        spy_factory_calls.append(spy)
        return spy  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
    # Sleep del retry bien chico para no contaminar la medición.
    monkeypatch.setattr(db_lifecycle, "_CONVERSION_WAL_RETRY_SLEEP_S", 0.001)

    _sembrar_db_en_modo_delete(db_path)
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=300),
    )
    try:
        inicio = time.monotonic()
        # El escenario es: el holder sostiene el lock durante ~el
        # budget completo. El primer intento espera 240ms, el segundo
        # entraría a la espera del handler con el busy_timeout
        # ORIGINAL (300ms), no con el restante. Bajo el bug, el total
        # puede ser ~540ms (primer intento) o más si la espera
        # legítima del segundo intento excede la ventana restante. El
        # fix acota cada intento al restante, así que el total <=
        # 300ms + tolerancia. Esta es la propiedad que el prompt
        # exige.
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await manager._init_single(db_path)
        transcurrido = time.monotonic() - inicio
        assert spy_factory_calls, "el spy debió capturar la conexión"
        assert spy_factory_calls[0]._disparos >= 1, "el primer execute debió ser BUSY (fase 1)"
        # El total debe estar acotado por la ventana configurada
        # (300ms) + tolerancia EXPLÍCITA de scheduling. El bug pre-fix
        # hace que el segundo intento consuma su propio busy_timeout
        # (300ms) y empuje el total a ~540ms (0.24 + 0.30); la
        # assertion en 450ms está entre ambos y discrimina.
        assert transcurrido < 0.45, (
            f"el tiempo total {transcurrido:.3f}s excede el budget + tolerancia "
            f"esperados (0.3s + 0.15s = 0.45s). El segundo intento reusó "
            f"busy_timeout completo en vez de limitarse al restante."
        )
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()


async def test_clasificacion_codigos_extendidos_exactos(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4: clasificación exacta de ``sqlite_errorcode``.

    El contrato: la decisión es código por código, NO por máscara
    ``(codigo & 0xFF)``. La lista de reintentables debe ser enumerada
    y verificada contra los códigos que ``PRAGMA journal_mode=WAL``
    puede devolver REALMENTE.

    Códigos verificados:
    - ``SQLITE_BUSY`` (5) ........... reintentable: hay otro owner activo.
    - ``SQLITE_BUSY_RECOVERY`` (261) . reintentable: WAL recovery en
      curso en otra conexión.
    - ``SQLITE_BUSY_SNAPSHOT`` (517)  NO reintentable: el snapshot de
      lectura no puede promoverse a escritura; en ``_enter_wal_mode``
      sobre una conexión fresh/autocommit es INALCANZABLE, así que
      fallar-closed / propagar es preferible a reintentar
      especulativamente.
    - ``SQLITE_BUSY_TIMEOUT`` (773) .. NO reintentable en este
      statement: la espera del busy handler agotó la ventana LOCAL del
      stmt, pero el outer budget ya la gobierna — reintentar duplica
      la espera sin cambiar el estado.
    - ``SQLITE_LOCKED`` (6) .......... reintentable: lock de tabla
      esperando que otra conexión lo libere.
    - ``SQLITE_LOCKED_SHAREDCACHE`` (262) reintentable: variante
      shared-cache.
    - Códigos extendidos no listados explícitamente: NO reintentables
      (fail-closed). Aceptarlos por la máscara ``& 0xFF`` es el bug.

    Red pre-fix: ``(codigo & 0xFF) in {5, 6}`` acepta TODOS los
    extendidos cuyo base sea 5 o 6, incluyendo los que el contrato
    rechaza explícitamente.
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    # Helper para construir OperationalError con código arbitrario.
    def error_con_codigo(codigo: int, mensaje: str) -> sqlite3.OperationalError:
        err = sqlite3.OperationalError(mensaje)
        # ``sqlite_errorcode`` es de sólo lectura en 3.11+, pero el
        # constructor ``sqlite3_errmsg`` y compañía no permiten
        # inyectarlo. Usamos un ``SimpleNamespace``-style fallback:
        # si la API lo permite, lo seteamos; si no, lo inyectamos por
        # ``__dict__``.
        try:
            err.sqlite_errorcode = codigo  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            err.__dict__["sqlite_errorcode"] = codigo
        return err

    casos_reintentables: list[tuple[int, str]] = [
        (sqlite3.SQLITE_BUSY, "database is locked"),
        (sqlite3.SQLITE_BUSY_RECOVERY, "database is locked"),
        (sqlite3.SQLITE_LOCKED, "database table is locked"),
        (sqlite3.SQLITE_LOCKED_SHAREDCACHE, "database table is locked"),
    ]
    casos_no_reintentables: list[tuple[int, str]] = [
        (sqlite3.SQLITE_BUSY_SNAPSHOT, "snapshot busy"),
        (sqlite3.SQLITE_BUSY_TIMEOUT, "busy timeout"),
        # Código no documentado para ``PRAGMA journal_mode=WAL``;
        # extendido no listado: fallar-closed.
        (0x12340005, "unknown extended busy"),
    ]
    # En 3.11 algunos nombres pueden no existir; verificamos y omitimos
    # los que no estén disponibles.
    disponibles: dict[str, int] = {
        "SQLITE_BUSY_SNAPSHOT": getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
        "SQLITE_BUSY_RECOVERY": getattr(sqlite3, "SQLITE_BUSY_RECOVERY", 261),
        "SQLITE_BUSY_TIMEOUT": getattr(sqlite3, "SQLITE_BUSY_TIMEOUT", 773),
        "SQLITE_LOCKED_SHAREDCACHE": getattr(sqlite3, "SQLITE_LOCKED_SHAREDCACHE", 262),
    }
    # Recalcular la lista de no-reintentables con los códigos reales del runtime.
    casos_no_reintentables = [
        (disponibles["SQLITE_BUSY_SNAPSHOT"], "snapshot busy"),
        (disponibles["SQLITE_BUSY_TIMEOUT"], "busy timeout"),
        (0x12340005, "unknown extended busy"),
    ]

    for codigo, mensaje in casos_reintentables:
        err = error_con_codigo(codigo, mensaje)
        # Inyectar el código vía el dict de la excepción (la API no lo
        # permite por atributo, pero ``__dict__`` lo acepta).
        err.__dict__["sqlite_errorcode"] = codigo
        assert db_lifecycle._es_contencion_de_bloqueo(err) is True, (
            f"código {codigo} ({mensaje}) debió ser reintentable"
        )

    for codigo, mensaje in casos_no_reintentables:
        err = error_con_codigo(codigo, mensaje)
        err.__dict__["sqlite_errorcode"] = codigo
        assert db_lifecycle._es_contencion_de_bloqueo(err) is False, (
            f"código {codigo} ({mensaje}) NO debió ser reintentable (el bug pre-fix lo aceptaba por máscara ``& 0xFF``)"
        )


async def test_clasificacion_no_acepta_operational_error_no_bloqueo() -> None:
    """F4 (no retry): un OperationalError de E/S o de schema NO es
    reintentable. La pre-fix aceptaba sólo por mensaje; la post-fix
    exige código.
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    casos = [
        sqlite3.OperationalError("no such table: foo"),
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.OperationalError("database is corrupt"),
    ]
    for err in casos:
        # Sin ``sqlite_errorcode``, el fallback por mensaje puede ser
        # "database is locked" — pero la fix debe preferir el código y
        # aceptar SÓLO los listados explícitamente. Aquí el mensaje
        # NO es "database is locked" / "database table is locked",
        # así que la clasificación por mensaje debe rechazarlo.
        assert db_lifecycle._es_contencion_de_bloqueo(err) is False, (
            f"error '{err}' no debió clasificarse como contención"
        )


async def test_recovery_cierra_conexion_temporal_al_cancelar(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: cancelar dentro de recovery cierra físicamente la conexión temporal.

    Se cancela la task que ejecuta ``_recover_orphaned_wal`` durante la
    fase de retry de ``_enter_wal_mode`` (después de conectar, antes del
    ``await conn.close()`` final). El ``except Exception`` actual NO
    cubre ``CancelledError`` (``BaseException``), así que la conexión
    temporal queda abierta.

    La inyección de BUSY en el primer intento de ``_enter_wal_mode`` (vía
    monkeypatch) fuerza el camino del retry: la cancelación aterriza en
    el ``await asyncio.sleep`` del loop. Sin la inyección, con sidecars
    presentes, ``PRAGMA journal_mode=WAL`` es un no-op y nunca entra al
    retry. El BUSY inyectado es la MISMA forma que el bypass real del
    busy handler (test existente ``test_conversion_wal_con_holder_permanente_falla_acotada``);
    acá verifica que la cancelación durante el retry cierra la conexión
    abierta por el recovery.

    Red pre-fix: la conexión temporal queda abierta tras la cancelación.
    ORACLE: el ``sqlite3.Connection`` interno (capturado por spy) rechaza
    operaciones cuando la conexión fue cerrada.
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    # Agrandar el sleep del retry del recovery para que el cancel sea determinista.
    monkeypatch.setattr(db_lifecycle, "_CONVERSION_WAL_RETRY_SLEEP_S", 0.5)
    # Forzar el camino del retry REAL: la primera ejecución de ``PRAGMA
    # journal_mode=WAL`` sobre el recovery con sidecars sería un no-op
    # (SQLite los ve y considera el archivo ya en WAL). Envolvemos la
    # conexión capturada para que su ``execute`` lance BUSY en el primer
    # intento y tenga éxito en los siguientes — la MISMA forma que el
    # bypass real del busy handler cuando el holder sostiene RESERVED.
    # El loop del ``_enter_wal_mode`` REAL entra entonces al sleep, y la
    # cancelación cae ahí (no en la línea de execute).
    conn_creadas: list[aiosqlite.Connection] = []
    conectar_real = aiosqlite.connect

    class _AisqliteExecuteSpy:
        """Envuelve una ``aiosqlite.Connection`` y lanza BUSY en el primer
        ``execute('PRAGMA journal_mode=WAL')``. Para cualquier otro
        ``execute`` delega.

        aiosqlite.Connection.execute es ``@asynccontextmanager``: la
        invocación devuelve un async context manager. Para intercalar el
        raise sin romper el contrato, devolvemos un async context manager
        que sólo levanta en ``__aenter__``.
        """

        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn
            self._disparado = False

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql and not self._disparado:
                self._disparado = True
                return _RaiseAsyncCtx(sqlite3.OperationalError("database is locked"))
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        spy = _AisqliteExecuteSpy(conn)  # type: ignore[arg-type]
        # El recovery espera un aiosqlite.Connection. Como sólo usa
        # ``execute``/``close`` sobre el objeto, ``_AisqliteExecuteSpy``
        # cumple el contrato (delegando el resto por ``__getattr__``).
        # Para que ``isinstance(conn, aiosqlite.Connection)`` no rompa en
        # otros tests, registramos el objeto original.
        conn_creadas.append(conn)
        return spy  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)

    _sembrar_db_en_modo_delete(db_path)
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    wal_path.write_bytes(b"orphan")
    shm_path.write_bytes(b"orphan")

    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=5000),
    )
    try:
        tarea = asyncio.create_task(manager._recover_orphaned_wal(db_path, wal_path, shm_path))
        # Esperar a que el recovery haya abierto la conexión y entrado al sleep.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if conn_creadas:
                break
        assert conn_creadas, "el recovery no llegó a abrir la conexión temporal"
        # Cancelar en plena fase de sleep (la ventana de 0.5s lo permite).
        await asyncio.sleep(0.05)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        # ORACLE físico: la conexión capturada por spy está cerrada. El
        # ``sqlite3.Connection`` interno rechaza operaciones cuando la
        # conexión fue cerrada.
        assert conn_creadas, "el spy debió capturar la conexión del recovery"
        conn_temp = conn_creadas[0]
        # ORACLE físico: la conexión aiosqlite capturada por spy debe estar
        # cerrada. El aiosqlite.Connection expone ``_connection is None``
        # cuando se cerró limpiamente. Sin el close, ``_connection`` sigue
        # siendo el sqlite3.Connection interno. El test del SELECT 1
        # directo sobre ``_conn`` no sirve: aiosqlite corre en un thread
        # separado y el sqlite3.Connection interno rechaza uso cross-thread
        # con ``ProgrammingError`` (falso positivo de "cerrado").
        with pytest.raises(ValueError, match="no active connection"):
            _ = conn_temp._conn  # type: ignore[attr-defined]
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()
