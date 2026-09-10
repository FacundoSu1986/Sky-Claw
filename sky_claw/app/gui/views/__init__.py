"""Sky Claw GUI Views - Capa de Vista (MVVM)

Este paquete contiene componentes visuales puros siguiendo el patrón MVVM.
Los componentes en views/ son "tontos" - solo contienen código de estructura visual.

REGLAS DE ORO:
1. Aislamiento de la Vista: Solo código de estructura visual.
2. PROHIBIDO: Acceso a sistema de archivos, llamadas HTTP/LLM, procesamiento de datos.
3. PERMITIDO: Formateo visual simple (ej. convertir fecha a string para mostrar).
4. Flujo de Datos: Las Vistas reciben datos vía props y callbacks.

Estructura:
- components/ : Componentes atómicos reutilizables (botones)
- sections/ : Secciones compuestas (paneles por evento del shell Forge)
- pages/ : Páginas completas (dashboard)

El shell Forge se renderiza ENTERO desde ``views/forge_dashboard.py``
(``render_forge_dashboard``); el paquete ``views`` solo reexporta la fachada
``render_dashboard`` que ``sky_claw_gui.py`` consume (delega en el Forge). Los
módulos pre-Forge (layout/header, sidebar y las secciones del viejo home) ya no
existen — dos shells/paletas paralelos fue exactamente el defecto que la
limpieza cerró; el ancla ``tests/test_gui_theme_contracts.py`` congela la
superficie pública para no reintroducirlos.
"""

from __future__ import annotations

from .pages.dashboard_page import render_dashboard

__all__ = [
    "render_dashboard",
]
