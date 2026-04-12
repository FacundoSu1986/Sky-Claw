"""Componentes reutilizables de la capa de vista.

Contiene componentes visuales atómicos y reutilizables.
Cada componente es "tonto" - solo maneja presentación visual.
"""

from .stat_card import create_stat_card
from .feature_card import create_feature_card
from .buttons import create_cta_button
from .mod_item import create_mod_list_item
from .chat_bubble import create_chat_message

__all__ = [
    "create_stat_card",
    "create_feature_card",
    "create_cta_button",
    "create_mod_list_item",
    "create_chat_message",
]
