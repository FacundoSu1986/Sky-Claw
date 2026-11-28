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

import sky_claw.app.core.db_lifecycle as db_lifecycle_mod
from sky_claw.app.core.db_lifecycle import (
    DatabaseConnectionQuarantinedError,
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
    DatabaseShutdownIncompleteError,
    WALHealth,
    convertir_a_wal,
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
            # Holder: espera a que el test pida soltar — pero con un
            # tope de seguridad. 30s se queda corto si el scheduling del
            # runner demora la entrada a _init_single (Windows CI donde el
            # thread worker de aiosqlite compite con el arranque del async
            # task en runners pesados). 180s alcanza sin impactar la suite.
            self.liberar.wait(180)
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


async def test_init_no_publica_reemplazo_tras_close_no_confirmado_en_recovery(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interleaving adversarial: recovery clase C + close NO confirmado.

    Secuencia reproducida (determinista, SQLite real, sin sleeps):

        _init_single()
          → detecta orphan WAL/SHM
          → _recover_orphaned_wal()
          → clase C (OSError en el pragma WAL, error TRAGADO tras el rename)
          → _recovery_rename_forense()
          → _cerrar_conexion_de_recovery() con close() FALLIDO
          → registra la conexión temporal + cuarentena del path
          → el recovery RETORNA (la clase C no propaga)
          → _init_single continúa Step 2
          → abre una segunda conexión y PISA _connections[path]

    Ese pisón destruye el único owner registrado de una conexión temporal
    posiblemente viva: la cuarentena queda vacía de contenido, porque
    ``shutdown_all()`` ya no encuentra en el registro la conexión que debía
    reintentar cerrar.

    Propiedad bajo test: si el recovery retuvo ownership porque el cierre
    no se confirmó, NINGUNA ruta de inicialización puede abrir ni publicar
    una conexión de reemplazo. El init falla cerrado con
    ``DatabaseConnectionQuarantinedError`` (no con la OSError tragada por
    la clase C, ni con éxito silencioso).

    Red pre-fix: Step 2 corría, ``aiosqlite.connect`` se llamaba DOS veces
    y ``_connections[path]`` apuntaba a la segunda conexión — el test
    fallaba en los tres oráculos (excepción, identidad y conteo).
    """

    class _SpyCloseFallaClaseC:
        """Delega execute; OSError en el pragma WAL (clase C) y close fallido."""

        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn
            self._cerrada = False

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql:
                return _RaiseAsyncCtx(OSError("fallo de E/S deliberado"))
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        async def close(self) -> None:
            self._cerrada = True
            raise OSError("close deliberadamente fallido")

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    conectar_real = aiosqlite.connect
    spys: list[_SpyCloseFallaClaseC] = []
    conexiones_reales: list[aiosqlite.Connection] = []

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        conexiones_reales.append(conn)
        if not spys:
            # La conexión TEMPORAL del recovery: clase C + close fallido.
            spy = _SpyCloseFallaClaseC(conn)  # type: ignore[arg-type]
            spys.append(spy)
            return spy  # type: ignore[return-value]
        # Cualquier conexión posterior delega limpia: post-fix NO debe
        # existir (oracle de conteo); pre-fix es la del Step 2.
        return conn

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
    _sembrar_db_en_modo_delete(db_path)
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    wal_path.write_bytes(b"orphan")
    shm_path.write_bytes(b"orphan")
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(busy_timeout_ms=50),
    )
    path_str = str(db_path.resolve())
    try:
        # El init completo NO puede publicar una conexión de reemplazo:
        # falla cerrado con la excepción del lifecycle, no con la OSError
        # que la clase C tragó, ni con éxito silencioso.
        with pytest.raises(DatabaseConnectionQuarantinedError):
            await manager.get_connection(db_path)

        # ORACLE 1: exactamente UNA llamada a aiosqlite.connect — la del
        # recovery. Step 2 jamás abrió una segunda conexión.
        assert len(conexiones_reales) == 1, (
            f"el init abrió {len(conexiones_reales)} conexiones: la del recovery y una de reemplazo"
        )

        # ORACLE 2: ownership retenido — el registro sigue apuntando a la
        # MISMA conexión temporal del recovery (la cuyo close falló).
        assert manager._connections.get(path_str) is spys[0], (
            "_connections[path] debe seguir siendo la conexión temporal del recovery"
        )

        # ORACLE 3: el path quedó fail-closed.
        assert path_str in manager._quarantined_paths, "el close no confirmado debe dejar el path en cuarentena"
        assert spys[0]._cerrada, "el close de la conexión temporal se intentó"
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()
        # Limpieza del thread worker: la conexión real nunca cerró (el
        # close del spy falla deliberadamente); la cerramos por fuera.
        for real in conexiones_reales:
            with contextlib.suppress(Exception):
                await real.close()


async def test_recovery_close_no_confirmado_no_renombra_wal(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close no confirmado del recovery => WAL intacto, sin rename.

    Propiedad: si el cierre de la conexión temporal del recovery NO se
    confirma, el archivo sigue bajo una conexión posiblemente viva.
    Renombrar el WAL en ese estado rompe la integridad del archivo bajo
    un handler activo. Por eso ``_recovery_rename_forense`` sólo puede
    renombrar si el cierre está CONFIRMADO (``_cerrar_conexion_de_recovery``
    retorna True).

    Escenario determinista:
    1. Orphan WAL/SHM (post-crash).
    2. Clase C (``OSError`` en el execute del pragma WAL).
    3. El close de la temp connection está forzado a fallar.
    4. ``_cerrar_conexion_de_recovery`` registra y monta cuarentena.
    5. El rename NO debe correr: el WAL permanece en ``<path>-wal``.
    """

    class _SpyCloseFallaWal:
        """Clase C en execute + close deliberadamente fallido."""

        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn

        def execute(self, sql: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql:
                return _RaiseAsyncCtx(OSError("fallo de E/S deliberado en recovery"))
            return self._conn.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

        async def close(self) -> None:
            raise OSError("close deliberadamente fallido")

        def __getattr__(self, item: str) -> object:
            return getattr(self._conn, item)

    conectar_real = aiosqlite.connect
    conexiones_reales: list[aiosqlite.Connection] = []

    async def conectar_spy(database: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await conectar_real(database, **kwargs)  # type: ignore[arg-type]
        conexiones_reales.append(conn)
        return _SpyCloseFallaWal(conn)  # type: ignore[return-value]

    monkeypatch.setattr(aiosqlite, "connect", conectar_spy)
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
        # Clase C traga el error --- no debe renombrar el WAL porque
        # el close de la conexión temporal NO se confirmó.
        await manager._recover_orphaned_wal(db_path, wal_path, shm_path)
        assert wal_path.exists(), (
            "el close no se confirmó y el WAL fue renombrado igualmente: "
            "esto viola la integridad del archivo bajo una conexión potencialmente viva"
        )
        path_str = str(db_path.resolve())
        assert path_str in manager._quarantined_paths, "el path debe quedar en cuarentena"
        assert manager._connections.get(path_str) is not None, (
            "ownership debe quedar registrado para que shutdown_all() lo cierre"
        )
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown_all()
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
            if isinstance(sql, str) and "PRAGMA journal_mode=WAL" in sql and not self._disparado:
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
        assert _sidecars_renombrados(db_path), "una Exception genérica debe ir a clase C (rename forense)"
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
    El bug MA D1 (second attempt with the FULL configured busy_timeout)
    is caught deterministically by asserting on the NUMERIC value the spy
    captured (``_busy_timeout_ms``), not on wall clock — ``asyncio.sleep``
    bajo carga de CI (runner de Windows) puede distorsionar la medición
    sin afectar la lógica del cap.
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
            # Valores capturados para la aserción determinística:
            # el busy_timeout con que `_enter_wal_mode` ejecuta cada intento.
            self._segundo_intento_busy_timeout_ms: int | None = None

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
                    # Probabilidad determinística del bug M-D1: el segundo
                    # intento llega con el restante (~60ms), no con el
                    # valor completo (300ms). El spy captura ese valor.
                    self._segundo_intento_busy_timeout_ms = self._busy_timeout_ms
                    return _DelayedRaise(
                        (self._busy_timeout_ms or 300) / 1000.0,
                        sqlite3.OperationalError("database is locked"),
                    )
                # Intentos N>=3: el deadline debe haber disparado ya. Si por
                # scheduling el elapsed quedó aún bajo (CI de Windows), NO
                # dejamos pasar el reintento: re-lanzamos BUSY igual.
                # Prevente "DID NOT RAISE" por segundo intento que duró
                # menos que el presupuesto en algunos runners.
                return _DelayedRaise(
                    0.001,
                    sqlite3.OperationalError("database is locked"),
                )
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
        # Wall-clock NO es aserción de propiedad en CI: asyncio.Sleep bajo
        # carga de scheduling puede desviar la medición sin afectar al
        # cap de busy_timeout. La propiedad determinística: el busy_timeout
        # capturado por el spy para el segundo intento (escape: <
        # 300ms) no puede ser el valor configurado completo.

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await manager._init_single(db_path)

        assert spy_factory_calls, "el spy debió capturar la conexión."
        assert spy_factory_calls[0]._disparos >= 1, "el primer execute debió ser BUSY (fase 1)"

        segundo_intento_timeout = spy_factory_calls[0]._segundo_intento_busy_timeout_ms
        assert segundo_intento_timeout is not None, (
            "el segundo intento nunca corrió (bug o la flecha regresó al_SCHEDULER)"
        )
        # Primer intento consumió ~240ms del budget de 300ms (Fase 1). El
        # segundo intento DEBE ver ~60ms, no 300ms. El margen ancho (< 250)
        # cubre scheduling de CI sin dejar pasar M-D1: el bug deja el segundo
        # intento con 300ms exactos.
        assert segundo_intento_timeout < 250, (
            f"el segundo intento debió capar su busy_timeout al restante(~60ms), "
            f"pero se observó {segundo_intento_timeout}ms — señal de M-D1 (se le dio el budget completo)"
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


def test_clasificacion_fallback_textual_exige_igualdad_exacta() -> None:
    """F4: el fallback (sin ``sqlite_errorcode``) es IGUALDAD EXACTA contra
    los strings canónicos de SQLite, nunca substrings de "busy"/"locked".

    Un mensaje que CONTIENE "locked" sin SER el string canónico — detalle
    extra, mayúsculas, otra condición que lo menciona — no es contención:
    reintentarlo especulativamente podría enmascarar un fallo ajeno a la
    contención. La mutación M-E2 (fallback por substring) muere acá.
    """
    import sky_claw.app.core.db_lifecycle as db_lifecycle

    cerca_pero_no_contencion = [
        sqlite3.OperationalError("database is locked (tabla audits)"),
        sqlite3.OperationalError("database is LOCKED"),
        sqlite3.OperationalError("statement UNLOCKED unexpectedly"),
    ]
    for err in cerca_pero_no_contencion:
        assert db_lifecycle._es_contencion_de_bloqueo(err) is False, (
            f"el fallback no debe aceptar por substring: '{err}' no es un string canónico"
        )

    exactos = [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database table is locked"),
    ]
    for err in exactos:
        assert db_lifecycle._es_contencion_de_bloqueo(err) is True, (
            f"el fallback debe aceptar el string canónico exacto: '{err}'"
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


# ---------------------------------------------------------------------------
# Contrato explícito de ``convertir_a_wal`` (remediación PR #1028)
#
# Dobles deterministas: SQLite real se reserva para las recetas de contención
# en ``test_conversion_wal_standalone.py``. Acá se gobiernan la aritmética del
# deadline, la restauración y la propagación de errores con reloj controlado.
# ---------------------------------------------------------------------------


class _CursorDeContrato:
    def __init__(
        self,
        fila: tuple[str, ...] | None,
        error: BaseException | None = None,
        esperar: asyncio.Event | None = None,
        marcar: asyncio.Event | None = None,
        error_al_salir: BaseException | None = None,
    ) -> None:
        self._fila = fila
        self._error = error
        self._esperar = esperar
        self._marcar = marcar
        # Se lanza DESPUÉS de resolver la espera, no antes: permite modelar
        # "el await cedió el control y al reanudar falló".
        self._error_al_salir = error_al_salir

    async def _bloquear(self) -> None:
        if self._esperar is None:
            return
        if self._marcar is not None:
            self._marcar.set()
        await self._esperar.wait()

    def __await__(self):
        async def _esperar() -> _CursorDeContrato:
            await self._bloquear()
            if self._error_al_salir is not None:
                raise self._error_al_salir
            if self._error is not None:
                raise self._error
            return self

        return _esperar().__await__()

    async def __aenter__(self) -> _CursorDeContrato:
        await self._bloquear()
        if self._error_al_salir is not None:
            raise self._error_al_salir
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def fetchone(self) -> tuple[str, ...] | None:
        return self._fila


class _ConexionDeContrato:
    """Conexión falsa: registra SQL y fallos inyectados, sin tocar SQLite."""

    def __init__(
        self,
        *,
        fila: tuple[str, ...] | None = ("wal",),
        error_pragma: BaseException | None = None,
        error_restauracion: BaseException | None = None,
        falla_timeout_en: int | None = None,
        inicio_restauracion: asyncio.Event | None = None,
        liberar_restauracion: asyncio.Event | None = None,
        restauracion_en_vuelo: bool = False,
        restauracion_bloqueante_una_vez: bool = False,
        restauracion_cancelada_una_vez: bool = False,
    ) -> None:
        self.sql: list[str] = []
        self.intentos_wal = 0
        self.timeout_llamadas = 0
        self._fila = fila
        self._error_pragma = error_pragma
        self._error_restauracion = error_restauracion
        self._falla_timeout_en = falla_timeout_en
        self._inicio_restauracion = inicio_restauracion
        self._liberar_restauracion = liberar_restauracion
        self._restauracion_en_vuelo = restauracion_en_vuelo
        self._restauracion_bloqueante_una_vez = restauracion_bloqueante_una_vez
        self._restauracion_cancelada_una_vez = restauracion_cancelada_una_vez
        self._bloqueo_consumido = False

    def execute(self, sql: object, *_args: object, **_kwargs: object) -> _CursorDeContrato:
        sentencia = str(sql)
        self.sql.append(sentencia)
        if sentencia.startswith("PRAGMA busy_timeout="):
            self.timeout_llamadas += 1
            if self.timeout_llamadas == self._falla_timeout_en:
                return _CursorDeContrato(None, self._error_restauracion)
            if self._restauracion_bloqueante_una_vez and self.timeout_llamadas == 2 and not self._bloqueo_consumido:
                # La restauración se suspende UNA vez (el switch de cancelación
                # se entrega acá) y al reanudar RESUELVE bien: modela el caso
                # "cleanup exitoso, cancelación pendiente en el task".
                self._bloqueo_consumido = True
                return _CursorDeContrato(None, esperar=self._liberar_restauracion, marcar=self._inicio_restauracion)
            if self._restauracion_cancelada_una_vez and self.timeout_llamadas == 2 and not self._bloqueo_consumido:
                # La restauración se suspende UNA vez y al reanudar LANZA
                # CancelledError: modela el cleanup interrumpido por una
                # cancelación externa real (el switch se entrega en el await).
                self._bloqueo_consumido = True
                return _CursorDeContrato(
                    None,
                    esperar=self._liberar_restauracion,
                    marcar=self._inicio_restauracion,
                    error_al_salir=asyncio.CancelledError("cleanup interrumpido"),
                )
            if self._restauracion_en_vuelo and self.timeout_llamadas == 2:
                # El 2º busy_timeout ES la restauración: cede el control para
                # que un Task.cancel() real caiga DENTRO de ella.
                return _CursorDeContrato(None, esperar=self._liberar_restauracion, marcar=self._inicio_restauracion)
            if self.timeout_llamadas == 2 and self._inicio_restauracion is not None:
                self._inicio_restauracion.set()
                return _CursorDeContrato(None, esperar=self._liberar_restauracion)
        if sentencia == "PRAGMA journal_mode=WAL":
            self.intentos_wal += 1
            if self._error_pragma is not None:
                error = self._error_pragma
                self._error_pragma = None
                return _CursorDeContrato(None, error)
            return _CursorDeContrato(self._fila)
        return _CursorDeContrato(None)


class _RelojGuiado:
    def __init__(self, valores: list[float]) -> None:
        self.valores = valores
        self.llamadas = 0

    def __call__(self) -> float:
        valor = self.valores[min(self.llamadas, len(self.valores) - 1)]
        self.llamadas += 1
        return valor


class _DormirGuiado:
    def __init__(self) -> None:
        self.duraciones: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.duraciones.append(segundos)


def _instalar_reloj(monkeypatch: pytest.MonkeyPatch, valores: list[float]) -> _DormirGuiado:
    dormir = _DormirGuiado()
    monkeypatch.setattr(db_lifecycle_mod, "_monotono", _RelojGuiado(valores))
    monkeypatch.setattr(db_lifecycle_mod, "_dormir", dormir)
    return dormir


def _error_busy() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.__dict__["sqlite_errorcode"] = sqlite3.SQLITE_BUSY
    error.__dict__["sqlite_errorname"] = "SQLITE_BUSY"
    return error


def _cadena(error: BaseException) -> list[BaseException]:
    """Recorre ``__context__`` con tope duro: una cadena circular la corta acá."""
    vistos: list[BaseException] = []
    actual: BaseException | None = error
    while actual is not None and len(vistos) <= 16:
        vistos.append(actual)
        actual = actual.__context__
    return vistos


def _sin_ciclo(error: BaseException) -> bool:
    """True si ``__context__`` no vuelve a una instancia ya visitada."""
    vistos: set[int] = set()
    actual: BaseException | None = error
    while actual is not None:
        if id(actual) in vistos:
            return False
        vistos.add(id(actual))
        actual = actual.__context__
    return True


async def test_convertir_a_wal_restaura_busy_timeout_en_exito(monkeypatch: pytest.MonkeyPatch) -> None:
    """Éxito: la conexión se publica con el timeout CONFIGURADO, no con el cap por intento."""
    _instalar_reloj(monkeypatch, [0.0, 0.01])
    conn = _ConexionDeContrato()
    await convertir_a_wal(conn, busy_timeout_ms=100)
    assert conn.intentos_wal == 1
    assert conn.sql[-1] == "PRAGMA busy_timeout=100"


@pytest.mark.parametrize("modo", ["contencion", "no_reintentable", "cancelacion"])
async def test_convertir_a_wal_restauracion_en_toda_salida_fallida(monkeypatch: pytest.MonkeyPatch, modo: str) -> None:
    """Timeout agotado, error no reintentable y cancelación restauran y propagan EL original."""
    if modo == "contencion":
        original: BaseException = _error_busy()
        reloj = [0.0, 0.2]
    elif modo == "no_reintentable":
        original = sqlite3.OperationalError("disk I/O error")
        reloj = [0.0, 0.02]
    else:
        original = asyncio.CancelledError()
        reloj = [0.0, 0.02]
    _instalar_reloj(monkeypatch, reloj)
    conn = _ConexionDeContrato(error_pragma=original)
    with pytest.raises(type(original)) as capturado:
        await convertir_a_wal(conn, busy_timeout_ms=100)
    assert capturado.value is original
    assert conn.sql[-1] == "PRAGMA busy_timeout=100"


async def test_convertir_a_wal_fallo_de_restauracion_no_oculta_el_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si la restauración también falla, se propaga la excepción primaria."""
    original = sqlite3.OperationalError("disk I/O error")
    _instalar_reloj(monkeypatch, [0.0, 0.02])
    conn = _ConexionDeContrato(
        error_pragma=original,
        error_restauracion=sqlite3.OperationalError("restore fail"),
        falla_timeout_en=2,
    )
    with pytest.raises(sqlite3.OperationalError) as capturado:
        await convertir_a_wal(conn, busy_timeout_ms=100)
    assert capturado.value is original


async def test_convertir_a_wal_no_reintenta_despues_del_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """El presupuesto vencido DURANTE el sleep no habilita un intento extra (M04)."""
    # El deadline se agota DURANTE el sleep: la comprobación posterior al
    # error ve restante positivo, así que sólo el guard del loop impide el
    # segundo intento. (La primera lectura alimenta el deadline.)
    _instalar_reloj(monkeypatch, [0.0, 0.0, 0.008, 0.011, 0.011])
    conn = _ConexionDeContrato(error_pragma=_error_busy())
    with pytest.raises(sqlite3.OperationalError):
        await convertir_a_wal(conn, busy_timeout_ms=10)
    assert conn.intentos_wal == 1


async def test_convertir_a_wal_sleep_usa_restante_vigente(monkeypatch: pytest.MonkeyPatch) -> None:
    """El sleep se capita con el restante RECALCULADO, no con el de antes del pragma (M05)."""
    dormir = _instalar_reloj(monkeypatch, [0.0, 0.099, 0.099, 0.099])
    conn = _ConexionDeContrato(error_pragma=_error_busy())
    await convertir_a_wal(conn, busy_timeout_ms=100)
    assert dormir.duraciones == [pytest.approx(0.001)]


@pytest.mark.parametrize("fila", [("delete",), None])
async def test_convertir_a_wal_falla_cerrado_si_sqlite_no_devuelve_wal(
    monkeypatch: pytest.MonkeyPatch, fila: tuple[str, ...] | None
) -> None:
    """No basta con que ``execute`` no lance: se consume el cursor y se exige 'wal'."""
    _instalar_reloj(monkeypatch, [0.0, 0.01])
    conn = _ConexionDeContrato(fila=fila)
    with pytest.raises(sqlite3.OperationalError):
        await convertir_a_wal(conn, busy_timeout_ms=100)


@pytest.mark.parametrize("valor", [True, False, -1, 1.5, "100", 2**31])
async def test_convertir_a_wal_rechaza_timeout_invalido(monkeypatch: pytest.MonkeyPatch, valor: object) -> None:
    """bool/float/str/negativo/fuera de rango se rechazan ANTES de tocar la conexión."""
    _instalar_reloj(monkeypatch, [0.0])
    conn = _ConexionDeContrato()
    with pytest.raises((ValueError, TypeError)):
        await convertir_a_wal(conn, busy_timeout_ms=valor)  # type: ignore[arg-type]
    assert conn.sql == []


@pytest.mark.parametrize("valor", [True, 1.5, "5", -1, 2**31])
def test_config_rechaza_busy_timeout_invalido(valor: object) -> None:
    """El config usa el MISMO dominio que el helper (dominio único, no dos validaciones)."""
    with pytest.raises(ValidationError):
        DatabaseLifecycleConfig(busy_timeout_ms=valor)  # type: ignore[arg-type]


def test_codigos_de_contencion_congelados() -> None:
    """Cualquier ampliación a máscara BUSY/LOCKED debe romper esta ancla."""
    assert (
        frozenset(
            {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_BUSY_RECOVERY,
                sqlite3.SQLITE_LOCKED,
                sqlite3.SQLITE_LOCKED_SHAREDCACHE,
            }
        )
        == db_lifecycle_mod._CODIGOS_DE_CONTENCION_REINTENTABLES
    )


# ---------------------------------------------------------------------------
# Ronda 2 — REV-03 (deadline tras configurar timeout) y REV-04 (cancelación real)
# ---------------------------------------------------------------------------


async def test_convertir_a_wal_revalida_deadline_tras_configurar_busy_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El presupuesto puede agotarse DURANTE el PRAGMA busy_timeout del reintento."""
    # deadline=0.020; 1er error en 0.010; sleep; 2do loop en 0.015 con 5 ms
    # de restante; la lectura siguiente (0.025) representa el reloj DESPUÉS
    # de configurar el busy_timeout. No debe iniciarse el 2do pragma WAL.
    _instalar_reloj(monkeypatch, [0.0, 0.0, 0.010, 0.015, 0.025, 0.025])
    conn = _ConexionDeContrato(error_pragma=_error_busy())
    with pytest.raises(sqlite3.OperationalError):
        await convertir_a_wal(conn, busy_timeout_ms=20)
    assert conn.intentos_wal == 1


async def test_timeout_cero_instala_cero_sin_ventana_de_un_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """``0`` es fail-fast real: ni siquiera un milisegundo de busy handler."""
    _instalar_reloj(monkeypatch, [0.0, 0.0])
    conn = _ConexionDeContrato()
    await convertir_a_wal(conn, busy_timeout_ms=0)
    assert conn.sql == ["PRAGMA busy_timeout=0", "PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=0"]


async def test_cancelacion_externa_durante_restauracion_propaga_cancelacion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: una cancelación EXTERNA efectiva durante el cleanup MANDA.

    La versión anterior de este test exigía ``OperationalError`` con el
    ``CancelledError`` como contexto: la tarea quedaba sin cancelar y el
    llamador no podía distinguir "me cancelaron" de "falló" (la reproducción
    del Tech Lead: ``cancelled: False``, ``cancelling: 1``). El diagnóstico
    anterior se conserva, pero como contexto de la cancelación.
    """
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    original = sqlite3.OperationalError("disk I/O error")
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        error_pragma=original,
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_bloqueante_una_vez=True,
    )

    async def correr() -> None:
        await convertir_a_wal(conn, busy_timeout_ms=100)

    tarea = asyncio.create_task(correr())
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("cancelacion externa durante cleanup")
    liberar.set()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    # La restauración SÍ completó (el doble sólo se suspendió una vez): el
    # único motivo por el que esto es cancelación es la política R2-01, no un
    # error de cleanup.
    assert conn.sql[-1] == "PRAGMA busy_timeout=100"
    assert tarea.cancelled(), "la tarea DEBE quedar cancelada: es lo que decide el llamador"
    # Contraste con la reproducción del Tech Lead (``cancelled: False``,
    # ``cancelling: 1``): acá la cancelación consumida se da de baja con
    # ``uncancel()``, así que no queda ninguna pendiente inflando el contador
    # del task group que la contenga.
    assert tarea.cancelling() == 0, "la cancelación ya consumida no debe quedar pendiente"


async def test_cancelacion_externa_durante_restauracion_conserva_el_diagnostico(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La cancelación gana el tipo, pero el error primario no se pierde."""
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    original = sqlite3.OperationalError("disk I/O error")
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        error_pragma=original,
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_bloqueante_una_vez=True,
    )

    tarea = asyncio.create_task(convertir_a_wal(conn, busy_timeout_ms=100))
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("externa")
    liberar.set()
    with pytest.raises(asyncio.CancelledError) as capturado:
        await tarea
    assert conn.sql[-1] == "PRAGMA busy_timeout=100", (
        "la restauración completó: no hay error de cleanup que explique el tipo"
    )
    assert any(es is original for es in _cadena(capturado.value)), (
        f"el error primario no quedó en la cadena: {[type(e).__name__ for e in _cadena(capturado.value)]}"
    )


async def test_cancelacion_con_wait_for_no_deja_la_tarea_pendiente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.wait_for`` (timeout externo) también gana y deja la tarea cancelada."""
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_bloqueante_una_vez=True,
    )

    async def correr() -> None:
        await convertir_a_wal(conn, busy_timeout_ms=100)

    tarea = asyncio.create_task(correr())
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(tarea, timeout=0.2)
    liberar.set()
    assert tarea.cancelled(), "wait_for debe dejar la conversión cancelada, no viva detrás del timeout"
    assert conn.sql[-1] == "PRAGMA busy_timeout=100", "la restauración se completó pese al timeout externo"


async def test_cancelacion_repetida_conserva_la_primera_y_no_publica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dos Task.cancel() reales: la primera manda y la restauración se intenta igual."""
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        error_pragma=asyncio.CancelledError("primera"),
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_en_vuelo=True,
    )

    async def correr() -> None:
        await convertir_a_wal(conn, busy_timeout_ms=100)

    tarea = asyncio.create_task(correr())
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("segunda")
    liberar.set()
    with pytest.raises(asyncio.CancelledError) as capturado:
        await tarea
    assert tarea.cancelled()
    assert capturado.value is not None
    assert conn.sql[-1] == "PRAGMA busy_timeout=100", "la restauración corre aunque la cancelación sea múltiple"


async def test_fallo_ordinario_de_restauracion_se_encadena_sin_ciclo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin cancelación externa, el error de cleanup queda en la cadena (sin ciclos)."""
    original = sqlite3.OperationalError("disk I/O error")
    cleanup = sqlite3.OperationalError("restore fail")
    _instalar_reloj(monkeypatch, [0.0, 0.02])
    conn = _ConexionDeContrato(error_pragma=original, error_restauracion=cleanup, falla_timeout_en=2)
    with pytest.raises(sqlite3.OperationalError) as capturado:
        await convertir_a_wal(conn, busy_timeout_ms=100)
    assert capturado.value is original, "el primario manda ante un fallo ordinario de cleanup"
    cadena = _cadena(capturado.value)
    assert any(es is cleanup for es in cadena), "el error de limpieza no se registró en la cadena"
    assert len(cadena) == len({id(es) for es in cadena}), f"cadena circular: {[type(e).__name__ for e in cadena]}"


async def test_cancelacion_sin_await_en_restauracion_igual_se_propaga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: cancelación pedida en una ventana SIN ``await`` interrumpible.

    La restauración ya resolvió y el switch queda pendiente en el task: no hay
    ``CancelledError`` que capturar, pero la petición es del llamador. Debe
    salir como cancelación, no como "falló" (rama sin except de cleanup).
    """
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    original = sqlite3.OperationalError("disk I/O error")
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        error_pragma=original,
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
    )

    tarea = asyncio.create_task(convertir_a_wal(conn, busy_timeout_ms=100))
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("cancelacion sin await")
    liberar.set()
    with pytest.raises(asyncio.CancelledError) as capturado:
        await tarea
    assert tarea.cancelled(), "la cancelación pedida por el llamador no puede quedar como fallo"
    assert any(es is original for es in _cadena(capturado.value)), (
        f"el diagnóstico previo no quedó en la cadena: {[type(e).__name__ for e in _cadena(capturado.value)]}"
    )


async def test_fallo_del_primer_intento_conserva_el_diagnostico_bajo_cancelacion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: cancelación externa sobre un fallo REAL del primer intento.

    Precedencia: la cancelación que pidió el llamador manda, pero el fallo real
    no se pierde — queda como diagnóstico en la cadena. El caso importa porque
    ``hubo_error_real`` se derivaba de ``ultimo_error``, que sólo se asigna en
    la fase de REINTENTO: con un fallo del PRIMER intento (un
    ``OperationalError`` no reintentable, p. ej. disk I/O) el flag quedaba en
    ``False`` y el diagnóstico dependía de un accidente del camino.
    """
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    original = sqlite3.OperationalError("disk I/O error")
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        error_pragma=original,
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_cancelada_una_vez=True,
    )

    tarea = asyncio.create_task(convertir_a_wal(conn, busy_timeout_ms=100))
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("cancelación externa que cae en el cleanup")
    liberar.set()

    with pytest.raises(asyncio.CancelledError) as capturado:
        await tarea
    assert tarea.cancelled(), "la cancelación externa manda: la tarea debe quedar cancelada"
    assert any(es is original for es in _cadena(capturado.value)), (
        f"el fallo real del primer intento se perdió: {[type(x).__name__ for x in _cadena(capturado.value)]}"
    )
    assert _sin_ciclo(capturado.value), "la cadena causal quedó circular"


async def test_restauracion_exitosa_no_se_reporta_como_cancelacion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: una cancelación no contamina una conversión POSTERIOR de la misma tarea.

    El caso que motivó este test era una marca ``ContextVar`` que decía "el
    switch ya se consumió" y podía quedar prendida. Se midió que esa marca
    moría con el ``Task`` y que su rama era inalcanzable, así que se eliminó
    (ver ``_cancelacion_externa_efectiva``). Lo que este test ancla ahora es la
    propiedad que importa y que sobrevive a esa eliminación: consumir un switch
    en una llamada no inventa una cancelación en la siguiente.

    La segunda conversión tiene que terminar por FALLO con cleanup fallido
    (``falla_timeout_en=2``), no por el camino feliz: sólo la rama de fallo
    consulta la política de cancelación, así que una segunda conversión que
    sale bien no podría delatar una marca contaminada. Con esa rama, una marca
    pegada convierte un ``OperationalError`` ordinario en ``CancelledError``.
    """
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02, 0.02, 0.02, 0.02])
    vistas: list[str] = []

    async def dos_conversiones() -> None:
        # 1ª: se cancela durante la restauración => consume un switch.
        conn1 = _ConexionDeContrato(
            inicio_restauracion=inicio,
            liberar_restauracion=liberar,
            restauracion_bloqueante_una_vez=True,
        )
        try:
            await convertir_a_wal(conn1, busy_timeout_ms=100)
        except asyncio.CancelledError:
            vistas.append("primera: cancelada")
        # 2ª: en la MISMA tarea, sin cancelación pendiente, y con cleanup que
        # falla: es la única rama que consulta la política de cancelación.
        conn2 = _ConexionDeContrato(
            error_pragma=sqlite3.OperationalError("disk I/O error"),
            error_restauracion=sqlite3.OperationalError("restore fail"),
            falla_timeout_en=2,
        )
        try:
            await convertir_a_wal(conn2, busy_timeout_ms=100)
        except asyncio.CancelledError:
            vistas.append("segunda: CANCELADA SIN PETICIÓN")
        except sqlite3.OperationalError:
            vistas.append("segunda: ok")
        else:
            vistas.append("segunda: sin error inesperado")

    tarea = asyncio.create_task(dos_conversiones())
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("sólo la primera")
    liberar.set()
    await asyncio.wait_for(tarea, timeout=5.0)

    assert vistas == ["primera: cancelada", "segunda: ok"], (
        f"la marca de cancelación contaminó una conversión posterior: {vistas}"
    )


async def test_adjuntar_contexto_no_crea_ciclo_consigo_mismo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: ``_adjuntar_contexto_si_falta`` no puede dejar la cadena circular.

    Un cleanup que reentrega la MISMA instancia que se va a adjuntar produciría
    ``cancelacion.__context__ = cancelacion``: el traceback quedaría inservible
    y cualquier recorrido de la cadena no terminaría.
    """
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_cancelada_una_vez=True,
    )

    tarea = asyncio.create_task(convertir_a_wal(conn, busy_timeout_ms=100))
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("externa")
    liberar.set()
    with pytest.raises(asyncio.CancelledError) as capturado:
        await tarea
    assert _sin_ciclo(capturado.value), (
        f"la cadena quedó circular: {[type(x).__name__ for x in _cadena(capturado.value)]}"
    )


async def test_cleanup_que_relanza_el_primario_no_crea_ciclo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: la cadena causal no puede volverse circular.

    Un cleanup que relanza el mismo diagnóstico (caso real: un ``execute`` que
    reentrega la última excepción del cursor) produciría
    ``original.__context__ = original``. Se verifica sobre una instancia que YA
    tiene contexto: así el guard es lo único que evita el ciclo, no la
    ausencia de contexto.
    """
    previo = ValueError("diagnóstico anterior")
    original = sqlite3.OperationalError("disk I/O error")
    original.__context__ = previo  # cadena previa que NO se puede perder
    _instalar_reloj(monkeypatch, [0.0, 0.02])
    conn = _ConexionDeContrato(error_pragma=original, error_restauracion=original, falla_timeout_en=2)
    with pytest.raises(sqlite3.OperationalError) as capturado:
        await convertir_a_wal(conn, busy_timeout_ms=100)
    assert capturado.value is original
    assert _sin_ciclo(capturado.value), "la cadena quedó circular"
    assert original.__context__ is previo, "la cadena causal previa se perdió"


async def test_cancelacion_sin_error_primario_cierra_con_cancelacion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primario = CancelledError y el cleanup falla: si no hubo error de SQLite,
    una cancelación externa efectiva durante el cleanup sigue mandando."""
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    _instalar_reloj(monkeypatch, [0.0, 0.02])
    conn = _ConexionDeContrato(
        error_pragma=asyncio.CancelledError("intrinseca"),
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_bloqueante_una_vez=True,
    )

    tarea = asyncio.create_task(convertir_a_wal(conn, busy_timeout_ms=100))
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("externa final")
    liberar.set()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert tarea.cancelled()
    assert conn.sql[-1] == "PRAGMA busy_timeout=100", (
        "el cleanup fue exitoso: el desenlace lo decide la política de cancelación"
    )


async def test_cancelacion_intrinseca_no_se_adorna_con_el_fallo_de_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: la cancelación INTRÍNSECA se propaga tal cual, sin encadenar el cleanup.

    Precedencia, regla 2: si el primario ya era ``CancelledError``, el desenlace
    es ese mismo objeto y el fallo de la restauración NO se le cuelga. No es
    cosmética: la regla 3 (fallo real) sí encadena el error de limpieza, y si
    la regla 2 lo hiciera también, un fallo del cleanup aparecería como
    diagnóstico de una cancelación que no tiene nada que ver.

    Este test distingue las dos reglas por la CADENA, que es lo único que
    cambia: en ambos casos el tipo propagado y el objeto raíz son idénticos
    (``CancelledError``), así que asertar sólo el tipo —como hacían los tests
    de la ronda anterior— dejaba pasar el mutante que borra la regla 2.
    """
    primario = asyncio.CancelledError("intrinseca de la conversión")
    cleanup = sqlite3.OperationalError("restore fail")
    _instalar_reloj(monkeypatch, [0.0, 0.02])
    conn = _ConexionDeContrato(error_pragma=primario, error_restauracion=cleanup, falla_timeout_en=2)

    with pytest.raises(asyncio.CancelledError) as capturado:
        await convertir_a_wal(conn, busy_timeout_ms=100)

    assert capturado.value is primario, "la cancelación intrínseca debe propagarse como el MISMO objeto"
    cadena = _cadena(capturado.value)
    assert not any(es is cleanup for es in cadena), (
        f"la cancelación intrínseca se adomó con el fallo de cleanup: {[type(e).__name__ for e in cadena]}"
    )
    assert len(cadena) == 1, (
        f"la cadena de una cancelación intrínseca debe ser sólo ella: {[type(e).__name__ for e in cadena]}"
    )


async def test_cleanup_exitoso_no_consume_un_switch_de_cancelacion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-01: el switch que entrega la cancelación del llamador queda CONSUMIDO.

    Ojo con el nombre histórico: lo que este test gobierna no es "no tocar el
    contador", sino lo contrario. Se midió que un ``Task.cancel()`` que cae
    dentro del ``await`` de la restauración interrumpe ESE ``await`` —un
    ``Event.wait()`` cancelado siempre lanza, no hay forma de que "resuelva
    bien" con el switch en vuelo— así que ``cleanup_error`` NUNCA es ``None``
    en este escenario. Esa premisa del nombre viejo era falsa.

    La propiedad correcta, y la que importa para el llamador, es el estado
    FINAL del ``Task``: si el switch quedara pendiente (``cancelling() == 1``
    tras retornar), el ``await`` siguiente del MISMO ``Task`` lo recibiría por
    sorpresa y el llamador vería una cancelación que no puede atribuir a su
    propia llamada. Medido antes del arreglo:
    ``['primera: cancelada', 'segunda: CANCELADA SIN PETICIÓN']``.

    Se asertan las tres cosas observables a la vez: la tarea termina
    ``cancelled()``, el switch quedó en 0 dentro de la conversión, y no hubo
    ``sqlite`` de por medio (conexión de contrato).
    """
    inicio = asyncio.Event()
    liberar = asyncio.Event()
    _instalar_reloj(monkeypatch, [0.0, 0.02, 0.02])
    conn = _ConexionDeContrato(
        inicio_restauracion=inicio,
        liberar_restauracion=liberar,
        restauracion_bloqueante_una_vez=True,
    )
    contador_dentro: list[int] = []
    propagado: list[BaseException] = []

    async def correr() -> None:
        tarea = asyncio.current_task()
        assert tarea is not None
        try:
            await convertir_a_wal(conn, busy_timeout_ms=100)
        except BaseException as error:
            propagado.append(error)
            raise
        finally:
            contador_dentro.append(tarea.cancelling())

    tarea = asyncio.create_task(correr())
    await asyncio.wait_for(inicio.wait(), timeout=2.0)
    tarea.cancel("cancelación del llamador")
    liberar.set()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert tarea.cancelled(), "la tarea debe terminar cancelada, no sólo levantar"
    assert propagado, "la conversión debió propagar una excepción"
    assert isinstance(propagado[0], asyncio.CancelledError), (
        f"el llamador debe ver una cancelación, no {type(propagado[0]).__name__}"
    )
    # La restauración SÍ se ejecutó (el intento quedó registrado) antes de que
    # la cancelación se entregara: la secuencia de SQL lo prueba.
    assert conn.sql[-1] == "PRAGMA busy_timeout=100", f"la restauración debió intentarse: {conn.sql}"
    # El switch quedó consumido: nadie puede atribuirse una cancelación
    # posterior del mismo Task.
    assert contador_dentro == [0], (
        f"el switch de cancelación del llamador quedó pendiente (contaminaría el await "
        f"siguiente del mismo Task): {contador_dentro}"
    )


async def test_cancelacion_real_no_deja_worker_ni_conexion_huerfana(tmp_path: Path) -> None:
    """R2-05: con trabajo REALMENTE encolado en el worker, la cancelación es efectiva.

    ``asyncio.all_tasks()`` NO enumera hilos: el test anterior afirmaba una
    propiedad que no medían sus aserciones. Acá se ejerce trabajo encolado de
    verdad —una tarea propia que hace ``await conn.execute(...)`` sobre una
    conexión aiosqlite, o sea un ``run_in_executor`` hacia el hilo daemon del
    worker, más una sentencia larga (``randomblob``) que lo mantiene ocupado— y
    se comprueba la finalización OBSERVABLE del worker por la vía pública de
    ``aiosqlite``: al terminar ``close()`` el worker llamó a ``_stop_running()``,
    que vacía ``_tx`` y apaga ``_running`` (``_connection is None`` es la
    consecuencia pública de ``aiosqlite.Connection.close()``).

    Limitación declarada, no cubierta: aiosqlite crea el hilo con
    ``daemon=True`` y no expone join ni joinable; que un worker que murió por
    excepción NO se reporte como fuga NO se verifica acá (requiere introspección
    de ``_thread`` + un hook de excepción del hilo). Lo verificado es: según el
    mecanismo público instalado, la conexión terminó cerrada y su worker sin
    trabajo pendiente al salir ``close()``.
    """
    import aiosqlite

    db = tmp_path / "cancelacion_real.db"
    conn = await aiosqlite.connect(db)
    hilo_worker = getattr(getattr(conn, "_connection", None), "_thread", None)
    ocupado = asyncio.Event()

    async def escritor() -> None:
        # Trabajo real encolado: la sentencia larga ocupa el ÚNICO worker de
        # esta conexión, así que lo que venga después espera en su cola.
        await conn.execute("CREATE TABLE t (x)")
        await conn.commit()
        ocupado.set()
        await conn.execute("SELECT randomblob(100000000)")

    tarea = asyncio.create_task(escritor())
    await asyncio.wait_for(ocupado.wait(), timeout=2.0)
    tarea.cancel("cancelacion real con trabajo encolado")
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert tarea.cancelled(), "la cancelación no fue efectiva sobre la tarea"

    try:
        await asyncio.wait_for(conn.close(), timeout=5.0)
    finally:
        with contextlib.suppress(Exception):
            await conn.close()

    # Finalización observable del worker, por su estado público/documented.
    assert conn._connection is None, "el worker siguió vivo: close() no lo detuvo"
    cola = getattr(conn, "_tx", None)
    if cola is not None:
        assert cola.empty(), "quedó trabajo encolado en el worker tras el cierre"
    if hilo_worker is not None:
        hilo_worker.join(5)
        assert not hilo_worker.is_alive(), "el hilo del worker sigue vivo tras close()"
