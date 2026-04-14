"""Componente de burbuja de chat.

Burbuja visual para mensajes de chat con estilo diferenciado usuario/agente.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from nicegui import ui


def create_chat_message(
    message: str,
    is_user: bool = False,
    timestamp: str | None = None,
) -> ui.element:
    """Crea una burbuja de mensaje de chat.

    Args:
        message: Texto del mensaje
        is_user: True si el mensaje es del usuario, False si es del agente
        timestamp: Timestamp opcional del mensaje

    Returns:
        ui.element: El elemento contenedor de la burbuja
    """
    cls = (
        "sky-dialog-box sky-dialog-box--user ml-auto w-3/4"
        if is_user
        else "sky-dialog-box sky-dialog-box--agent mr-auto w-3/4"
    )
    with ui.element("div").classes(cls) as bubble:
        ui.label(message).classes("text-[#e5e5e5] text-sm leading-relaxed")
        if timestamp:
            ui.label(timestamp).classes("text-[#6b7280] text-xs mt-1 block")

    return bubble
