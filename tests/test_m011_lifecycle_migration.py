"""M-01.1: locks/router/dlq must obtain their SQLite connection from
``DatabaseLifecycleManager`` when one is injected (contrato M-01).

Direct ``aiosqlite.connect`` bypasses WAL recovery, hardened pragmas, and the
coordinated ``shutdown_all()`` checkpoint. The DI is optional: without a
lifecycle each module keeps its pre-M-01 direct-connect fallback (existing
tests construct them bare and must stay green).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sky_claw.antigravity.core.db_lifecycle import DatabaseLifecycleManager
from sky_claw.antigravity.core.dlq_manager import DLQManager
from sky_claw.antigravity.core.event_bus import create_bus_with_dlq
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.orchestrator.rollback_factory import create_rollback_components


async def test_locks_uses_lifecycle_connection_and_does_not_close_it(tmp_path):
    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "locks.db"
        lock_mgr = DistributedLockManager(db, lifecycle=mgr)
        await lock_mgr.initialize()

        shared = await mgr.get_connection(db)
        assert lock_mgr._conn is shared

        # Lock roundtrip works on the lifecycle-managed connection.
        info = await lock_mgr.acquire_lock("res.esp", "agent-a")
        assert info.agent_id == "agent-a"
        assert await lock_mgr.release_lock("res.esp", "agent-a") is True

        # close() must NOT close the lifecycle-owned connection.
        await lock_mgr.close()
        async with shared.execute("SELECT 1") as cur:
            assert await cur.fetchone() is not None
    finally:
        await mgr.shutdown_all()


async def test_dlq_reuses_lifecycle_connection_across_operations(tmp_path):
    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "dlq.db"
        dlq = DLQManager(db_path=db, handler_resolver=lambda _name: None, lifecycle=mgr)

        assert await dlq.list_pending() == []
        key = str(db.resolve())
        assert key in mgr._connections
        shared = mgr._connections[key]

        # Second operation must reuse the SAME connection, still open.
        assert await dlq.list_dead() == []
        assert mgr._connections[key] is shared
        async with shared.execute("SELECT 1") as cur:
            assert await cur.fetchone() is not None
    finally:
        await mgr.shutdown_all()


async def test_router_uses_lifecycle_connection_and_does_not_close_it(tmp_path):
    from sky_claw.antigravity.agent.router import LLMRouter

    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "history.db"
        router = LLMRouter(
            provider=MagicMock(),
            db_path=str(db),
            lifecycle=mgr,
        )
        await router.open()

        shared = await mgr.get_connection(db)
        assert router._conn is shared

        await router.close()
        async with shared.execute("SELECT 1") as cur:
            assert await cur.fetchone() is not None
    finally:
        await mgr.shutdown_all()


def test_rollback_factory_threads_lifecycle_to_journal_and_locks(tmp_path):
    mgr = DatabaseLifecycleManager()
    components = create_rollback_components(tmp_path / "backups", lifecycle=mgr)
    assert components.lock_manager._lifecycle is mgr
    assert components.journal._lifecycle is mgr


def test_create_bus_with_dlq_threads_lifecycle(tmp_path):
    mgr = DatabaseLifecycleManager()
    bus = create_bus_with_dlq(db_path=tmp_path / "dlq.db", lifecycle=mgr)
    assert bus._dlq._lifecycle is mgr
