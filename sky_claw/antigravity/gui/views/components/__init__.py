"""Componentes reutilizables de la capa de vista.

Contiene componentes visuales atómicos y reutilizables.
Cada componente es "tonto" - solo maneja presentación visual.
"""

from .buttons import create_cta_button
from .chat_bubble import create_chat_message
from .feature_card import create_feature_card
from .mod_item import create_mod_list_item
from .stat_card import create_stat_card

__all__ = [
    "create_chat_message",
    "create_cta_button",
    "create_feature_card",
    "create_mod_list_item",
    "create_stat_card",
]
