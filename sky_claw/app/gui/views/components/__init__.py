"""Componentes reutilizables de la capa de vista.

Contiene componentes visuales atómicos y reutilizables.
Cada componente es "tonto" - solo maneja presentación visual.

Solo queda ``create_cta_button``: es la dependencia vivo-importada por
``sections/preview_manifest_panel.py`` (que entra en cadena por el import de
``sections/__init__`` en ``forge_dashboard.py``). Las tarjetas/burbujas del
viejo home pre-Forge se eliminaron junto con las secciones muertas.
"""

from __future__ import annotations

from .buttons import create_cta_button

__all__ = [
    "create_cta_button",
]
