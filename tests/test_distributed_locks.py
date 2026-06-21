"""Tests for DistributedLockManager and SnapshotTransactionLock.

Sprint 2 (Fase 1): Validates TTL-based lease expiration, atomic acquisition,
exponential backoff, rollback-on-failure, and lock release safety.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from sky_claw.antigravity.db.locks import (
    DEFAULT_LOCK_TTL_SECONDS,
    DistributedLockManager,
    LockAcquisitionError,
    LockInfo,
    LockLeaseLostError,
    SnapshotTransactionLock,
)
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager

if TYPE_CHECKING:
    import pathlib

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_lock_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a temp path for the lock database."""
    return tmp_path / "test_locks.db"


@pytest.fixture
async def lock_manager(tmp_lock_db: pathlib.Path) -> DistributedLockManager:
    """Create and initialize a DistributedLockManager, close after test."""
    mgr = DistributedLockManager(
        tmp_lock_db,
        default_ttl=2.0,  # Short TTL for fast tests
        max_retries=3,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
def snapshot_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


@pytest.fixture
def snapshot_manager(snapshot_dir: pathlib.Path) -> FileSnapshotManager:
    return FileSnapshotManager(snapshot_dir=snapshot_dir)


# =============================================================================
# DistributedLockManager — basic operations
# =============================================================================


@pytest.mark.asyncio
async def test_acquire_and_release_lock(lock_manager: DistributedLockManager) -> None:
    """Agent can acquire and then release a lock."""
    lock = await lock_manager.acquire_lock("resource_a", "agent_1")

    assert lock.resource_id == "resource_a"
    assert lock.agent_id == "agent_1"
    assert lock.remaining_ttl > 0
    assert not lock.is_expired

    released = await lock_manager.release_lock("resource_a", "agent_1")
    assert released is True


@pytest.mark.asyncio
async def test_release_nonexistent_lock(lock_manager: DistributedLockManager) -> None:
    """Releasing a lock that doesn't exist returns False."""
    released = await lock_manager.release_lock("nonexistent", "agent_1")
    assert released is False


@pytest.mark.asyncio
async def test_acquire_lock_idempotent_same_agent(
    lock_manager: DistributedLockManager,
) -> None:
    """Same agent re-acquiring the same resource succeeds (owns the lock)."""
    # When the same agent already holds the lock, the expires_at check will
    # fail (lock is NOT expired). The SQL does not insert/update.
    # This is by design — the agent already holds it.
    lock1 = await lock_manager.acquire_lock("res", "agent_1", ttl=10.0)
    assert lock1 is not None

    # The second call should fail because the lock is NOT expired.
    with pytest.raises(LockAcquisitionError):
        await lock_manager.acquire_lock("res", "agent_2", ttl=10.0)


# =============================================================================
# TTL Expiration Tests (CRITICAL)
# =============================================================================


@pytest.mark.asyncio
async def test_expired_ttl_allows_reacquisition(
    tmp_lock_db: pathlib.Path,
) -> None:
    """When a lock's TTL expires, another agent can acquire it."""
    mgr = DistributedLockManager(
        tmp_lock_db,
        default_ttl=0.15,  # 150ms — will expire quickly
        max_retries=3,
        backoff_base=0.1,
        backoff_max=0.3,
    )
    await mgr.initialize()

    try:
        # Agent A acquires with very short TTL
        lock_a = await mgr.acquire_lock("file.esp", "agent_a")
        assert lock_a.agent_id == "agent_a"

        # Wait for TTL to expire
        await asyncio.sleep(0.25)

        # Agent B should now be able to acquire
        lock_b = await mgr.acquire_lock("file.esp", "agent_b")
        assert lock_b.agent_id == "agent_b"
        assert lock_b.resource_id == "file.esp"

    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_non_expired_ttl_blocks_acquisition(
    lock_manager: DistributedLockManager,
) -> None:
    """When TTL has not expired, another agent cannot acquire the lock."""
    await lock_manager.acquire_lock("critical.esp", "agent_1", ttl=10.0)

    with pytest.raises(LockAcquisitionError) as exc_info:
        await lock_manager.acquire_lock("critical.esp", "agent_2", ttl=10.0)

    assert exc_info.value.resource_id == "critical.esp"
    assert exc_info.value.agent_id == "agent_2"


@pytest.mark.asyncio
async def test_lock_info_shows_expiration(
    lock_manager: DistributedLockManager,
) -> None:
    """get_lock_info returns correct TTL metadata."""
    await lock_manager.acquire_lock("resource_x", "agent_1", ttl=5.0)

    info = await lock_manager.get_lock_info("resource_x")
    assert info is not None
    assert info.resource_id == "resource_x"
    assert info.agent_id == "agent_1"
    assert info.remaining_ttl > 0
    assert info.remaining_ttl <= 5.0
    assert not info.is_expired


@pytest.mark.asyncio
async def test_lock_info_expired(tmp_lock_db: pathlib.Path) -> None:
    """LockInfo.is_expired returns True after TTL passes."""
    mgr = DistributedLockManager(tmp_lock_db, default_ttl=0.1)
    await mgr.initialize()
    try:
        await mgr.acquire_lock("res", "agent_1")
        await asyncio.sleep(0.15)

        info = await mgr.get_lock_info("res")
        assert info is not None
        assert info.is_expired
        assert info.remaining_ttl == 0.0
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_lock_info_nonexistent(lock_manager: DistributedLockManager) -> None:
    """get_lock_info on unknown resource returns None."""
    assert await lock_manager.get_lock_info("nope") is None


# =============================================================================
# Exponential Backoff
# =============================================================================


@pytest.mark.asyncio
async def test_acquire_retries_with_backoff(
    lock_manager: DistributedLockManager,
) -> None:
    """Acquisition retries multiple times before failing."""
    await lock_manager.acquire_lock("locked_res", "agent_holder", ttl=60.0)

    t_start = time.monotonic()
    with pytest.raises(LockAcquisitionError):
        await lock_manager.acquire_lock("locked_res", "agent_waiter", ttl=60.0)
    elapsed = time.monotonic() - t_start

    # With backoff_base=0.05 and 3 retries (0 + 0.05 + 0.10 = 0.15 minimum)
    assert elapsed >= 0.1, f"Backoff too fast: {elapsed:.3f}s"


# =============================================================================
# Cleanup
# =============================================================================


@pytest.mark.asyncio
async def test_cleanup_expired_locks(tmp_lock_db: pathlib.Path) -> None:
    """cleanup_expired removes only expired locks."""
    mgr = DistributedLockManager(tmp_lock_db, default_ttl=0.1, max_retries=1)
    await mgr.initialize()
    try:
        await mgr.acquire_lock("res_a", "agent_1")
        await mgr.acquire_lock("res_b", "agent_2")

        # Also acquire one with a long TTL
        mgr2 = DistributedLockManager(tmp_lock_db, default_ttl=60.0, max_retries=1)
        await mgr2.initialize()
        await mgr2.acquire_lock("res_c", "agent_3")

        await asyncio.sleep(0.15)

        removed = await mgr.cleanup_expired()
        assert removed == 2  # res_a and res_b expired

        # res_c should still exist
        info = await mgr.get_lock_info("res_c")
        assert info is not None
        assert not info.is_expired

        await mgr2.close()
    finally:
        await mgr.close()


# =============================================================================
# Force Release
# =============================================================================


@pytest.mark.asyncio
async def test_force_release(lock_manager: DistributedLockManager) -> None:
    """force_release deletes a lock regardless of agent_id."""
    await lock_manager.acquire_lock("res", "agent_1")
    released = await lock_manager.force_release("res")
    assert released is True

    info = await lock_manager.get_lock_info("res")
    assert info is None


# =============================================================================
# SnapshotTransactionLock — integration
# =============================================================================


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_happy_path(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Normal exit: lock acquired, snapshot created, lock released."""
    target = tmp_path / "test_mod.esp"
    target.write_text("original content")

    await snapshot_manager.initialize()

    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="test_mod.esp",
        agent_id="synthesis-agent",
        target_files=[target],
        ttl=5.0,
    ) as ctx:
        assert ctx.lock_info is not None
        assert ctx.lock_info.resource_id == "test_mod.esp"
        assert len(ctx.snapshots) == 1
        assert ctx.snapshots[0].original_path == str(target)

        # Simulate modification
        target.write_text("modified content")

    # After normal exit, lock should be released
    info = await lock_manager.get_lock_info("test_mod.esp")
    assert info is None  # Released

    # File should still have modified content (no rollback)
    assert target.read_text() == "modified content"


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_rollback_on_error(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Exception: rollback restores original file, lock released."""
    target = tmp_path / "rollback_test.esp"
    target.write_text("pristine state")

    await snapshot_manager.initialize()

    with pytest.raises(RuntimeError, match="pipeline exploded"):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="rollback_test.esp",
            agent_id="dyndolod-agent",
            target_files=[target],
        ) as _:
            # Modify file, then crash
            target.write_text("corrupted state")
            raise RuntimeError("pipeline exploded")

    # File should be restored to original
    assert target.read_text() == "pristine state"

    # Lock should be released
    info = await lock_manager.get_lock_info("rollback_test.esp")
    assert info is None


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_no_files(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """Transaction lock works with no target files (lock only)."""
    await snapshot_manager.initialize()

    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="lockonly",
        agent_id="agent_1",
    ) as ctx:
        assert ctx.lock_info is not None
        assert len(ctx.snapshots) == 0

    # Lock released
    assert await lock_manager.get_lock_info("lockonly") is None


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_nonexistent_file_skipped(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Non-existent files in target_files are silently skipped."""
    await snapshot_manager.initialize()

    nonexistent = tmp_path / "does_not_exist.esp"

    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="skip_test",
        agent_id="agent_1",
        target_files=[nonexistent],
    ) as ctx:
        assert len(ctx.snapshots) == 0  # Skipped


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_releases_on_snapshot_failure(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """If snapshot creation fails, the lock is still released."""
    from sky_claw.antigravity.db.journal import JournalSnapshotError

    snap_mgr = FileSnapshotManager(snapshot_dir=tmp_path / "snaps")
    await snap_mgr.initialize()

    # Create a real file so the is_file() check passes, then mock the
    # create_snapshot method to raise JournalSnapshotError.
    target_file = tmp_path / "will_fail.esp"
    target_file.write_text("content")

    async def _failing_create(*args: object, **kwargs: object) -> None:
        raise JournalSnapshotError("Simulated snapshot I/O failure")

    snap_mgr.create_snapshot = _failing_create  # type: ignore[assignment]

    with pytest.raises(JournalSnapshotError, match="Simulated"):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snap_mgr,
            resource_id="snap_fail",
            agent_id="agent_1",
            target_files=[target_file],
        ):
            pass  # Should not reach here

    # Lock should still be released despite snapshot failure
    info = await lock_manager.get_lock_info("snap_fail")
    assert info is None


# =============================================================================
# SnapshotTransactionLock — force_rollback (dry-run / preview)
# =============================================================================


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_force_rollback_on_clean_exit(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """force_rollback=True restores files even on a CLEAN (no-exception) exit.

    This is the primitive that powers dry-run/preview: the chain runs for real
    inside the lock, then every target file is reverted on the way out.
    """
    target = tmp_path / "preview.esp"
    target.write_text("original content")

    await snapshot_manager.initialize()

    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="preview.esp",
        agent_id="preview-agent",
        target_files=[target],
        force_rollback=True,
    ) as ctx:
        assert len(ctx.snapshots) == 1
        # Simulate a real run mutating the file inside the transaction.
        target.write_text("mutated by dry-run chain")

    # Clean exit, but force_rollback reverted the file to its original bytes.
    assert target.read_text() == "original content"
    # Lock released as usual.
    assert await lock_manager.get_lock_info("preview.esp") is None


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_force_rollback_false_keeps_mutation(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Default force_rollback=False preserves a clean-exit mutation (backward-compat)."""
    target = tmp_path / "real_run.esp"
    target.write_text("before")

    await snapshot_manager.initialize()

    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="real_run.esp",
        agent_id="real-agent",
        target_files=[target],
    ):
        target.write_text("after")

    # No force_rollback → the mutation survives (existing production behavior).
    assert target.read_text() == "after"


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_cancellation_releases_lock(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Cancellation mid-preview leaves no orphan lock and reverts the file.

    Invariant: ``asyncio.CancelledError`` must still release the lock via the
    ``finally`` block, and (because the error is non-None) trigger rollback.
    """
    target = tmp_path / "cancel.esp"
    target.write_text("pristine")

    await snapshot_manager.initialize()

    entered = asyncio.Event()

    async def _run() -> None:
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="cancel.esp",
            agent_id="agent-cancel",
            target_files=[target],
            force_rollback=True,
        ):
            target.write_text("mid-flight")
            entered.set()  # lock held + snapshot taken + file mutated
            await asyncio.sleep(10)  # cancelled here

    task = asyncio.create_task(_run())
    # Deterministic hand-off: wait until the task is *inside* the context instead
    # of racing a fixed sleep(0.05), which flaked under full-suite CPU load.
    # Bounded + task-aware: if _run() fails before entered.set() (e.g. lock
    # acquisition or snapshot creation raises), surface that failure immediately
    # instead of hanging the suite forever on an unbounded entered.wait().
    entered_wait = asyncio.ensure_future(entered.wait())
    try:
        done, _pending = await asyncio.wait(
            {task, entered_wait},
            timeout=5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            # _run() exited before signalling entry → re-raise its real failure
            # (or fail clearly if it somehow returned without entering).
            await task
            raise AssertionError("task exited before entering the lock context")
        if not done:
            # Timed out without entry: drain the cancelled task with a *bounded*
            # await so the context manager can unwind (release the lock) and no
            # dangling task is left for fixture teardown to trip over.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
            raise AssertionError("task did not enter the lock context within 5s")
    finally:
        entered_wait.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await entered_wait

    # entered fired → the task is parked at sleep(10) inside the lock context.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # No orphaned lock despite cancellation.
    assert await lock_manager.get_lock_info("cancel.esp") is None
    # File reverted to its pre-transaction bytes.
    assert target.read_text() == "pristine"


@pytest.mark.asyncio
async def test_force_rollback_raises_when_restore_fails(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """A failed restore during a FORCED (dry-run) rollback must surface loudly.

    Otherwise a preview could leave a file mutated while reporting success —
    silently violating the no-mutation guarantee. The lock must still release.
    """
    from sky_claw.antigravity.db.locks import SnapshotRollbackError

    target = tmp_path / "preview.esp"
    target.write_text("original")
    await snapshot_manager.initialize()

    async def _failing_restore(*_a: object, **_k: object) -> bool:
        raise OSError("disk gone")

    snapshot_manager.restore_snapshot = _failing_restore  # type: ignore[assignment]

    with pytest.raises(SnapshotRollbackError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="preview.esp",
            agent_id="preview-agent",
            target_files=[target],
            force_rollback=True,
        ):
            target.write_text("mutated by dry-run")

    # Lock released despite the rollback failure (no orphan).
    assert await lock_manager.get_lock_info("preview.esp") is None


@pytest.mark.asyncio
async def test_exception_path_restore_failure_does_not_mask_original(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """On the EXCEPTION path a failed restore is logged but must not shadow the
    original exception (no SnapshotRollbackError masking)."""
    target = tmp_path / "x.esp"
    target.write_text("original")
    await snapshot_manager.initialize()

    async def _failing_restore(*_a: object, **_k: object) -> bool:
        raise OSError("disk gone")

    snapshot_manager.restore_snapshot = _failing_restore  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="boom"):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="x.esp",
            agent_id="agent",
            target_files=[target],
        ):
            raise RuntimeError("boom")

    assert await lock_manager.get_lock_info("x.esp") is None


# =============================================================================
# Concurrency test
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_lock_acquisition(
    tmp_lock_db: pathlib.Path,
) -> None:
    """Only one of two concurrent agents can acquire the same resource."""
    mgr = DistributedLockManager(
        tmp_lock_db,
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.1,
    )
    await mgr.initialize()

    results: dict[str, str] = {}  # agent_id -> "ok" | "fail"

    async def try_acquire(agent_id: str) -> None:
        try:
            await mgr.acquire_lock("contested_resource", agent_id)
            results[agent_id] = "ok"
        except LockAcquisitionError:
            results[agent_id] = "fail"

    try:
        await asyncio.gather(
            try_acquire("agent_alpha"),
            try_acquire("agent_beta"),
        )

        # Exactly one should succeed, one should fail
        assert sorted(results.values()) == ["fail", "ok"]
    finally:
        await mgr.close()


# =============================================================================
# Default TTL constant
# =============================================================================


def test_default_ttl_is_ten_minutes() -> None:
    """Default TTL constant is 600 seconds (10 minutes) for xEdit compatibility."""
    assert DEFAULT_LOCK_TTL_SECONDS == 600.0


# =============================================================================
# LockInfo dataclass
# =============================================================================


def test_lock_info_remaining_ttl_zero_when_expired() -> None:
    """remaining_ttl clamps to 0 when lock is expired."""
    info = LockInfo(
        resource_id="r",
        agent_id="a",
        acquired_at=time.time() - 100,
        expires_at=time.time() - 50,
    )
    assert info.is_expired
    assert info.remaining_ttl == 0.0


def test_lock_info_remaining_ttl_positive_when_active() -> None:
    """remaining_ttl is positive for active locks."""
    info = LockInfo(
        resource_id="r",
        agent_id="a",
        acquired_at=time.time(),
        expires_at=time.time() + 300,
    )
    assert not info.is_expired
    assert info.remaining_ttl > 0


# =============================================================================
# DistributedLockManager — renew_lock (hardening jun-2026)
# =============================================================================


@pytest.mark.asyncio
async def test_renew_lock_extends_live_lease(lock_manager: DistributedLockManager) -> None:
    """renew_lock extiende expires_at de un lease vivo del mismo agente."""
    await lock_manager.acquire_lock("res-renew", "agent-1", ttl=2.0)
    before = await lock_manager.get_lock_info("res-renew")
    assert before is not None

    renewed = await lock_manager.renew_lock("res-renew", "agent-1", ttl=10.0)

    assert renewed is True
    after = await lock_manager.get_lock_info("res-renew")
    assert after is not None
    assert after.expires_at > before.expires_at


@pytest.mark.asyncio
async def test_renew_lock_returns_false_for_expired_lease(
    lock_manager: DistributedLockManager,
) -> None:
    """Un lease ya expirado no puede renovarse — el holder perdió la exclusividad."""
    await lock_manager.acquire_lock("res-expired", "agent-1", ttl=0.05)
    await asyncio.sleep(0.15)

    assert await lock_manager.renew_lock("res-expired", "agent-1") is False


@pytest.mark.asyncio
async def test_renew_lock_returns_false_for_other_agent(
    lock_manager: DistributedLockManager,
) -> None:
    """Solo el agente dueño del lease puede renovarlo."""
    await lock_manager.acquire_lock("res-owned", "agent-1", ttl=5.0)

    assert await lock_manager.renew_lock("res-owned", "agent-2") is False


@pytest.mark.asyncio
async def test_renew_lock_swallows_unexpected_sqlite_error(
    lock_manager: DistributedLockManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """renew_lock es best-effort: CUALQUIER sqlite3.Error (no solo
    Operational/Integrity) degrada a False en vez de propagarse.

    PR #181 review (Copilot): si una subclase fuera del tuple legacy
    (p.ej. ProgrammingError por conexión cerrada) escapara, mataría el
    heartbeat sin marcar lease_lost. Acá se ejercita el layer de renew_lock
    directamente (el test del heartbeat parchea renew_lock entero).
    """
    await lock_manager.acquire_lock("res-dberr", "agent-1", ttl=5.0)

    class _BoomConn:
        def execute(self, *args: object, **kwargs: object) -> object:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    # _ensure_conn() devuelve la conn aiosqlite viva; la cambiamos por una cuyo
    # execute() tira un sqlite3.Error que el catch viejo no cubría.
    monkeypatch.setattr(lock_manager, "_ensure_conn", lambda: _BoomConn())

    assert await lock_manager.renew_lock("res-dberr", "agent-1") is False


# =============================================================================
# SnapshotTransactionLock — heartbeat auto-renew (hardening jun-2026)
# =============================================================================


@pytest.mark.asyncio
async def test_snapshot_lock_heartbeat_keeps_lease_alive(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """Una operación más larga que el TTL no pierde el lease: el heartbeat renueva.

    Sin heartbeat, dormir 2.5x TTL dentro del contexto dejaría el lease expirado
    y un segundo agente podría adquirir el mismo recurso (dos escritores).
    """
    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="res-hb",
        agent_id="holder",
        ttl=0.4,
    ):
        await asyncio.sleep(1.0)  # 2.5x TTL — el heartbeat debe haber renovado
        with pytest.raises(LockAcquisitionError):
            await lock_manager.acquire_lock("res-hb", "intruder", ttl=0.4)

    # Tras la salida limpia el lock se libera y otro agente puede adquirirlo.
    info = await lock_manager.acquire_lock("res-hb", "intruder", ttl=0.4)
    assert info.agent_id == "intruder"


@pytest.mark.asyncio
async def test_snapshot_lock_lease_lost_raises_on_clean_exit(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """Si el renew falla (lease perdido), la salida limpia debe avisar al caller.

    El holder pudo haber competido con otro escritor — reportar éxito sería
    mentir sobre la exclusividad de la operación.
    """
    with pytest.raises(LockLeaseLostError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="res-lost",
            agent_id="holder",
            ttl=0.45,
        ) as ctx:
            # Simula que otro proceso limpió/robó el lock (force_release admin).
            await lock_manager.force_release("res-lost")
            await asyncio.sleep(0.6)  # el heartbeat detecta el renew fallido
            assert ctx.lease_lost is True


@pytest.mark.asyncio
async def test_snapshot_lock_heartbeat_survives_renew_crash(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una excepción inesperada en renew_lock no mata el heartbeat en silencio.

    Si la renovación crashea (p.ej. conexión cerrada), el lease va a expirar
    igual: debe tratarse como lease perdido y reportarse en la salida limpia,
    no morir como task exception nunca recuperada.
    """

    async def exploding_renew(*args: object, **kwargs: object) -> bool:
        raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    monkeypatch.setattr(lock_manager, "renew_lock", exploding_renew)

    with pytest.raises(LockLeaseLostError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="res-crash",
            agent_id="holder",
            ttl=0.3,
        ) as ctx:
            await asyncio.sleep(0.4)  # al menos un beat (intervalo 0.1s)
            assert ctx.lease_lost is True


# =============================================================================
# SnapshotTransactionLock — assert_owned() + renew_divisor (hardening jun-2026)
# =============================================================================
# Cierra la ventana entre la pérdida real del lease y su detección por el
# heartbeat (acotada solo por TTL/divisor). assert_owned() se llama justo antes
# de cada mutación crítica para re-verificar exclusividad; renew_divisor permite
# acortar el intervalo de renovación sin tocar el TTL.


@pytest.mark.asyncio
async def test_assert_owned_passes_while_lease_held(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """Con el lease vivo y propio, assert_owned() no levanta (camino feliz)."""
    await snapshot_manager.initialize()
    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="res-owned-ok",
        agent_id="holder",
        ttl=5.0,
        auto_renew=False,
    ) as ctx:
        await ctx.assert_owned()  # verify_db=True por defecto — no debe levantar


@pytest.mark.asyncio
async def test_assert_owned_fast_path_short_circuits_db(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si el heartbeat ya marcó lease_lost, el fast-path levanta SIN tocar la DB.

    Parcheamos get_lock_info para que explote: si assert_owned lo consultara,
    veríamos ese error; al levantar LockLeaseLostError probamos el corto-circuito.
    """
    await snapshot_manager.initialize()

    async def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("get_lock_info no debería consultarse en el fast-path")

    with pytest.raises(LockLeaseLostError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="res-fastpath",
            agent_id="holder",
            ttl=5.0,
            auto_renew=False,
        ) as ctx:
            ctx._lease_lost = True  # simula la detección previa del heartbeat
            monkeypatch.setattr(lock_manager, "get_lock_info", _boom)
            await ctx.assert_owned()


@pytest.mark.asyncio
async def test_assert_owned_verify_db_detects_stolen_lease(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """assert_owned(verify_db=True) detecta que otro agente robó el recurso.

    Sin heartbeat (auto_renew=False) la pérdida solo se descubre con el chequeo
    fresco contra la DB justo antes de mutar; debe levantar y dejar lease_lost.
    """
    await snapshot_manager.initialize()
    captured: dict[str, SnapshotTransactionLock] = {}

    with pytest.raises(LockLeaseLostError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="res-steal",
            agent_id="holder",
            ttl=5.0,
            auto_renew=False,
        ) as ctx:
            captured["ctx"] = ctx
            # Otro proceso reclama el recurso (admin force_release + reacquire).
            await lock_manager.force_release("res-steal")
            await lock_manager.acquire_lock("res-steal", "thief", ttl=5.0)
            await ctx.assert_owned()  # propiedad perdida → levanta

    assert captured["ctx"].lease_lost is True


@pytest.mark.asyncio
async def test_assert_owned_verify_db_false_is_pure_fast_path(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_db=False nunca consulta la DB: solo mira el flag del heartbeat."""
    await snapshot_manager.initialize()

    async def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("verify_db=False no debe tocar la DB")

    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="res-nodb",
        agent_id="holder",
        ttl=5.0,
        auto_renew=False,
    ) as ctx:
        monkeypatch.setattr(lock_manager, "get_lock_info", _boom)
        await ctx.assert_owned(verify_db=False)  # lease_lost=False → no levanta


@pytest.mark.asyncio
async def test_renew_divisor_below_one_rejected(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """renew_divisor < 1.0 renovaría DESPUÉS de expirar → se rechaza en construcción."""
    with pytest.raises(ValueError):
        SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="res-bad-div",
            agent_id="holder",
            renew_divisor=0.5,
        )


@pytest.mark.asyncio
async def test_renew_divisor_shrinks_detection_window(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """Un divisor mayor renueva más seguido y mantiene vivo un lease de TTL corto.

    Con renew_divisor=6 sobre TTL=0.6 el intervalo es ~0.1s; dormir 1.4x TTL
    dentro del contexto no debe perder la exclusividad.
    """
    await snapshot_manager.initialize()
    async with SnapshotTransactionLock(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        resource_id="res-div-hb",
        agent_id="holder",
        ttl=0.6,
        renew_divisor=6.0,
    ):
        await asyncio.sleep(0.85)  # > TTL — solo sobrevive si renovó varias veces
        with pytest.raises(LockAcquisitionError):
            await lock_manager.acquire_lock("res-div-hb", "intruder", ttl=0.6)
