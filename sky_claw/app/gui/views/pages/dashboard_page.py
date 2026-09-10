"""Página principal del dashboard.

``render_dashboard`` ES la fachada histórica que ``sky_claw_gui.py`` invoca;
hoy delega 1:1 en ``views/forge_dashboard.py`` (shell Forge v4.0). La vieja
isla pre-Forge que vivía debajo (cols/scroller sections, home sections, page
content) no llegaba a ejecutarse: se eliminó con la limpieza de código muerto.

VIEW PURO - Sin lógica de negocio, solo composición de vistas.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def render_dashboard(
    stats: dict[str, Any],
    mods: list[dict[str, Any]],
    chat_messages: list[dict[str, Any]],
    is_thinking: bool,
    callbacks: dict[str, Callable],
    active_section: str = "Dashboard",
    identity: dict[str, str] | None = None,
    search_query: str = "",
    conflicts_list: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    downloads: dict[str, Any] | None = None,
    resolved_conflicts: list[dict[str, Any]] | None = None,
) -> None:
    """Renderiza la página completa del dashboard.

    Compone todas las secciones del dashboard en el layout principal:
    - Sidebar (navegación lateral)
    - Header (encabezado)
    - Stats Section (estadísticas)
    - Features Section (características)
    - Mods Preview + Chat Preview (grid 2 columnas)
    - CTA Section (call-to-action)

    Args:
        stats: Estadísticas para la sección de stats con claves:
            - active_mods: Variable reactiva con número de mods activos
            - pending_updates: Variable reactiva con actualizaciones pendientes
            - conflicts_count: Variable reactiva con conteo de conflictos
            - storage_used: Variable reactiva con almacenamiento usado (GB)
        mods: Lista de mods para preview, cada uno con:
            - name: str - Nombre del mod
            - status: str - Estado ('active', 'update', 'conflict', 'inactive')
            - size_mb: int/float - Tamaño en MB
        chat_messages: Mensajes del chat, cada uno con:
            - content: str - Contenido del mensaje
            - is_user: bool - True si es del usuario
            - timestamp: str - Timestamp del mensaje
        is_thinking: Estado de procesamiento del agente
        callbacks: Dict con callbacks:
            - on_send_message: Callable[[str], None] - Envío de mensaje chat
            - on_view_all_mods: Callable - Ver todos los mods
            - on_mod_click: Callable[[str], None] - Clic en un mod
            - on_navigate: Callable[[str], None] - Navegación
            - on_cta_primary: Callable - Acción principal CTA
            - on_cta_secondary: Callable - Acción secundaria CTA (opcional)
            - on_feature_click: Callable[[str], None] - Clic en feature (opcional)
            - on_mod_toggle: Callable[[str, bool], Awaitable] - Toggle de mod en
              la sección Mods (opcional; sin él los switches se muestran
              deshabilitados)
        active_section: Sección activa de ``NAV_SECTIONS`` (Parte 5). Decide el
            highlight del sidebar Y el contenido del área principal:
            "Dashboard" → home, "Mods" → lista completa, resto → placeholder.
            En producción la provee el store (``store.get("active_section")``).

    Example:
        Los ``stats`` son los proxies reactivos del viewmodel (``ReactiveState``,
        vía ``get_state()`` en ``sky_claw_gui``), no el ``AppState`` puro:

        >>> state = get_state()  # ReactiveState (sky_claw_gui)
        >>> render_dashboard(
        ...     stats={
        ...         'active_mods': state.active_mods,
        ...         'pending_updates': state.pending_updates,
        ...         'conflicts_count': state.conflicts_count,
        ...         'storage_used': state.storage_used,
        ...     },
        ...     mods=[{'name': 'Test Mod', 'status': 'active', 'size_mb': 100}],
        ...     chat_messages=[],
        ...     is_thinking=False,
        ...     callbacks={
        ...         'on_send_message': lambda msg: print(f"Send: {msg}"),
        ...         'on_view_all_mods': lambda: print("View all"),
        ...         'on_mod_click': lambda name: print(f"Mod: {name}"),
        ...         'on_navigate': lambda page: print(f"Navigate: {page}"),
        ...         'on_cta_primary': lambda: print("Start!"),
        ...         'on_cta_secondary': lambda: print("Demo!"),
        ...     },
        ...     active_section="Dashboard",
        ... )
    """
    # v4.0 "Forja del Dovahkiin": el shell completo (sidebar + header Draconato +
    # hero "Salve Dovahkiin" + plaquetas + Rituales + Orden de Carga + Asistente)
    # lo arma el port fiel del mockup HiFi. Mantiene esta firma intacta.
    from ..forge_dashboard import render_forge_dashboard

    render_forge_dashboard(
        stats=stats,
        mods=mods,
        chat_messages=chat_messages,
        is_thinking=is_thinking,
        callbacks=callbacks,
        active_section=active_section,
        identity=identity,
        search_query=search_query,
        conflicts_list=conflicts_list,
        settings=settings,
        downloads=downloads,
        resolved_conflicts=resolved_conflicts,
    )


# ── Helpers compartidos por los controllers activos ──────────────────────────

#: Secciones del sidebar sin vista dedicada (placeholders honestos): usado por
#: el controlador de navegación y su test ancla.
_PLACEHOLDER_SECTIONS: tuple[str, ...] = ("Conflicts", "Downloads", "Settings")


def mods_for_list(mods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapta los mods del dashboard (``status``) al contrato de ``build_mod_list``
    (``enabled``) — formateo visual simple permitido por las reglas de views/."""
    return [
        {
            "name": m.get("name", "Mod desconocido"),
            "enabled": m.get("status") != "inactive",
            "version": str(m.get("version", "")),
        }
        for m in mods
    ]
