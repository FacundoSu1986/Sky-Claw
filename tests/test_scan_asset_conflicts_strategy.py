"""Tests for the scan_asset_conflicts tool strategies.

The asset scan (`AssetConflictDetector.detect_conflicts`) is synchronous by
design (rglob + MD5 over the whole MO2 VFS). The strategies wrap it for the
async tool dispatcher, so they MUST offload the callable off the event loop —
otherwise a large VFS freezes the bus, WS heartbeats and HITL timeouts.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sky_claw.antigravity.orchestrator.tool_strategies.scan_asset_conflicts import (
    ScanAssetConflictsJsonStrategy,
    ScanAssetConflictsStrategy,
)

#: How long the simulated blocking scan takes.
_BLOCKING_SCAN_SECONDS = 0.5
#: Heartbeat resolution while the scan runs.
_HEARTBEAT_INTERVAL = 0.01
#: Minimum heartbeats that prove the loop kept turning during the scan.
#: Ideal is ~50 (0.5s / 10ms); 10 tolerates scheduler jitter on CI.
_MIN_TICKS = 10


async def _count_heartbeats_during(coro_factory: Callable[[], Awaitable[Any]]) -> tuple[Any, int]:
    """Run the strategy while a heartbeat task counts event-loop turns."""
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            ticks += 1

    hb = asyncio.create_task(heartbeat(), name="loop-heartbeat-probe")
    try:
        result = await coro_factory()
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb
    return result, ticks


@dataclasses.dataclass(frozen=True)
class _FakeConflict:
    file_path: str
    winner_mod: str


async def test_scan_strategy_does_not_block_event_loop() -> None:
    """A blocking scan callable must not freeze the event loop."""

    def blocking_scan() -> list[Any]:
        time.sleep(_BLOCKING_SCAN_SECONDS)
        return []

    strategy = ScanAssetConflictsStrategy(blocking_scan)
    result, ticks = await _count_heartbeats_during(lambda: strategy.execute({}))

    assert result["status"] == "success"
    assert ticks >= _MIN_TICKS, (
        f"event loop starved during scan: only {ticks} heartbeats in "
        f"{_BLOCKING_SCAN_SECONDS}s — the blocking callable runs in the loop"
    )


async def test_json_strategy_does_not_block_event_loop() -> None:
    """Same guarantee for the JSON-report variant."""

    def blocking_scan_json() -> str:
        time.sleep(_BLOCKING_SCAN_SECONDS)
        return '{"total_conflicts": 0}'

    strategy = ScanAssetConflictsJsonStrategy(blocking_scan_json)
    result, ticks = await _count_heartbeats_during(lambda: strategy.execute({}))

    assert result["status"] == "success"
    assert result["json_report"] == '{"total_conflicts": 0}'
    assert ticks >= _MIN_TICKS, (
        f"event loop starved during scan: only {ticks} heartbeats in "
        f"{_BLOCKING_SCAN_SECONDS}s — the blocking callable runs in the loop"
    )


async def test_scan_strategy_serializes_conflicts_as_dicts() -> None:
    """Existing contract: conflicts come back as plain dicts (dataclasses.asdict)."""
    conflicts = [_FakeConflict("meshes/a.nif", "Mod A")]
    strategy = ScanAssetConflictsStrategy(lambda: list(conflicts))

    result = await strategy.execute({})

    assert result == {
        "status": "success",
        "conflicts": [{"file_path": "meshes/a.nif", "winner_mod": "Mod A"}],
    }
