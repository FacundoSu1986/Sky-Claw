"""Strategies para ``scan_asset_conflicts`` y ``scan_asset_conflicts_json``.

Las strategies reciben un **callable estrecho** (ni un detector ni el
supervisor):

- Producción cablea ``AssetConflictScanner.scan`` / ``.scan_json``
  (``sky_claw/app/orchestrator/asset_conflict_scan.py``) directamente al
  dispatcher. La resolución lazy vive en ``AssetConflictScanner.detector``: el
  detector se construye en el primer acceso (``MO2_PATH`` puede hidratarse
  después de haber construido el supervisor) y se memoiza.
- El escaneo bloqueante (``rglob`` sincrónico + MD5 sobre todo el VFS de MO2)
  corre fuera del event loop vía ``asyncio.to_thread``; la strategy no conoce
  ni importa al Supervisor.
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
