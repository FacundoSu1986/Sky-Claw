"""
FASE 5: Módulo de Detección de Conflictos de Assets.

Este módulo proporciona capacidades de análisis de conflictos de archivos
"loose" (sueltos) dentro del sistema de archivos virtual de Mod Organizer 2.

RESTRICCIÓN DE SEGURIDAD: Este módulo es STRICTLY READ-ONLY.
No debe modificar, mover ni ocultar archivos.
"""

from .asset_scanner import (
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
