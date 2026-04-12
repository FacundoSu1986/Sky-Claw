"""AppState - Modelo de Estado Centralizado PURE DATA. FASE 4 MVC."""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


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

    # Datos puros de los mensajes (diccionarios o strings, NO widgets gráficos)
    _chat_messages: List[Dict[str, str]] = field(default_factory=list)

    # Datos de los inputs del usuario, no las cajas de texto físicas
    form_data: Dict[str, str] = field(default_factory=dict)

    # Tareas asíncronas de fondo (mantenido por seguridad del event loop)
    _bg_tasks: set = field(default_factory=set)

    def clear_chat_messages(self) -> None:
        self._chat_messages.clear()

    def add_chat_message(self, role: str, content: str) -> None:
        self._chat_messages.append({"role": role, "content": content})

    def get_message_count(self) -> int:
        return len(self._chat_messages)

    def is_chat_full(self) -> bool:
        return self.get_message_count() >= self.max_chat_messages


# Implementación del Singleton/Factory — thread-safe con Lock
_GLOBAL_APP_STATE: Optional[AppState] = None
_STATE_LOCK = Lock()


def get_app_state(config_path: Optional[Path] = None) -> AppState:
    """Garantiza una única instancia del estado global para evitar desincronizaciones."""
    global _GLOBAL_APP_STATE
    with _STATE_LOCK:
        if _GLOBAL_APP_STATE is None:
            if config_path is None:
                config_path = Path("config.json")
            _GLOBAL_APP_STATE = AppState(config_path=config_path)
    return _GLOBAL_APP_STATE
