"""Sky-Claw GUI — Registro de iconos SVG.

Convención: cada constante es un SVG inline completo, listo para meter dentro de
``ui.html(f'... {_ICON_X} ...')``. Los del shell Forge heredan el color con
``stroke="currentColor"`` (el wrapper decide); los de marca (ojo de dragón)
llevan sus colores horneados.

El registro legacy del wizard (~12 constantes: layers/mod/pending/conflict/
storage/chat/settings/search/server/chart/anvil/cart) se eliminó: ninguna
tenía consumidores fuera de este archivo, y un registro de iconos con
constantes muertas invita a la duplicación divergente. Si uno vuelve a hacer
falta, el historial de git lo restaura.
"""

from __future__ import annotations

_ICON_ROCKET = """<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/>
    <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/>
    <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>
</svg>"""

# Iconos del shell Forge ("Forja del Dovahkiin"). Reemplazan a los emojis del
# shell (escudo del modal HITL, espadas de disputas, candado del Modo local):
# los emojis se renderizan con la pila de color del SO y rompen la ilusión
# diegética; todos heredan el color vía ``currentColor`` — el wrapper decide.
_ICON_SHIELD_CHECK = """<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1 1 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>
</svg>"""

_ICON_SWORDS = """<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <polyline points="14.5 17.5 3 6 3 3 6 3 17.5 14.5"/><line x1="13" x2="19" y1="19" y2="13"/><line x1="16" x2="20" y1="16" y2="20"/><line x1="19" x2="21" y1="21" y2="19"/><polyline points="14.5 6.5 18 3 21 3 21 6 17.5 9.5"/><line x1="5" x2="9" y1="14" y2="18"/><line x1="7" x2="4" y1="17" y2="20"/><line x1="3" x2="5" y1="19" y2="21"/>
</svg>"""

_ICON_LOCK = """<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
</svg>"""

_ICON_UNLOCK = """<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>
</svg>"""

# Marca de la Forja: el ojo del dragón (D4 del roadmap GUI). Un solo recurso
# compartido por el sidebar del shell y el asistente de primer arranque —
# ninguna copia divergente.
_ICON_DRAGON_EYE = """<svg width="30" height="30" viewBox="0 0 48 48" fill="none" aria-hidden="true">
    <path d="M5 24C13 14 35 14 43 24C35 34 13 34 5 24Z" fill="#0a0705" stroke="#c8a86a" stroke-width="1.5"/>
    <ellipse cx="24" cy="24" rx="9" ry="9" fill="url(#scIris)"/>
    <path d="M24 15C26.6 18.2 26.6 29.8 24 33C21.4 29.8 21.4 18.2 24 15Z" fill="#120a06"/>
    <defs><radialGradient id="scIris" cx="50%" cy="42%" r="60%"><stop offset="0%" stop-color="#ffd071"/><stop offset="55%" stop-color="#d49a36"/><stop offset="100%" stop-color="#7a531f"/></radialGradient></defs>
</svg>"""
