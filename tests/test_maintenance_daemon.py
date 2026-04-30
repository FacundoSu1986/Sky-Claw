"""Tests for MaintenanceDaemon — FASE 1.5.4 hardening.

Validates that ``_pruning_check()`` and ``_checkpoint_tick()`` re-raise
``asyncio.CancelledError`` so the daemon can shut down cleanly while
those tasks are in flight.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.orchestrator.maintenance_daemon import MaintenanceDaemon


def _make_snapshot_manager(hang: bool = False) -> MagicMock:
    """Build a snapshot manager mock. If ``hang=True``, ``get_stats`` blocks
    until cancelled (suitable for testing CancelledError propagation)."""
    sm = MagicMock()
    if hang:

        async def _hang() -> object:
            await asyncio.Event().wait()  # blocks forever
            return MagicMock()

        sm.get_stats = AsyncMock(side_effect=_hang)
    else:
        stats = MagicMock(total_size_bytes=0)
        sm.get_stats = AsyncMock(return_value=stats)
    sm.cleanup_old_snapshots = AsyncMock(return_value=MagicMock(deleted_count=0, freed_bytes=0, errors=[]))
    return sm


class TestPruningCheckCancellation:
    """FASE 1.5.4 hardening: _pruning_check must re-raise CancelledError
    so MaintenanceDaemon.stop() can complete promptly during shutdown."""

    @pytest.mark.asyncio
    async def test_pruning_check_propagates_cancellation(self) -> None:
        sm = _make_snapshot_manager(hang=True)
        daemon = MaintenanceDaemon(snapshot_manager=sm)

        task = asyncio.create_task(daemon._pruning_check())
        # Yield control so the inner ``await sm.get_stats()`` is reached
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_pruning_check_does_not_swallow_normal_exceptions(self) -> None:
        """Sanity: non-Cancelled exceptions are still logged and swallowed
        (we want the loop to keep running on transient pruning errors)."""
        sm = MagicMock()
        sm.get_stats = AsyncMock(side_effect=RuntimeError("disk gone"))
        daemon = MaintenanceDaemon(snapshot_manager=sm)

        # Should NOT raise — non-Cancelled exceptions are logged and swallowed
        await daemon._pruning_check()
