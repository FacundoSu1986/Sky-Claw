"""Tests for SSP-001: Snapshot checksum sidecar verification.

Verifies that:
  1. create_snapshot writes a .meta.json sidecar with the full SHA256 checksum.
  2. restore_snapshot reads the checksum from the sidecar (not the filename).
  3. If the snapshot file is corrupted, restore_snapshot raises JournalSnapshotError
     when verify_checksum=True.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from sky_claw.db.journal import JournalSnapshotError
from sky_claw.db.snapshot_manager import FileSnapshotManager


@pytest.fixture
def manager(tmp_path: pathlib.Path):
    """Return an initialised FileSnapshotManager backed by a tmp directory."""
    snap_dir = tmp_path / "snapshots"
    mgr = FileSnapshotManager(snapshot_dir=snap_dir, max_size_mb=100)
    # initialize() is sync-like (mkdir), call it directly
    import asyncio

    asyncio.run(mgr.initialize())
    return mgr


class TestSnapshotChecksumSidecar:
    """SSP-001: Checksum sidecar .meta.json integrity."""

    @pytest.mark.asyncio
    async def test_create_snapshot_writes_meta_json(self, manager: FileSnapshotManager, tmp_path: pathlib.Path):
        """Snapshot creation must produce a .meta.json sidecar with 64-hex checksum."""
        original = tmp_path / "original.txt"
        original.write_text("hello snapshot", encoding="utf-8")

        info = await manager.create_snapshot(original)
        meta_path = pathlib.Path(info.snapshot_path).with_suffix(pathlib.Path(info.snapshot_path).suffix + ".meta.json")

        assert meta_path.exists(), f"Meta sidecar not found: {meta_path}"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "checksum" in data
        assert len(data["checksum"]) == 64
        # Validate hex format
        int(data["checksum"], 16)

    @pytest.mark.asyncio
    async def test_restore_snapshot_verifies_checksum_from_sidecar(
        self, manager: FileSnapshotManager, tmp_path: pathlib.Path
    ):
        """Restoring a corrupted snapshot must raise JournalSnapshotError."""
        original = tmp_path / "original.txt"
        original.write_text("pristine content", encoding="utf-8")

        info = await manager.create_snapshot(original)
        snapshot_path = pathlib.Path(info.snapshot_path)

        # Corrupt the snapshot file (but leave .meta.json intact)
        snapshot_path.write_text("CORRUPTED CONTENT", encoding="utf-8")

        target = tmp_path / "restored.txt"
        with pytest.raises(JournalSnapshotError) as exc_info:
            await manager.restore_snapshot(snapshot_path, target, verify_checksum=True)

        assert "Checksum verification failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_restore_snapshot_success_when_intact(self, manager: FileSnapshotManager, tmp_path: pathlib.Path):
        """Restoring an intact snapshot must succeed and copy the file correctly."""
        original = tmp_path / "original.txt"
        original.write_text("intact content", encoding="utf-8")

        info = await manager.create_snapshot(original)
        snapshot_path = pathlib.Path(info.snapshot_path)

        target = tmp_path / "restored.txt"
        result = await manager.restore_snapshot(snapshot_path, target, verify_checksum=True)

        assert result is True
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "intact content"
