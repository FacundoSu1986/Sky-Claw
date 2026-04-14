"""Componentes de layout de la capa de vista.

Contiene componentes estructurales como sidebar, header, footer.
Cada componente es "tonto" - solo maneja presentación visual.
"""

from .header import create_header
from .sidebar import create_sidebar

__all__ = [
    "create_header",
    "create_sidebar",
]
