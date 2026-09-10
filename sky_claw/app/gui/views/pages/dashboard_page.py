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
    """Renderiza la página completa del dashboard (shell Forge v4.0).

    Fachada de compatibilidad: los parámetros se reenvían 1:1 a
    ``forge_dashboard.render_forge_dashboard``, que dibuja el shell completo
    (sidebar del Draconato, header, hero "Salve Dovahkiin", plaquetas de
    stats, Rituales, Orden de Carga, Asistente Arcano) y despacha el contenido
    por ``active_section`` (Dashboard / Mods / Conflicts / Downloads /
    Settings, todas con vista dedicada en el shell).

    Args:
        stats: Estadísticas del hero/plaquetas — claves esperadas:
            ``active_mods``, ``pending_updates``, ``conflicts_count``,
            ``storage_used``.
        mods: Lista de mods con ``name``, ``status`` ('active', 'update',
            'conflict', 'inactive') y ``size_mb`` (opc. ``version``).
        chat_messages: Mensajes del Asistente con ``content`` e ``is_user``.
        is_thinking: Estado de procesamiento del agente.
        callbacks: Diccionario de callbacks de la UI — los que consume el
            shell (navegación, envío de chat, rituales, HITL, disputas,
            ajustes…) viven documentados junto a cada sección de
            ``forge_dashboard.py``.
        active_section: Sección activa del shell (la provee el store;
            ``store.get("active_section")`` en producción).
        identity: Identidad del usuario (nombre/rol) del sidebar.
        search_query, conflicts_list, resolved_conflicts, settings,
        downloads: Secciones dedicadas del shell; ver
            ``render_forge_dashboard`` para el detalle.

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
        ...     callbacks={'on_navigate': print, 'on_send_message': print},
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
