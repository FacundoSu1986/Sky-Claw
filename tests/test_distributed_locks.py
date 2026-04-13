"""Tests for DistributedLockManager and SnapshotTransactionLock.

Sprint 2 (Fase 1): Validates TTL-based lease expiration, atomic acquisition,
exponential backoff, rollback-on-failure, and lock release safety.
"""

from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

from sky_claw.db.locks import (
    DEFAULT_LOCK_TTL_SECONDS,
    DistributedLockManager,
    LockAcquisitionError,
    LockInfo,
    SnapshotTransactionLock,
)
from sky_claw.db.snapshot_manager import FileSnapshotManager


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
    from sky_claw.db.journal import JournalSnapshotError

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
