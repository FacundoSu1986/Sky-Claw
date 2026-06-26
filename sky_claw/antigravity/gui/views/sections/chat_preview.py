"""Sección de preview del chat con el agente AI del dashboard.

Muestra una interfaz de chat con historial de mensajes y campo de entrada.
Los mensajes y callbacks se reciben como parámetros.

VIEW PURO - Sin lógica de negocio, solo presentación.
Separada de la lógica de procesamiento de mensajes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from nicegui import ui

from ..components import create_chat_message

logger = logging.getLogger(__name__)

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
    "accent_cyan": "#06b6d4",
}


def _do_rollback(
    text: str,
    exc: BaseException,
    restore_fn: Callable[[str], Any],
    notify_fn: Callable[[str], Any],
) -> None:
    """Best-effort rollback: try restore + notify, swallow either's failure.

    A broken ``restore_fn`` must not block the user-facing notification, and
    a broken ``notify_fn`` must not crash the caller — at least one of the
    two will fire so the user gets some signal.
    """
    logger.warning("chat send failed (rollback engaged): %s", exc)
    try:
        restore_fn(text)
    except Exception as restore_exc:  # noqa: BLE001
        logger.error("chat restore_fn raised (input text may be lost): %s", restore_exc)
    try:
        notify_fn(f"⚠️ No se pudo enviar el mensaje: {exc}")
    except Exception as notify_exc:  # noqa: BLE001
        logger.error("chat notify_fn raised (user got no feedback): %s", notify_exc)


def _try_send_with_rollback(
    msg: str,
    on_send: Callable[[str], Any],
    restore_fn: Callable[[str], Any],
    notify_fn: Callable[[str], Any],
    *,
    original_text: str | None = None,
) -> None:
    """Optimistic send: the caller has already cleared the input.

    Rollback triggers for BOTH sync exceptions raised by ``on_send`` AND
    async failures when ``on_send`` returns an awaitable (e.g. the wiring
    ``lambda msg: asyncio.create_task(controller.handle_send_message(msg))``).
    For the async path we attach a ``done_callback`` that inspects the
    future's exception.

    Cancellation is intentionally re-raised on the sync path (explicit
    ``except asyncio.CancelledError: raise``) and ignored on the async
    path so a cancelled task doesn't trigger user-facing notifications.

    ``original_text`` defaults to ``msg`` for legacy callers but should be
    passed by UI handlers that ``.strip()`` the input — restoring the
    stripped version would silently drop leading/trailing whitespace the
    user typed (Copilot review on PR #144).
    """
    text_for_restore = original_text if original_text is not None else msg
    try:
        result = on_send(msg)
    except asyncio.CancelledError:
        raise  # never swallow cancellation
    except Exception as exc:  # noqa: BLE001 — surfaced via notify_fn for UX
        _do_rollback(text_for_restore, exc, restore_fn, notify_fn)
        return

    # Async path: on_send returned an awaitable (Task / Future / coroutine).
    # The real GUI wiring uses ``lambda msg: asyncio.create_task(...)`` so
    # the rollback contract here is what makes daemon/network failures
    # actually trigger the restore-and-notify cycle.
    if asyncio.iscoroutine(result):
        result = asyncio.ensure_future(result)
    if asyncio.isfuture(result):

        def _on_done(task: asyncio.Future[Any]) -> None:
            if task.cancelled():
                return  # cancellation is not a user-facing failure
            exc = task.exception()
            if exc is not None:
                _do_rollback(text_for_restore, exc, restore_fn, notify_fn)

        result.add_done_callback(_on_done)


def create_chat_preview(
    messages: list[dict[str, Any]],
    is_thinking: bool = False,
    on_send_message: Callable[[str], None] | None = None,
    placeholder: str = "Ask me anything about your mods...",
    title: str = "AI Assistant",
    subtitle: str = "Powered by DeepSeek",
    welcome_message: dict[str, Any] | None = None,
) -> ui.element:
    """Preview del chat con el agente.

    Muestra un contenedor de chat con:
    - Header con título y subtítulo
    - Área de mensajes scrolleable
    - Campo de entrada con botón de envío

    Args:
        messages: Lista de mensajes con claves:
            - content: str - Contenido del mensaje
            - is_user: bool - True si es del usuario, False si es del agente
            - timestamp: str - Timestamp del mensaje
        is_thinking: Si el agente está procesando (para mostrar indicador)
        on_send_message: Callback cuando usuario envía mensaje, recibe el texto
        placeholder: Texto placeholder del input
        title: Título del chat (default: "AI Assistant")
        subtitle: Subtítulo del chat (default: "Powered by DeepSeek")
        welcome_message: Mensaje de bienvenida opcional si no hay mensajes

    Returns:
        ui.element: El contenedor principal del chat

    Example:
        >>> messages = [
        ...     {'content': 'Hello!', 'is_user': True, 'timestamp': '10:30'},
        ...     {'content': 'Hi! How can I help?', 'is_user': False, 'timestamp': '10:31'},
        ... ]
        >>> def on_send(text: str):
        ...     print(f"Sending: {text}")
        >>> create_chat_preview(
        ...     messages=messages,
        ...     is_thinking=False,
        ...     on_send_message=on_send,
        ... )
    """
    with ui.element("div").classes(
        "bg-[#0f0f0f] border border-[#1f2937] rounded-2xl overflow-hidden"
    ) as chat_container:
        # ═══════════════════════════════════════════════════════════════
        # HEADER
        # ═══════════════════════════════════════════════════════════════
        with (
            ui.element("div")
            .classes("p-4 border-b border-[#1f2937]")
            .style(f"background: linear-gradient(135deg, {COLORS['accent_violet']}20, {COLORS['accent_cyan']}20);"),
            ui.row().classes("items-center gap-3"),
        ):
            # Icono del agente
            ui.html(f"""
                    <div class="w-10 h-10 rounded-xl flex items-center
                         justify-center sky-glow-static"
                         style="background: linear-gradient(135deg,
                                {COLORS["accent_violet"]},
                                {COLORS["accent_cyan"]});">
                        <svg width="20" height="20" viewBox="0 0 24 24"
                             fill="none" stroke="white" stroke-width="2">
                            <path d="M12 2a10 10 0 0 1 10 10c0 5.52-4.48
                                     10-10 10S2 17.52 2 12 6.48 2 12 2z"/>
                            <path d="M12 8v4"/>
                            <path d="M12 16h.01"/>
                        </svg>
                    </div>
                """)

            with ui.column():
                ui.label(title).classes("text-white font-bold")
                ui.label(subtitle).classes("text-[#6b7280] text-xs")

        # ═══════════════════════════════════════════════════════════════
        # ÁREA DE MENSAJES
        # ═══════════════════════════════════════════════════════════════
        messages_container = ui.element("div").classes("p-4 h-48 overflow-y-auto sky-scrollbar")

        with messages_container:
            # Mostrar mensaje de bienvenida si no hay mensajes
            if not messages and welcome_message:
                create_chat_message(
                    welcome_message.get("content", "Hello! How can I help you?"),
                    is_user=False,
                    timestamp=welcome_message.get("timestamp", "Now"),
                )
            elif not messages:
                # Mensaje de bienvenida por defecto
                create_chat_message(
                    "Hello, Dragonborn! I can help you manage your Skyrim mods. What would you like to do?",
                    is_user=False,
                    timestamp="Now",
                )
            else:
                # Mostrar mensajes existentes
                for msg in messages:
                    create_chat_message(
                        msg.get("content", ""),
                        is_user=msg.get("is_user", False),
                        timestamp=msg.get("timestamp", ""),
                    )

            # Indicador de "pensando" si está procesando
            if is_thinking:
                with ui.row().classes("items-center gap-2 text-[#6b7280]"):
                    ui.spinner("dots", size="sm")
                    ui.label("Thinking...")

        # ═══════════════════════════════════════════════════════════════
        # INPUT AREA
        # ═══════════════════════════════════════════════════════════════
        with ui.element("div").classes("p-4 border-t border-[#1f2937]"), ui.element("div").classes("flex gap-2"):
            chat_input = ui.input(
                placeholder=placeholder,
                value="",
            ).classes(
                "flex-1 bg-[#0a0a0a] border border-[#1f2937] rounded-xl "
                "px-4 py-3 text-white placeholder-[#6b7280] sky-input-premium"
            )

            # Función interna para manejar el envío.
            # P1.5 R-06: clear-before-send con rollback — la UI limpia el input
            # de inmediato (snappy) y si el callback falla restaura el texto y
            # notifica al usuario con ui.notify. ``original_text`` preserva el
            # whitespace que el usuario tipeó (el callback recibe ``msg`` ya
            # stripped) — Copilot review on PR #144.
            def _handle_send():
                original_text = chat_input.value
                msg = original_text.strip()
                if not msg or not on_send_message:
                    return
                chat_input.value = ""  # optimistic clear
                _try_send_with_rollback(
                    msg,
                    on_send=on_send_message,
                    restore_fn=lambda text: setattr(chat_input, "value", text),
                    notify_fn=lambda text: ui.notify(text, type="negative"),
                    original_text=original_text,
                )

            # Enter envía el mensaje (además del botón). Sin este binding el
            # usuario tenía que clickear el botón con el mouse sí o sí.
            chat_input.on("keydown.enter", _handle_send)

            # Botón de envío
            send_button = (
                ui.button()
                .classes("p-3 rounded-xl transition-colors sky-btn-cta")
                .props("ripple")
                .on("click", _handle_send)
            )

            with send_button:
                ui.html("""
                        <svg width="20" height="20" viewBox="0 0 24 24"
                             fill="none" stroke="white" stroke-width="2">
                            <line x1="22" y1="2" x2="11" y2="13"/>
                            <polygon points="22 2 15 22 11 13 2 9 22 2"/>
                        </svg>
                    """)

    return chat_container
