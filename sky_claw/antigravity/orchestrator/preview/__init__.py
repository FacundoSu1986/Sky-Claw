"""Dry-run / preview subsystem for the LOOT->xEdit->DynDOLOD chain.

Exposes the typed :class:`PreviewManifest` contract and the
:class:`ChainPreviewService` that produces it without permanently mutating
any file (every stage runs inside a force-rollback transaction).
"""

from __future__ import annotations

from sky_claw.antigravity.orchestrator.preview.manifest import (
    ConflictPair,
    ConflictPreview,
    LoadOrderDiff,
    LODPlan,
    PluginMove,
    PreviewManifest,
    StageChangeSet,
    sort_by_master_rules,
)

__all__ = [
    "ConflictPair",
    "ConflictPreview",
    "LODPlan",
    "LoadOrderDiff",
    "PluginMove",
    "PreviewManifest",
    "StageChangeSet",
    "sort_by_master_rules",
]
