"""ChatController — gestión del ciclo de vida del chat y procesamiento LLM.

RESTRICCIÓN: CERO NiceGUI. Solo manipula AppState y EventBus.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.gui.event_bus import EventBus, EventType, SkyClawEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from sky_claw.antigravity.gui.models.app_state import AppState

_logger = logging.getLogger("SkyClaw.ChatController")


class ChatController:
    """
    Gestiona el envío de mensajes, el procesamiento LLM y las respuestas
    del agente. Publica AGENT_STATUS_CHANGE para sincronizar la UI reactiva.

    Dependencias inyectadas:
        app_state:            Estado de dominio puro.
        event_bus:            Bus de eventos Observer.
        agent_client_factory: Callable que retorna AgentCommunicationClient
                              (lazy — evita instanciar fuera del contexto async).
    """

    def __init__(
        self,
        app_state: AppState,
        event_bus: EventBus,
        agent_client_factory: Callable,
    ) -> None:
        self.app_state = app_state
        self.event_bus = event_bus
        self._agent_client_factory = agent_client_factory
        event_bus.subscribe(EventType.LLM_RESPONSE, self.handle_llm_response)

    # ── Public callbacks — wired to views via DI ───────────────────────────────

    async def handle_send_message(self, message: str) -> None:
        """Recibe el mensaje del usuario desde la vista y lo procesa."""
        self.app_state.add_chat_message("user", message)
        await self.process_user_message(message)

    def prepare_messages_for_view(self, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Convierte formato interno de AppState al formato esperado por la vista."""
        return [
            {
                "content": m.get("content", ""),
                "is_user": m.get("role") == "user",
                "timestamp": m.get("timestamp", "Now"),
            }
            for m in messages
        ]

    # ── Internal logic ─────────────────────────────────────────────────────────

    async def process_user_message(self, message: str) -> None:
        """Envía el mensaje al daemon vía WebSocket (fire-and-forget)."""
        self._set_thinking(True)
        try:
            agent_client = self._agent_client_factory()
            sent = await agent_client.send_chat_message(message)
            if not sent:
                # Fallback: daemon desconectado — publicar respuesta local
                self.event_bus.publish(
                    SkyClawEvent(
                        type=EventType.LLM_RESPONSE,
                        data={"response": "⚠️ Daemon offline — message queued."},
                        source="ui_fallback",
                    )
                )
                self._set_thinking(False)
        except Exception as exc:
            _logger.error("Error enviando mensaje al daemon: %s", exc)
            self._set_thinking(False)

    def handle_llm_response(self, event: SkyClawEvent) -> None:
        """Almacena la respuesta del LLM en AppState y resetea is_thinking."""
        response = event.data.get("response", event.data.get("text", ""))
        self.app_state.add_chat_message("assistant", response)
        self._set_thinking(False)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _set_thinking(self, value: bool) -> None:
        """Actualiza AppState y notifica al ViewModel vía EventBus."""
        self.app_state.is_thinking = value
        self.event_bus.publish(
            SkyClawEvent(
                type=EventType.AGENT_STATUS_CHANGE,
                data={"is_thinking": value},
                source="chat_controller",
            )
        )
