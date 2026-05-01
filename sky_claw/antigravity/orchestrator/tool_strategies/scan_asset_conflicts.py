"""Strategies for `scan_asset_conflicts` and `scan_asset_conflicts_json`.

Replaces supervisor.py:328-336. The handlers receive **callables** (not
the cached detector) so that:
  - The lazy-init semantics of supervisor.asset_detector are preserved
    (the property builds the detector on first access).
  - Tests can monkey-patch `supervisor.scan_asset_conflicts` after the
    dispatcher is wired (late-binding via lambda re-resolving attribute).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any


class ScanAssetConflictsStrategy:
    name = "scan_asset_conflicts"

    def __init__(self, scan_callable: Callable[[], list[Any]]) -> None:
        self.scan_callable = scan_callable

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        conflicts = self.scan_callable()
        return {
            "status": "success",
            "conflicts": [dataclasses.asdict(c) for c in conflicts],
        }


class ScanAssetConflictsJsonStrategy:
    name = "scan_asset_conflicts_json"

    def __init__(self, scan_json_callable: Callable[[], str]) -> None:
        self.scan_json_callable = scan_json_callable

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "success",
            "json_report": self.scan_json_callable(),
        }
