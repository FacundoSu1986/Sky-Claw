"""AppState - Modelo de Estado Centralizado PURE DATA. FASE 4 MVC."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

#: Secciones canónicas de navegación (Parte 5). Fuente única para controllers
#: y para el sidebar — vive acá (modelo puro) para que los controllers no
#: importen vistas NiceGUI ni viceversa.
NAV_SECTIONS: tuple[str, ...] = ("Dashboard", "Mods", "Conflicts", "Downloads", "Settings")


def enrich_conflicts(
    conflicts: list[dict[str, Any]] | None,
    mods: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Enriquece los conflictos de la DB con los nombres de los mods.

    Seam puro (sin NiceGUI, sin I/O) que consume la pantalla de Conflictos: la
    tabla ``conflicts`` guarda ``mod_id_1/2`` (FKs), así que acá se mapean a
    nombres legibles usando la lista de mods. Ids no encontrados caen a
    "Mod desconocido" y un ``conflict_type`` ausente a "Conflicto", para no
    mostrar campos vacíos.
    """
    names = {m.get("id"): m.get("name", "?") for m in (mods or [])}
    out: list[dict[str, Any]] = []
    for c in conflicts or []:
        out.append(
            {
                "id": c.get("id"),
                "type": c.get("conflict_type") or "Conflicto",
                "mod_a": names.get(c.get("mod_id_1"), "Mod desconocido"),
                "mod_b": names.get(c.get("mod_id_2"), "Mod desconocido"),
                "detected_at": c.get("detected_at"),
                # La nota de resolución (F3) alimenta la sección "Resueltas";
                # None cuando el conflicto sigue pendiente o se resolvió sin nota.
                "resolution": c.get("resolution"),
            }
        )
    return out


@dataclass
class AppState:
    """
    Estado de dominio de Sky-Claw.
    ESTRICTAMENTE PROHIBIDO almacenar widgets, elementos UI o controladores aquí.
    """

    config_path: Path
    max_chat_messages: int = 500
    is_running: bool = True
    is_thinking: bool = False
    wizard_step: int = 1
    # Parte 5: navegación y selección (datos puros, las vistas deciden el render)
    active_section: str = "Dashboard"
    selected_mod: str | None = None

    # GUI estática → funcional: término de búsqueda del header (lo consume la
    # pantalla de Mods para pre-filtrar) e identidad mostrada en el header
    # (data-driven; reemplaza los literales hardcodeados de la vista).
    search_query: str = ""
    user_display_name: str = "Dovahkiin"
    user_role: str = "Maestro de la Forja"

    # Datos puros de los mensajes (diccionarios o strings, NO widgets gráficos)
    _chat_messages: list[dict[str, str]] = field(default_factory=list)

    # Datos de los inputs del usuario, no las cajas de texto físicas
    form_data: dict[str, str] = field(default_factory=dict)

    # Tareas asíncronas de fondo (mantenido por seguridad del event loop)
    _bg_tasks: set[Any] = field(default_factory=set)

    def clear_chat_messages(self) -> None:
        self._chat_messages.clear()

    def add_chat_message(self, role: str, content: str) -> None:
        self._chat_messages.append({"role": role, "content": content})

    def get_message_count(self) -> int:
        return len(self._chat_messages)

    def is_chat_full(self) -> bool:
        return self.get_message_count() >= self.max_chat_messages


# Implementación del Singleton/Factory — thread-safe con Lock
_GLOBAL_APP_STATE: AppState | None = None
_STATE_LOCK = Lock()


def get_app_state(config_path: Path | None = None) -> AppState:
    """Garantiza una única instancia del estado global para evitar desincronizaciones."""
    global _GLOBAL_APP_STATE
    with _STATE_LOCK:
        if _GLOBAL_APP_STATE is None:
            if config_path is None:
                config_path = Path("config.json")
            _GLOBAL_APP_STATE = AppState(config_path=config_path)
    return _GLOBAL_APP_STATE
