"""Componentes de layout de la capa de vista.

Contiene componentes estructurales como sidebar, header, footer.
Cada componente es "tonto" - solo maneja presentación visual.
"""

from .sidebar import create_sidebar
from .header import create_header

__all__ = [
    "create_sidebar",
    "create_header",
]
