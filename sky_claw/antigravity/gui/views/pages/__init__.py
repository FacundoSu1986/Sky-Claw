"""Páginas completas de la capa de vista.

Contiene páginas que componen secciones y componentes para formar
vistas completas de la aplicación (ej. dashboard_page, mods_page).
Las páginas son "tontas" - solo componen componentes visuales.
"""

from .dashboard_page import render_dashboard, render_dashboard_page_content

__all__ = [
    "render_dashboard",
    "render_dashboard_page_content",
]
