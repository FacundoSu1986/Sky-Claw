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


async def test_dlq_rolls_back_dangling_transaction_on_shared_connection(tmp_path, monkeypatch):
    """A DLQ write interrupted between its DML and ``commit()`` (e.g. the task
    cancelled during shutdown) must not leave an open transaction on the SHARED
    lifecycle connection — the next operation would inherit and commit/discard
    it. The per-op fallback got this for free (closing the connection rolls
    back); the lifecycle path must roll back explicitly.
    """
    import asyncio

    import pytest

    from sky_claw.antigravity.core.event_bus import Event

    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "dlq.db"
        dlq = DLQManager(db_path=db, handler_resolver=lambda _n: None, lifecycle=mgr)
        await dlq.list_pending()  # ensure schema + register the shared connection
        shared = mgr._connections[str(db.resolve())]

        async def _cancelled_commit():
            raise asyncio.CancelledError

        monkeypatch.setattr(shared, "commit", _cancelled_commit)
        with pytest.raises(asyncio.CancelledError):
            await dlq.enqueue(Event(topic="t.fail", payload={"k": "v"}), lambda _e: None, RuntimeError("x"))
        monkeypatch.undo()

        assert shared.in_transaction is False, "dangling transaction left on the shared connection"
        # The discarded row must not resurface in a later (healthy) operation.
        assert await dlq.list_pending() == []
    finally:
        await mgr.shutdown_all()
