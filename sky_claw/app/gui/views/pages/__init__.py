"""Páginas completas de la capa de vista.

Contiene páginas que componen secciones y componentes para formar
vistas completas de la aplicación (ej. dashboard_page).
Las páginas son "tontas" - solo componen componentes visuales.

``render_dashboard_page_content`` (superficie del viejo home pre-Forge) se
eliminó con el resto de la isla inalcanzable del dashboard legacy.
"""

from __future__ import annotations

from .dashboard_page import render_dashboard

__all__ = [
    "render_dashboard",
]
