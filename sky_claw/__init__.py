"""Sky-Claw – Autonomous Skyrim mod management agent."""

__version__ = "0.1.0"

# FASE 5: Asset Conflict Detection Module
from sky_claw.local.assets import (
    AssetConflictDetector,
    AssetConflictReport,
    AssetInfo,
    AssetType,
)

__all__ = [
    "AssetConflictDetector",
    "AssetConflictReport",
    "AssetInfo",
    "AssetType",
]
