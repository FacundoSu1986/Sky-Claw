"""Secciones compuestas de la capa de vista.

Contiene secciones que combinan múltiples componentes para formar
partes coherentes de la interfaz (ej. stats_section, features_section).
Las secciones son "tontas" - solo componen componentes visuales.
"""

from __future__ import annotations

from .chat_preview import create_chat_preview
from .cta_section import create_cta_section
from .features_section import create_features_section
from .mods_preview import create_mods_preview
from .preview_manifest_panel import build_preview_view_model, create_preview_manifest_panel
from .stats_section import create_stats_section

__all__ = [
    "build_preview_view_model",
    "create_chat_preview",
    "create_cta_section",
    "create_features_section",
    "create_mods_preview",
    "create_preview_manifest_panel",
    "create_stats_section",
]
