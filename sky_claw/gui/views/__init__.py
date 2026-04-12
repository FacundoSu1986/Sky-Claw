"""Sky Claw GUI Views - Capa de Vista (MVVM)

Este paquete contiene componentes visuales puros siguiendo el patrón MVVM.
Los componentes en views/ son "tontos" - solo contienen código de estructura visual.

REGLAS DE ORO:
1. Aislamiento de la Vista: Solo código de estructura visual.
2. PROHIBIDO: Acceso a sistema de archivos, llamadas HTTP/LLM, procesamiento de datos.
3. PERMITIDO: Formateo visual simple (ej. convertir fecha a string para mostrar).
4. Flujo de Datos: Las Vistas reciben datos vía props y callbacks.

Estructura:
- components/ : Componentes atómicos reutilizables (botones, tarjetas, etc.)
- layout/ : Componentes de layout (header, sidebar, etc.)
- sections/ : Secciones compuestas (stats, features, etc.)
- pages/ : Páginas completas (dashboard, mods, etc.)
"""

# Componentes atómicos
from .components.stat_card import create_stat_card
from .components.feature_card import create_feature_card
from .components.buttons import create_cta_button
from .components.mod_item import create_mod_list_item
from .components.chat_bubble import create_chat_message

# Layout
from .layout.sidebar import create_sidebar
from .layout.header import create_header

# Secciones compuestas
from .sections.stats_section import create_stats_section
from .sections.features_section import create_features_section
from .sections.cta_section import create_cta_section
from .sections.mods_preview import create_mods_preview
from .sections.chat_preview import create_chat_preview

# Páginas completas
from .pages.dashboard_page import render_dashboard, render_dashboard_page_content

__all__ = [
    # Componentes atómicos
    "create_stat_card",
    "create_feature_card",
    "create_cta_button",
    "create_mod_list_item",
    "create_chat_message",
    # Layout
    "create_sidebar",
    "create_header",
    # Secciones
    "create_stats_section",
    "create_features_section",
    "create_cta_section",
    "create_mods_preview",
    "create_chat_preview",
    # Páginas
    "render_dashboard",
    "render_dashboard_page_content",
]
