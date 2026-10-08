"""Tests de veto de rollback cuando la terminalidad es desconocida (issue #623).

Verifica que:
1. SnapshotTransactionLock veta rollback de snapshots si la excepción tiene
   terminality_unknown o deriva de VfsTeardownError (caminando la cadena causal).
2. DirectoryRollback veta la restauración si la excepción tiene terminality_unknown.
3. Los casos de control (excepción normal sin terminality_unknown) continúan
   restaurando normalmente.
4. El lock se libera siempre, incluso cuando el rollback fue vetado.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.app.db.locks import (
    DistributedLockManager,
    SnapshotTransactionLock,
)
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.mo2.brokered_loot import LOOTTimeoutError
from sky_claw.local.mo2.vfs_broker import VfsTeardownDeadlineError
from sky_claw.local.tools._dir_rollback import DirectoryRollback


@pytest.fixture
def tmp_lock_db(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "test_locks.db"


@pytest.fixture
async def lock_manager(tmp_lock_db: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_lock_db,
        default_ttl=2.0,
        max_retries=3,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
def snapshot_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


@pytest.fixture
def snapshot_manager(snapshot_dir: pathlib.Path) -> FileSnapshotManager:
    return FileSnapshotManager(snapshot_dir=snapshot_dir)


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_vetoes_rollback_when_terminality_unknown(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Si la terminalidad del worker es desconocida, SnapshotTransactionLock NO debe revertir."""
    target = tmp_path / "loadorder.txt"
    target.write_text("pristine loadorder")
    await snapshot_manager.initialize()

    exc = LOOTTimeoutError(10.0)
    exc.terminality_unknown = True
    exc.teardown_error = VfsTeardownDeadlineError("teardown deadline")

    tx_lock: SnapshotTransactionLock | None = None
    with pytest.raises(LOOTTimeoutError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="loadorder.txt",
            agent_id="loot-agent",
            target_files=[target],
        ) as tx:
            tx_lock = tx
            target.write_text("mutated loadorder during sort")
            raise exc

    assert tx_lock is not None
    # El archivo NO debe haber sido revertido porque un worker vivo podría seguir escribiendo
    assert target.read_text() == "mutated loadorder during sort"
    assert getattr(tx_lock, "rollback_vetoed_unknown_terminality", False) is True
    assert tx_lock.rollback_attempted is False

    # El lock debe estar liberado
    info = await lock_manager.get_lock_info("loadorder.txt")
    assert info is None


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_vetoes_force_rollback_when_terminality_unknown(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Incluso con force_rollback=True (preview), terminalidad desconocida veta rollback."""
    target = tmp_path / "preview.txt"
    target.write_text("pristine preview")
    await snapshot_manager.initialize()

    exc = RuntimeError("timeout en preview")
    exc.terminality_unknown = True

    tx_lock: SnapshotTransactionLock | None = None
    with pytest.raises(RuntimeError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="preview.txt",
            agent_id="preview-agent",
            target_files=[target],
            force_rollback=True,
        ) as tx:
            tx_lock = tx
            target.write_text("mutated preview state")
            raise exc

    assert tx_lock is not None
    assert target.read_text() == "mutated preview state"
    assert getattr(tx_lock, "rollback_vetoed_unknown_terminality", False) is True
    assert tx_lock.rollback_attempted is False


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_chain_walk_detects_unknown_cause(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Verifica que el veto detecte terminality_unknown a través de la cadena __cause__."""
    target = tmp_path / "chain.txt"
    target.write_text("pristine chain")
    await snapshot_manager.initialize()

    cause = VfsTeardownDeadlineError("deadline expired")
    exc = RuntimeError("high level operation failed")
    exc.__cause__ = cause

    tx_lock: SnapshotTransactionLock | None = None
    with pytest.raises(RuntimeError):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="chain.txt",
            agent_id="chain-agent",
            target_files=[target],
        ) as tx:
            tx_lock = tx
            target.write_text("mutated chain state")
            raise exc

    assert tx_lock is not None
    assert target.read_text() == "mutated chain state"
    assert getattr(tx_lock, "rollback_vetoed_unknown_terminality", False) is True
    assert tx_lock.rollback_attempted is False


@pytest.mark.asyncio
async def test_snapshot_transaction_lock_normal_error_restores_snapshot(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Control: un error ordinario sin terminalidad desconocida sí ejecuta rollback."""
    target = tmp_path / "control.txt"
    target.write_text("pristine control")
    await snapshot_manager.initialize()

    tx_lock: SnapshotTransactionLock | None = None
    with pytest.raises(RuntimeError, match="error ordinario"):
        async with SnapshotTransactionLock(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            resource_id="control.txt",
            agent_id="control-agent",
            target_files=[target],
        ) as tx:
            tx_lock = tx
            target.write_text("mutated control state")
            raise RuntimeError("error ordinario")

    assert tx_lock is not None
    assert target.read_text() == "pristine control"
    assert getattr(tx_lock, "rollback_vetoed_unknown_terminality", False) is False
    assert tx_lock.rollback_attempted is True


@pytest.mark.asyncio
async def test_directory_rollback_vetoes_restore_when_terminality_unknown(
    tmp_path: pathlib.Path,
) -> None:
    """DirectoryRollback no restaura el backup si la excepción tiene terminality_unknown."""
    target_dir = tmp_path / "output_dir"
    target_dir.mkdir()
    (target_dir / "old_file.txt").write_text("initial content")

    exc = LOOTTimeoutError(5.0)
    exc.terminality_unknown = True

    rb = DirectoryRollback(target_dir)
    with pytest.raises(LOOTTimeoutError):
        async with rb:
            target_dir.mkdir()
            (target_dir / "new_file.txt").write_text("new mutated content")
            raise exc

    # No debe haber restaurado: new_file debe seguir existiendo y el backup no fue movido de vuelta
    assert not rb.rollback_completed
    assert (target_dir / "new_file.txt").exists()
    assert (target_dir / "new_file.txt").read_text() == "new mutated content"
    assert not (target_dir / "old_file.txt").exists()


@pytest.mark.asyncio
async def test_directory_rollback_normal_error_restores_backup(
    tmp_path: pathlib.Path,
) -> None:
    """Control: DirectoryRollback restaura el backup ante error ordinario."""
    target_dir = tmp_path / "control_dir"
    target_dir.mkdir()
    (target_dir / "old_file.txt").write_text("initial content")

    rb = DirectoryRollback(target_dir)
    with pytest.raises(RuntimeError, match="error comun"):
        async with rb:
            target_dir.mkdir()
            (target_dir / "bad_file.txt").write_text("bad")
            raise RuntimeError("error comun")

    assert rb.rollback_completed
    assert (target_dir / "old_file.txt").exists()
    assert (target_dir / "old_file.txt").read_text() == "initial content"
    assert not (target_dir / "bad_file.txt").exists()
