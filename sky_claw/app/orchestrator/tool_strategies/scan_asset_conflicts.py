"""Strategies for `scan_asset_conflicts` and `scan_asset_conflicts_json`.

The strategies receive a **narrow callable** (not a detector, not the
supervisor):

- Production wires ``AssetConflictScanner.scan`` / ``.scan_json``
  (``sky_claw/app/orchestrator/asset_conflict_scan.py``) directly into the
  dispatcher. The scanner keeps the lazy detector resolution: ``detector`` is
  built on first access (``MO2_PATH`` may hydrate after the supervisor is
  constructed) and memoized.
- The blocking scan (synchronous ``rglob`` + MD5 over the whole MO2 VFS) runs
  off the event loop via ``asyncio.to_thread``; the strategy neither knows nor
  imports the Supervisor.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from typing import Any


class ScanAssetConflictsStrategy:
    name = "scan_asset_conflicts"

    def __init__(self, scan_callable: Callable[[], list[Any]]) -> None:
        self.scan_callable = scan_callable

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # The scan walks the whole MO2 VFS with synchronous I/O (rglob + MD5);
        # run it off-loop or it starves the event loop for the entire scan.
        conflicts = await asyncio.to_thread(self.scan_callable)
        return {
            "status": "success",
            "conflicts": [dataclasses.asdict(c) for c in conflicts],
        }


class ScanAssetConflictsJsonStrategy:
    name = "scan_asset_conflicts_json"

    def __init__(self, scan_json_callable: Callable[[], str]) -> None:
        self.scan_json_callable = scan_json_callable

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # Same blocking profile as ScanAssetConflictsStrategy.
        json_report = await asyncio.to_thread(self.scan_json_callable)
        return {
            "status": "success",
            "json_report": json_report,
        }
