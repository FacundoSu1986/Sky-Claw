"""Secciones compuestas de la capa de vista.

Contiene los paneles por evento del shell Forge (preflight, flight report,
preview manifest). Las secciones son "tontas" - solo componen componentes
visuales.

Las secciones del viejo home pre-Forge (cta/features/mods_preview/stats) se
eliminaron en el PR de limpieza: el shell Forge no las renderizaba — el home
actual lo dibuja entero ``render_forge_dashboard``.
"""

from __future__ import annotations

from .flight_report_panel import build_flight_report_view_model, create_flight_report_panel
from .preflight_panel import build_preflight_view_model, create_preflight_panel
from .preview_manifest_panel import build_preview_view_model, create_preview_manifest_panel

__all__ = [
    "build_flight_report_view_model",
    "build_preflight_view_model",
    "build_preview_view_model",
    "create_flight_report_panel",
    "create_preflight_panel",
    "create_preview_manifest_panel",
]
