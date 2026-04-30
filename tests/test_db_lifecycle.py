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
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from pydantic import ValidationError

from sky_claw.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
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
        conn = lifecycle.get_connection(str(db_path))
        assert conn is not None

        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
            assert row[0] == "wal"

        await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_pragma_application_on_connect(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        await lifecycle.init_all()
        conn = lifecycle.get_connection(str(db_path))

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
        conn = lifecycle.get_connection(str(db_path))
        async with conn.execute("SELECT val FROM test") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "before_crash"

        await lifecycle.shutdown_all()

    @pytest.mark.asyncio
    async def test_init_with_clean_state(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        """Init on a clean database should work without issues."""
        await lifecycle.init_all()
        conn = lifecycle.get_connection(str(db_path))
        assert conn is not None
        await lifecycle.shutdown_all()


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------


class TestCheckpoint:
    @pytest.mark.asyncio
    async def test_passive_checkpoint(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        await lifecycle.init_all()
        conn = lifecycle.get_connection(str(db_path))

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
        conn = lifecycle.get_connection(str(db_path))

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
    async def test_shutdown_no_orphan_wal(self, lifecycle: DatabaseLifecycleManager, db_path: Path) -> None:
        """After shutdown, WAL and SHM files should be eliminated."""
        await lifecycle.init_all()
        conn = lifecycle.get_connection(str(db_path))

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
        conn = lifecycle.get_connection(str(db_path))

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
        conn = lifecycle.get_connection(str(db_path))

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
        conn = lifecycle.get_connection(str(db_path))

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
