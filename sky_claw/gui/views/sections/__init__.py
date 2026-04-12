"""Secciones compuestas de la capa de vista.

Contiene secciones que combinan múltiples componentes para formar
partes coherentes de la interfaz (ej. stats_section, features_section).
Las secciones son "tontas" - solo componen componentes visuales.
"""

from .stats_section import create_stats_section
from .features_section import create_features_section
from .cta_section import create_cta_section
from .mods_preview import create_mods_preview
from .chat_preview import create_chat_preview

__all__ = [
    "create_stats_section",
    "create_features_section",
    "create_cta_section",
    "create_mods_preview",
    "create_chat_preview",
]
